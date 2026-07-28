"""Unit tests for batched Lean verification (assembly + bisection correctness).

The core property: `verify_batch` must return the SAME per-statement verdict as
solo `verify`, only faster. These tests use an injected runner that simulates
Lean, so the logic is checked without a toolchain.
"""

from __future__ import annotations

from pathlib import Path

from leanscreen.verify import (
    LeanVerifier,
    RunnerOutput,
    VerifierStatus,
    assemble_batch,
)


def test_assemble_hoists_imports_and_renames() -> None:
    drafts = [
        "import Mathlib\ntheorem foo : True := by\n  sorry",
        "import Mathlib\nimport Aesop\ntheorem foo : False := by\n  sorry",  # same name!
    ]
    src = assemble_batch(drafts)
    # Imports hoisted to the top, deduped; bodies have no inline import.
    assert src.startswith("import Aesop\nimport Mathlib") or src.startswith("import Mathlib")
    assert src.count("import Mathlib") == 1
    # Collision-prone name `foo` renamed uniquely -> no duplicate-declaration error.
    assert "batch_decl_0" in src and "batch_decl_1" in src
    assert "theorem foo" not in src
    # Each statement is section-isolated.
    assert src.count("section") == 2 and src.count("end") == 2


def test_assemble_strips_unlimited_heartbeats() -> None:
    src = assemble_batch(["set_option maxHeartbeats 0\ntheorem t : True := by\n  sorry"])
    assert "maxHeartbeats" not in src


def _verifier(simulate: object) -> LeanVerifier:
    return LeanVerifier(Path("/nonexistent"), runner=simulate)  # type: ignore[arg-type]


def test_clean_batch_marks_all_valid_in_one_run() -> None:
    runs: list[int] = []

    def runner(source: str) -> RunnerOutput:
        runs.append(source.count("section"))
        return RunnerOutput(output="warning: declaration uses 'sorry'", timed_out=False)

    drafts = [f"theorem t{i} : True := by sorry" for i in range(8)]
    results = _verifier(runner).verify_batch(drafts)
    assert all(r.status is VerifierStatus.VALID for r in results)
    assert len(runs) == 1  # one clean batch settles all 8 — the whole point


def test_dirty_batch_bisects_and_matches_solo() -> None:
    # Statement index 5 is the only invalid one. The marker lives in the GOAL TYPE
    # (not the name) so it survives the assembler's collision-safe renaming.
    def runner(source: str) -> RunnerOutput:
        if "BadGoalType" in source:
            return RunnerOutput(output="x.lean:1:1: error: bad", timed_out=False)
        return RunnerOutput(output="warning: uses 'sorry'", timed_out=False)

    drafts = [f"theorem t{i} : True := by sorry" for i in range(8)]
    drafts[5] = "theorem t5 : BadGoalType := by sorry"
    results = _verifier(runner).verify_batch(drafts)

    statuses = [r.status for r in results]
    assert statuses[5] is VerifierStatus.INVALID
    assert all(statuses[i] is VerifierStatus.VALID for i in range(8) if i != 5)


def test_all_invalid_still_correct() -> None:
    def runner(_source: str) -> RunnerOutput:
        return RunnerOutput(output="error: every one is bad", timed_out=False)

    drafts = [f"theorem t{i} : Bad := by sorry" for i in range(5)]
    results = _verifier(runner).verify_batch(drafts)
    assert all(r.status is VerifierStatus.INVALID for r in results)


def test_empty_batch() -> None:
    assert _verifier(lambda _s: RunnerOutput("", False)).verify_batch([]) == []


def test_crashed_batch_does_not_bless_members() -> None:
    # The whole-batch fast path requires exit 0: a crashed lean (no `error:`
    # text, non-zero exit) must fall through to solo verification, where each
    # member is classified ERROR — never VALID.
    verifier = LeanVerifier(
        Path("/nonexistent"),
        runner=lambda _s: RunnerOutput(output="", timed_out=False, returncode=134),
    )
    results = verifier.verify_batch(
        ["theorem a : True := by sorry", "theorem b : True := by sorry"]
    )
    assert [r.status for r in results] == [VerifierStatus.ERROR, VerifierStatus.ERROR]
