"""Step 6: verify a Lean draft against mathlib — the trustworthy oracle.

Validity (does it typecheck?) is decided by Lean itself, not by an LLM. We write
the draft to a temp file inside the Lean project and run ``lake env lean`` on it.
The verdict rule mirrors what we proved by hand:

    only a `sorry` warning, no `error:` lines  ->  VALID
    any `error:` line                           ->  INVALID

The subprocess runner is injected so the verdict logic is unit-tested without a
Lean toolchain, while the real runner shells out to ``lake``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from leanscreen.errors import VerifierError


class VerifierStatus(StrEnum):
    """Result of running the Lean verifier on a draft (validity, not meaning)."""

    VALID = "valid"  # compiled; at most a `sorry` warning
    INVALID = "invalid"  # Lean reported an error -> malformed statement
    TIMEOUT = "timeout"  # verification exceeded the time budget
    ERROR = "error"  # verifier failed to run (toolchain problem)


# When a batch is dirty (has errors) and shrinks to this size or smaller, we stop
# bisecting and verify each member SOLO (the authoritative path). This bounds the
# worst case (all-invalid) to ~1.25x solo while keeping the big win on clean batches.
_BATCH_SOLO_THRESHOLD = 4

IMPORT_RE = re.compile(r"^\s*import\s+\S+")
# maxHeartbeats 0 (unlimited) would let one bad statement hang the whole batch;
# we strip it so the timeout stays meaningful and bad statements error instead.
_HEARTBEAT_RE = re.compile(r"^\s*set_option\s+maxHeartbeats\b")
_DECL_NAME_RE = re.compile(r"\b(theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_'.]*)")

# A runner takes the full Lean source text and returns (combined_output, timed_out).
LeanRunner = Callable[[str], "RunnerOutput"]


@dataclass(frozen=True, slots=True)
class RunnerOutput:
    """Raw result of executing Lean on a source file."""

    output: str
    timed_out: bool
    returncode: int = 0


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Verdict for one Lean draft."""

    status: VerifierStatus
    output: str
    seconds: float


class Verifier(Protocol):
    """What the pipeline needs from a verifier — satisfied by both the
    subprocess :class:`LeanVerifier` and the persistent ``ReplVerifier``."""

    def verify(self, lean_code: str) -> VerifyResult:
        """Verify one Lean draft."""
        ...

    def verify_batch(self, drafts: list[str]) -> list[VerifyResult]:
        """Verify many drafts with verdicts identical to solo verification."""
        ...


def is_clean_run(run: RunnerOutput) -> bool:
    """True when a Lean run PROVES everything in its file compiled: exit 0, no
    timeout, no error lines. The single predicate behind both the solo verdict
    and the batch fast path, so the two can never drift apart."""
    return not run.timed_out and run.returncode == 0 and "error:" not in run.output


def classify_output(output: str, *, timed_out: bool, returncode: int = 0) -> VerifierStatus:
    """Map raw Lean output to a verdict.

    A line containing ``error:`` means the statement is malformed (INVALID).
    A non-zero exit with NO recognizable error line means the toolchain died
    abnormally (panic, OOM, missing file) — that is ERROR, never VALID: a
    crash must not silently bless a draft. Otherwise VALID (a lone ``sorry``
    warning is expected and fine, and exits 0).
    """
    if timed_out:
        return VerifierStatus.TIMEOUT
    for line in output.splitlines():
        if "error:" in line:
            return VerifierStatus.INVALID
    if returncode != 0:
        return VerifierStatus.ERROR
    return VerifierStatus.VALID


def ensure_import(lean_code: str) -> str:
    """Prepend ``import Mathlib`` unless the draft already imports it."""
    if "import Mathlib" in lean_code:
        return lean_code
    return f"import Mathlib\n\n{lean_code}\n"


# Mathlib renamed the big-operator binder keyword (`∑ x in s,` -> `∑ x ∈ s,`);
# external benchmarks predating the rename fail to parse against our pin —
# 73/90 residual elaboration failures across the 2026-07-16 census corpora.
# Statement content is unchanged: this is pure surface syntax.
_LEGACY_BINDER_RE = re.compile(r"([∑∏⋃⋂⨆⨅]\s[^,∑∏⋃⋂⨆⨅]*?)\sin\s")


def normalize_legacy_syntax(lean_code: str) -> str:
    """Rewrite pre-rename big-operator binders (`∑ x in s,` -> `∑ x ∈ s,`)
    for ELABORATION ONLY — the stored statement text is never modified."""
    return _LEGACY_BINDER_RE.sub(r"\1 ∈ ", lean_code)


_OPEN_PROOF_TAIL_RE = re.compile(r":=\s*(by)?\s*$")
_STATEMENT_HEAD_RE = re.compile(r"^\s*(theorem|lemma|example)\b")


def ensure_proof_stub(lean_code: str) -> str:
    """Close a statement-only declaration so it parses.

    Two shapes: a trailing `:=` or `:= by` gets a `sorry` appended (external
    benchmarks ship statements with the proof in a separate column — 345/371
    ProofNet# rows hit `unexpected end of input` in the 2026-07-16 backfill
    until stubbed), and a theorem-family statement with NO `:=` at all gets
    `:= sorry` (the common in-loop drafting shape; found 2026-07-28 when the
    MCP parity pair could never elaborate). Definitions and structures are
    left untouched in the no-`:=` case — a bodiless `def` is genuinely
    incomplete, not a statement awaiting its proof. A statement already
    ending in a proof term (or `sorry`) is returned untouched."""
    tail = lean_code.rstrip()
    if _OPEN_PROOF_TAIL_RE.search(tail):
        return tail + " sorry"
    if ":=" not in tail and _STATEMENT_HEAD_RE.match(tail):
        return tail + " := sorry"
    return lean_code


def extract_errors(output: str, *, max_chars: int = 2000) -> str:
    """Pull just the Lean error diagnostics out of raw verifier output.

    This is the signal fed back to the model for correction: each ``error:``
    line plus its immediate continuation lines (Lean often prints the offending
    term and an expected/actual type on following lines), with ``sorry``
    warnings and unrelated noise dropped. Falls back to the whole output if no
    ``error:`` line is found, and is length-capped so one pathological message
    can't bloat a prompt.
    """
    kept: list[str] = []
    capturing = False
    for line in output.splitlines():
        if "error:" in line:
            capturing = True
            kept.append(line.rstrip())
        elif capturing and (not line.strip() or "warning:" in line):
            capturing = False  # a blank line or the next warning ends this error
        elif capturing:
            kept.append(line.rstrip())
    text = "\n".join(kept).strip() or output.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + " …[truncated]"
    return text


def _prepare_unit(draft: str, index: int) -> tuple[set[str], str]:
    """Turn one draft into a collision-safe, isolated unit for a batch file.

    Returns (imports found, unit body). The unit:
    * has its ``import`` lines hoisted out (collected separately, deduped once),
    * has ``set_option maxHeartbeats`` lines dropped,
    * has its declaration renamed to a unique ``batch_decl_<i>`` (no collisions),
    * is wrapped in ``section ... end`` so any ``open``/``set_option``/``variable``
      is scoped to this statement and cannot leak into the next one.
    """
    imports: set[str] = set()
    body_lines: list[str] = []
    for line in draft.splitlines():
        if IMPORT_RE.match(line):
            imports.add(line.strip())
            continue
        if _HEARTBEAT_RE.match(line):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    body = _DECL_NAME_RE.sub(rf"\1 batch_decl_{index}", body, count=1)
    return imports, f"section\n{body}\nend"


def assemble_batch(drafts: list[str]) -> str:
    """Assemble many drafts into one Lean file with a single ``import Mathlib``.

    Each statement is uniquely renamed and ``section``-isolated, so every
    declaration's validity is identical to verifying it alone — the file only
    amortizes the (expensive) mathlib load across all of them.
    """
    all_imports: set[str] = set()
    units: list[str] = []
    for i, draft in enumerate(drafts):
        imports, unit = _prepare_unit(draft, i)
        all_imports |= imports
        units.append(unit)
    all_imports.add("import Mathlib")
    header = "\n".join(sorted(all_imports))
    return header + "\n\n" + "\n\n".join(units) + "\n"


class LeanVerifier:
    """Verifies Lean drafts by invoking ``lake env lean`` in the project."""

    def __init__(
        self,
        project_path: Path,
        *,
        timeout_seconds: float = 180.0,
        runner: LeanRunner | None = None,
    ) -> None:
        self._project_path = project_path
        self._timeout = timeout_seconds
        self._runner = runner if runner is not None else self._subprocess_runner

    def verify(self, lean_code: str) -> VerifyResult:
        """Verify one Lean draft; returns a validity verdict (never raises on a
        Lean *rejection* — only on the toolchain failing to run)."""
        source = ensure_import(lean_code)
        start = time.perf_counter()
        result = self._runner(source)
        elapsed = time.perf_counter() - start
        status = classify_output(
            result.output, timed_out=result.timed_out, returncode=result.returncode
        )
        return VerifyResult(status=status, output=result.output, seconds=elapsed)

    def verify_batch(self, drafts: list[str]) -> list[VerifyResult]:
        """Verify many drafts, amortizing the mathlib load — without changing any
        verdict versus :meth:`verify`.

        Correctness guarantee: a batch that compiles with NO ``error:`` lines
        proves every statement in it is valid (Lean would have reported an error
        otherwise). Any dirty batch is bisected; once small it falls back to
        per-statement :meth:`verify` (the authoritative oracle). So each result
        equals what solo verification would have produced — only faster.
        """
        results: list[VerifyResult | None] = [None] * len(drafts)
        if drafts:
            self._verify_indices(drafts, list(range(len(drafts))), results)
        # mypy: every slot is filled by _verify_indices.
        return [r for r in results if r is not None]

    def _verify_indices(
        self,
        drafts: list[str],
        indices: list[int],
        results: list[VerifyResult | None],
    ) -> None:
        source = assemble_batch([drafts[i] for i in indices])
        start = time.perf_counter()
        run = self._runner(source)
        elapsed = time.perf_counter() - start

        if is_clean_run(run):
            # Clean batch (exit 0, no errors) => every member is provably VALID.
            # The returncode check matters: a CRASHED lean (panic, OOM) prints no
            # `error:` line — without it a dead toolchain would bless the batch.
            share = elapsed / len(indices)
            for i in indices:
                results[i] = VerifyResult(VerifierStatus.VALID, "", share)
            return

        # Dirty (error or timeout): isolate. Small => authoritative solo verify.
        if len(indices) <= _BATCH_SOLO_THRESHOLD:
            for i in indices:
                results[i] = self.verify(drafts[i])
            return

        mid = len(indices) // 2
        self._verify_indices(drafts, indices[:mid], results)
        self._verify_indices(drafts, indices[mid:], results)

    def _subprocess_runner(self, source: str) -> RunnerOutput:
        """Default runner: write a temp .lean file in the project and run lake."""
        if not self._project_path.is_dir():
            raise VerifierError(f"Lean project path not found: {self._project_path}")
        tmp = self._project_path / f"_verify_{uuid.uuid4().hex}.lean"
        try:
            tmp.write_text(source, encoding="utf-8")
            try:
                # Own process group: `lake` runs `lean` as a CHILD, and a plain
                # timeout kill reaps only lake — the lean grandchild survives as
                # an orphan still elaborating while the dirty-batch bisection
                # launches more runs beside it. Group-kill takes the tree down.
                proc = subprocess.Popen(
                    ["lake", "env", "lean", tmp.name],
                    cwd=self._project_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise VerifierError(
                    "`lake` not found — is the Lean toolchain installed and on PATH?"
                ) from exc
            try:
                stdout, stderr = proc.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                try:
                    pgid = os.getpgid(proc.pid)
                    if pgid == os.getpgid(0):
                        raise ProcessLookupError  # shared group: killpg = suicide
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()  # group gone/shared: fall back to the wrapper
                proc.wait(timeout=10)
                return RunnerOutput(output="<timeout>", timed_out=True)
            return RunnerOutput(
                output=stdout + stderr,
                timed_out=False,
                returncode=proc.returncode,
            )
        finally:
            tmp.unlink(missing_ok=True)
