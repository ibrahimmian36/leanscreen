"""Unit tests for the persistent REPL verifier (fake transport, no Lean)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from leanscreen.lean_server import (
    ReplTimeoutError,
    ReplVerifier,
    classify_response,
    strip_imports,
)
from leanscreen.verify import VerifierStatus


class _FakeProcess:
    """Stand-in for ReplProcess: a 'hang' command times out; tracks start/kill.

    Lets us drive ReplVerifier's real kill/restart recovery path (verify ->
    ReplTimeoutError -> kill -> next call restarts) without spawning Lean.
    """

    def __init__(self) -> None:
        self.starts = 0
        self.kills = 0
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def start(self) -> None:
        self._ready = True
        self.starts += 1

    @property
    def env_id(self) -> int:
        return 0

    def send(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if "hang" in payload["cmd"]:
            raise ReplTimeoutError("simulated hang")
        return {"messages": []}

    def kill(self) -> None:
        self._ready = False
        self.kills += 1


def test_classify_sorry_warning_is_valid() -> None:
    status, out = classify_response(
        {"messages": [{"severity": "warning", "data": "declaration uses 'sorry'"}], "env": 1}
    )
    assert status is VerifierStatus.VALID
    assert "sorry" in out


def test_classify_error_is_invalid() -> None:
    status, out = classify_response(
        {"messages": [{"severity": "error", "data": "unknown identifier 'Foo'"}], "env": 1}
    )
    assert status is VerifierStatus.INVALID
    assert "unknown identifier" in out


def test_classify_no_messages_is_valid() -> None:
    assert classify_response({"env": 2})[0] is VerifierStatus.VALID


def test_strip_imports_removes_only_import_lines() -> None:
    code = "import Mathlib\n\ntheorem t : 1 = 1 := by\n  sorry"
    assert strip_imports(code) == "theorem t : 1 = 1 := by\n  sorry"


def test_verifier_uses_transport_and_maps_verdicts() -> None:
    sent: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append(payload)
        if "broken" in payload["cmd"]:
            return {"messages": [{"severity": "error", "data": "nope"}]}
        return {"messages": [{"severity": "warning", "data": "declaration uses 'sorry'"}]}

    v = ReplVerifier(Path("/nonexistent"), Path("/nonexistent"), transport=transport)
    ok = v.verify("import Mathlib\ntheorem t : 1 = 1 := by\n  sorry")
    bad = v.verify("theorem broken : nonsense := by\n  sorry")
    assert ok.status is VerifierStatus.VALID
    assert bad.status is VerifierStatus.INVALID
    # imports are stripped before sending — the env already has mathlib
    assert "import Mathlib" not in sent[0]["cmd"]


def test_verify_batch_is_per_statement() -> None:
    def transport(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        return {"messages": []}

    v = ReplVerifier(Path("/x"), Path("/x"), transport=transport)
    results = v.verify_batch(["theorem a : True := by sorry", "theorem b : True := by sorry"])
    assert len(results) == 2
    assert all(r.status is VerifierStatus.VALID for r in results)


def test_verify_batch_maps_mixed_verdicts() -> None:
    # A batch with a bad statement in the middle: each gets its OWN verdict.
    def transport(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if "bad" in payload["cmd"]:
            return {"messages": [{"severity": "error", "data": "nope"}]}
        return {"messages": []}

    v = ReplVerifier(Path("/x"), Path("/x"), transport=transport)
    results = v.verify_batch(
        [
            "theorem a : True := by sorry",
            "theorem bad : nonsense := by sorry",
            "theorem c : True := by sorry",
        ]
    )
    assert [r.status for r in results] == [
        VerifierStatus.VALID,
        VerifierStatus.INVALID,
        VerifierStatus.VALID,
    ]


def test_timeout_in_one_statement_is_isolated_and_process_restarts() -> None:
    # THE isolation guarantee: a statement that times out is killed and reported
    # TIMEOUT, while its neighbours in the batch still get correct verdicts — and
    # the process is restarted so the next statement runs in a clean environment.
    fake = _FakeProcess()
    v = ReplVerifier(Path("/x"), Path("/x"), process=fake)  # type: ignore[arg-type]
    results = v.verify_batch(
        [
            "theorem a : True := by sorry",
            "theorem hang : True := by sorry",  # the _FakeProcess times out on "hang"
            "theorem c : True := by sorry",
        ]
    )
    assert [r.status for r in results] == [
        VerifierStatus.VALID,
        VerifierStatus.TIMEOUT,
        VerifierStatus.VALID,
    ]
    assert results[1].output == "<timeout>"
    assert fake.kills == 1  # the wedged process was killed after the timeout
    assert fake.starts == 2  # ...and restarted for the surviving neighbour


def test_send_wraps_broken_pipe_as_verifier_error() -> None:
    # TOCTOU: the process can die between the poll() check and the write.
    import queue as _queue
    from types import SimpleNamespace

    from leanscreen.errors import VerifierError
    from leanscreen.lean_server import ReplProcess

    proc = ReplProcess(Path("/x"), Path("/x"))

    class _DyingStdin:
        def write(self, _s: str) -> None:
            raise BrokenPipeError("repl died")

        def flush(self) -> None:  # pragma: no cover
            pass

    proc._proc = SimpleNamespace(  # type: ignore[assignment]
        poll=lambda: None,
        stdin=_DyingStdin(),
        stdout=None,
        kill=lambda: None,
        wait=lambda timeout: None,
    )
    proc._lines = _queue.Queue()
    with pytest.raises(VerifierError, match="died mid-send"):
        proc.send({"cmd": "x"}, timeout=1.0)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="process groups are POSIX-only; Windows uses plain Popen.kill()",
)
def test_kill_takes_down_the_whole_process_group(tmp_path: Path) -> None:
    """killpg semantics: the repl is lake's CHILD — killing only the wrapper
    orphaned it with its mathlib heap (the historic 25-minute stalls)."""
    import os
    import signal
    import subprocess
    import time

    from leanscreen.lean_server import ReplProcess

    rp = ReplProcess(tmp_path, tmp_path / "repl")  # paths unused by kill()
    rp._proc = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo $!; wait"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert rp._proc.stdout is not None
    child_pid = int(rp._proc.stdout.readline())

    rp.kill()

    assert rp._proc is None  # leader reaped and state reset
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break  # the grandchild died with the group — the point of killpg
        time.sleep(0.05)
    else:
        os.kill(child_pid, signal.SIGKILL)  # don't leak the sleep on failure
        raise AssertionError("group kill left the child process alive")


def test_kill_never_group_kills_its_own_process_group(tmp_path: Path) -> None:
    """A child spawned WITHOUT start_new_session shares OUR group — killpg
    would SIGKILL the caller (it took down a whole pytest run once). kill()
    must detect the shared group and fall back to a plain single-process kill."""
    import subprocess

    from leanscreen.lean_server import ReplProcess

    rp = ReplProcess(tmp_path, tmp_path / "repl")
    rp._proc = subprocess.Popen(["sleep", "30"])  # same process group as us
    rp.kill()
    assert rp._proc is None  # we are alive to assert it, and the child is reaped
