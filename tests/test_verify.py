"""Unit tests for the Lean verifier verdict logic (no Lean toolchain needed)."""

from __future__ import annotations

from pathlib import Path

from leanscreen.verify import (
    LeanVerifier,
    RunnerOutput,
    VerifierStatus,
    classify_output,
    ensure_import,
    extract_errors,
)


def test_classify_valid_on_sorry_warning() -> None:
    out = "Test.lean:3:8: warning: declaration uses 'sorry'"
    assert classify_output(out, timed_out=False) is VerifierStatus.VALID


def test_extract_errors_keeps_error_drops_sorry_warning() -> None:
    out = (
        "f.lean:3:8: warning: declaration uses 'sorry'\n"
        "f.lean:3:0: error: unknown identifier 'Nat.foo'\n"
    )
    errors = extract_errors(out)
    assert "unknown identifier 'Nat.foo'" in errors
    assert "sorry" not in errors  # the benign warning is dropped


def test_extract_errors_keeps_continuation_lines() -> None:
    out = (
        "f.lean:3:0: error: type mismatch\n"
        "  expected ℝ\n"
        "  got ℕ\n"
        "f.lean:5:0: warning: declaration uses 'sorry'\n"
    )
    errors = extract_errors(out)
    assert "type mismatch" in errors and "expected ℝ" in errors and "got ℕ" in errors
    assert "declaration uses 'sorry'" not in errors  # capture stops at the warning


def test_extract_errors_falls_back_to_full_output() -> None:
    # No `error:` line (e.g. a panic) -> hand back the raw output rather than "".
    assert extract_errors("PANIC at unreachable code") == "PANIC at unreachable code"


def test_extract_errors_is_length_capped() -> None:
    out = "f.lean:1:0: error: " + ("x" * 5000)
    assert len(extract_errors(out, max_chars=200)) <= 200 + len(" …[truncated]")


def test_classify_invalid_on_error() -> None:
    out = "Test.lean:3:8: error: unknown identifier 'foo'"
    assert classify_output(out, timed_out=False) is VerifierStatus.INVALID


def test_classify_valid_on_clean_output() -> None:
    assert classify_output("", timed_out=False) is VerifierStatus.VALID


def test_classify_timeout() -> None:
    assert classify_output("anything", timed_out=True) is VerifierStatus.TIMEOUT


def test_ensure_import_prepends_once() -> None:
    assert ensure_import("theorem t : True := by sorry").startswith("import Mathlib")
    already = "import Mathlib\n\ntheorem t : True := by sorry"
    assert ensure_import(already) == already


def test_verifier_uses_injected_runner_and_adds_import() -> None:
    seen: dict[str, str] = {}

    def runner(source: str) -> RunnerOutput:
        seen["source"] = source
        return RunnerOutput(output="warning: declaration uses 'sorry'", timed_out=False)

    # The injected runner is used, so the project path is never touched.
    verifier = LeanVerifier(Path("/nonexistent"), runner=runner)
    result = verifier.verify("theorem t : True := by\n  sorry")
    assert result.status is VerifierStatus.VALID
    assert seen["source"].startswith("import Mathlib")  # import injected
    assert result.seconds >= 0.0


def test_verifier_reports_invalid() -> None:
    def runner(_source: str) -> RunnerOutput:
        return RunnerOutput(output="x.lean:1:1: error: bad", timed_out=False)

    result = LeanVerifier(Path("/nonexistent"), runner=runner).verify("theorem t")
    assert result.status is VerifierStatus.INVALID


def test_crash_without_error_text_is_never_valid() -> None:
    # A panicked toolchain (non-zero exit, no `error:` line) must NOT bless a draft.
    assert classify_output("", timed_out=False, returncode=134) is VerifierStatus.ERROR
    assert (
        classify_output("INTERNAL PANIC: out of memory", timed_out=False, returncode=1)
        is VerifierStatus.ERROR
    )


def test_error_line_still_wins_over_returncode() -> None:
    # Lean exits 1 when it reports errors — that is a normal INVALID, not ERROR.
    out = "x.lean:1:8: error: unknown identifier 'Foo'"
    assert classify_output(out, timed_out=False, returncode=1) is VerifierStatus.INVALID


def test_verifier_crash_returns_error_status() -> None:
    verifier = LeanVerifier(
        Path("/nonexistent"),
        runner=lambda _s: RunnerOutput(output="", timed_out=False, returncode=139),
    )
    result = verifier.verify("theorem t : True := by\n  sorry")
    assert result.status is VerifierStatus.ERROR
