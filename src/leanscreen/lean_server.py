"""Persistent Lean verifier: mathlib loads ONCE, statements verify in seconds.

The classic path (``lake env lean file.lean``) re-imports mathlib on every
invocation (~60s). This module drives the community REPL
(https://github.com/leanprover-community/repl) instead: one long-lived process
imports mathlib a single time at startup, then each statement is checked
against that environment via a JSON stdin/stdout protocol in ~1-3s.

Contract: identical verdicts to the subprocess verifier — Lean itself is still
the oracle; only the import cost is amortized. On a crash or per-command
timeout the process is killed and lazily restarted (paying one re-import).
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from leanscreen.errors import VerifierError
from leanscreen.verify import IMPORT_RE, VerifierStatus, VerifyResult

# Importing mathlib into the REPL is a one-off cost; give it generous room.
_IMPORT_TIMEOUT_SECONDS = 600.0

# A transport sends one REPL command and returns the decoded JSON response.
# Injected in tests; the real one talks to the repl subprocess.
ReplTransport = Callable[[dict[str, Any], float], dict[str, Any]]


class ReplTimeoutError(Exception):
    """A single REPL command exceeded its time budget."""


@dataclass(frozen=True, slots=True)
class _Message:
    severity: str
    text: str


def _parse_messages(response: dict[str, Any]) -> list[_Message]:
    out: list[_Message] = []
    for m in response.get("messages", []):
        if isinstance(m, dict):
            out.append(_Message(str(m.get("severity", "")), str(m.get("data", ""))))
    return out


def classify_response(response: dict[str, Any]) -> tuple[VerifierStatus, str]:
    """Map a REPL response to (verdict, human-readable output).

    Same rule as the subprocess verifier: any error message -> INVALID;
    otherwise VALID (a lone `sorry` warning is expected and fine).
    """
    messages = _parse_messages(response)
    text = "\n".join(f"{m.severity}: {m.text}" for m in messages)
    if any(m.severity == "error" for m in messages):
        return VerifierStatus.INVALID, text
    return VerifierStatus.VALID, text


def strip_imports(lean_code: str) -> str:
    """Drop ``import`` lines — the REPL environment already has mathlib loaded."""
    return "\n".join(line for line in lean_code.splitlines() if not IMPORT_RE.match(line)).strip()


def _pump_lines(stream: IO[str] | None, out: queue.Queue[str | None]) -> None:
    """Reader thread: every stdout line into the queue; None on EOF."""
    assert stream is not None
    for line in stream:
        out.put(line)
    out.put(None)


class ReplProcess:
    """Owns the repl subprocess and the line-oriented JSON protocol."""

    def __init__(self, project_path: Path, repl_binary: Path) -> None:
        self._project_path = project_path
        self._repl_binary = repl_binary
        self._proc: subprocess.Popen[str] | None = None
        self._env_id: int | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()

    @property
    def ready(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._env_id is not None

    def start(self) -> None:
        """Spawn the repl and import mathlib once (the slow part)."""
        if not self._project_path.is_dir():
            raise VerifierError(f"Lean project path not found: {self._project_path}")
        if not self._repl_binary.is_file():
            raise VerifierError(f"Lean repl binary not found: {self._repl_binary}")
        self._proc = subprocess.Popen(
            ["lake", "env", str(self._repl_binary)],
            cwd=self._project_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            # Own process group: the actual repl is lake's CHILD, and killing
            # only lake left the repl alive holding gigabytes of mathlib —
            # wedged mid-elaboration, re-parented to pid 1, stacking up once
            # per timeout while the run started fresh repls beside it (the
            # historic 25-minute stalls). kill() takes down the whole group.
            start_new_session=True,
        )
        # A dedicated reader thread feeds a queue. NEVER select() on a buffered
        # text stream: readline() can pull the response terminator into Python's
        # internal buffer, after which the fd looks empty and select blocks
        # forever on data we already hold (a deadlock we hit live).
        self._lines = queue.Queue()
        threading.Thread(
            target=_pump_lines, args=(self._proc.stdout, self._lines), daemon=True
        ).start()
        response = self.send({"cmd": "import Mathlib"}, _IMPORT_TIMEOUT_SECONDS)
        env = response.get("env")
        if not isinstance(env, int):
            self.kill()
            raise VerifierError(f"repl did not return an env for mathlib import: {response}")
        self._env_id = env

    @property
    def env_id(self) -> int:
        assert self._env_id is not None, "start() must succeed before env_id"
        return self._env_id

    def send(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """One round-trip: JSON command in, JSON response out (blank-line framed)."""
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise VerifierError("repl process is not running")
        try:
            proc.stdin.write(json.dumps(payload) + "\n\n")
            proc.stdin.flush()
        except OSError as exc:  # TOCTOU: the process can die between poll() and write
            self.kill()
            raise VerifierError(f"repl died mid-send: {exc}") from exc
        return self._read_response(timeout)

    def _read_response(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        buf: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReplTimeoutError(f"repl command exceeded {timeout:.0f}s")
            try:
                line = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:  # EOF — process died
                raise VerifierError("repl process closed its stdout")
            if line.strip() == "":
                if buf:  # blank line terminates a response
                    try:
                        return dict(json.loads("".join(buf)))
                    except json.JSONDecodeError:
                        continue  # interior blank line of a pretty-printed object
                continue
            buf.append(line)

    def kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            # Kill the PROCESS GROUP (lake + the repl it spawned). Killing only
            # the lake wrapper orphans the repl with its mathlib heap.
            # AttributeError/TypeError: test seams inject fake procs without a
            # real pid — fall back to their plain kill() like the group cases.
            try:
                pgid = os.getpgid(self._proc.pid)
                if pgid == os.getpgid(0):
                    # The child shares OUR process group (spawned without
                    # start_new_session, e.g. by a test harness): killpg here
                    # would SIGKILL the calling process too. Plain kill only.
                    raise ProcessLookupError
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError, AttributeError, TypeError):
                self._proc.kill()  # group gone/foreign/shared: fall back
            self._proc.wait(timeout=10)
        self._proc = None
        self._env_id = None
        # Defensive: a fresh queue so no line from a dead process can ever be
        # read as a response to a future command (start() also replaces it).
        self._lines = queue.Queue()


class ReplVerifier:
    """Drop-in verifier backed by a persistent REPL session.

    Public surface mirrors :class:`LeanVerifier` (verify / verify_batch) so the
    pipeline is indifferent to which one it gets. With a warm environment there
    is no import cost to amortize, so verify_batch is simply per-statement
    verification — and unlike the batched file, one statement's timeout cannot
    take neighbours down with it.
    """

    def __init__(
        self,
        project_path: Path,
        repl_binary: Path,
        *,
        timeout_seconds: float = 180.0,
        transport: ReplTransport | None = None,
        process: ReplProcess | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        # `process` is a test seam: inject a fake to exercise the kill/restart
        # recovery path (a statement timing out, the process being killed, and the
        # next statement restarting it) WITHOUT spawning real Lean. Defaults real.
        self._process = process if process is not None else ReplProcess(project_path, repl_binary)
        self._transport = transport

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._transport is not None:  # test seam
            return self._transport(payload, self._timeout)
        if not self._process.ready:
            self._process.start()
        payload = {**payload, "env": self._process.env_id}
        return self._process.send(payload, self._timeout)

    def verify(self, lean_code: str) -> VerifyResult:
        """Verify one draft against the warm mathlib environment."""
        body = strip_imports(lean_code)
        start = time.perf_counter()
        try:
            response = self._send({"cmd": body})
        except ReplTimeoutError:
            # The env may be wedged mid-elaboration: kill it; next call restarts.
            self._process.kill()
            elapsed = time.perf_counter() - start
            return VerifyResult(VerifierStatus.TIMEOUT, "<timeout>", elapsed)
        elapsed = time.perf_counter() - start
        status, output = classify_response(response)
        return VerifyResult(status, output, elapsed)

    def verify_batch(self, drafts: list[str]) -> list[VerifyResult]:
        """Per-statement verification (no import cost to amortize)."""
        return [self.verify(d) for d in drafts]

    def close(self) -> None:
        """Terminate the repl subprocess."""
        self._process.kill()
