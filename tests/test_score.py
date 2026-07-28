"""The single-pair scoring core: verdict assembly, judge rules, proof stubs."""

from __future__ import annotations

import json

import pytest

from leanscreen.backtranslate import BackReview
from leanscreen.score import ExternalPair, score_pair
from leanscreen.verify import VerifyResult


class _FakeAid:
    def __init__(self, verdict: str = "faithful", issues: str = "") -> None:
        self.verdict = verdict
        self.issues = issues
        self.calls = 0

    def review(self, informal: str, lean_code: str) -> BackReview:
        self.calls += 1
        return BackReview(
            back_english="rendering", verdict=self.verdict, issues=self.issues, raw=""
        )


class _FlippingAid:
    """Mismatch on the first call, faithful on repeats (a one-off flip)."""

    def __init__(self) -> None:
        self.calls = 0

    def review(self, informal: str, lean_code: str) -> BackReview:
        self.calls += 1
        verdict = "mismatch" if self.calls == 1 else "faithful"
        return BackReview(back_english="r", verdict=verdict, issues="", raw="")


class _FakeVerifier:
    def __init__(self, status: str, output: str = "") -> None:
        self._status = status
        self._output = output

    def verify(self, lean_code: str) -> VerifyResult:
        return VerifyResult(status=self._status, output=self._output, seconds=0.0)  # type: ignore[arg-type]

    def verify_batch(self, drafts: list[str]) -> list[VerifyResult]:
        return [self.verify(d) for d in drafts]


class _FakeProber:
    def __init__(self, falsified: bool) -> None:
        self._falsified = falsified

    def complete(self, system: str, user: str, temperature=None, *, cache_prefix=None) -> str:  # type: ignore[no-untyped-def]
        return json.dumps(
            {
                "falsified": self._falsified,
                "counterexample": "n := 0",
                "check": "hypotheses hold; conclusion fails",
                "informal_also_false": None,
            }
        )


_THM = ExternalPair(
    "t1",
    "theorem",
    "Every natural number greater than 2 is not equal to 2.",
    "theorem t (n : ℕ) (h : 2 < n) : n ≠ 2",
)


def test_both_judges_faithful_passes_screening() -> None:
    s = score_pair(_THM, judge_a=_FakeAid(), judge_b=_FakeAid(), verifier=None, prober=None)
    assert s.outcome == "passed_screening"
    assert s.reasons == []


def test_single_judge_mismatch_flags() -> None:
    s = score_pair(
        _THM,
        judge_a=_FakeAid("mismatch", "dropped hypothesis"),
        judge_b=_FakeAid(),
        verifier=None,
        prober=None,
    )
    assert s.outcome == "flagged"
    assert s.reasons == ["judge-a-mismatch"]
    assert s.judge_a_issues == "dropped hypothesis"


def test_unclear_routes_to_human() -> None:
    s = score_pair(
        _THM, judge_a=_FakeAid(), judge_b=_FakeAid("unclear"), verifier=None, prober=None
    )
    assert s.outcome == "needs_human_review"


def test_multidecl_routes_to_human_without_paying_judges() -> None:
    ja, jb = _FakeAid(), _FakeAid()
    pair = ExternalPair("v1", "theorem", "Two claims.", "theorem a : True\ntheorem b : True")
    s = score_pair(pair, judge_a=ja, judge_b=jb, verifier=None, prober=None)
    assert s.outcome == "needs_human_review"
    assert s.reasons == ["deterministic-vacuous:multiple-declarations"]
    assert ja.calls == 0 and jb.calls == 0


def test_lean_elaboration_failure_is_not_a_flag() -> None:
    s = score_pair(
        _THM,
        judge_a=_FakeAid(),
        judge_b=_FakeAid(),
        verifier=_FakeVerifier("invalid", "foo.lean:1:0: error: unknown identifier 'zeta'"),
        prober=None,
    )
    assert s.lean_status == "did_not_elaborate_in_our_env"
    assert "unknown identifier" in s.lean_detail
    assert s.outcome == "passed_screening"  # env mismatch alone never flags
    s2 = score_pair(
        _THM, judge_a=_FakeAid(), judge_b=_FakeAid(), verifier=_FakeVerifier("valid"), prober=None
    )
    assert s2.lean_status == "valid_in_our_env"


def test_probe_falsification_flags_with_counterexample() -> None:
    s = score_pair(
        _THM, judge_a=_FakeAid(), judge_b=_FakeAid(), verifier=None, prober=_FakeProber(True)
    )
    assert s.outcome == "flagged"
    assert "falsified-by-counterexample" in s.reasons
    assert s.probe_counterexample.startswith("n := 0")


def test_no_judges_runs_the_free_deterministic_screen() -> None:
    s = score_pair(_THM, verifier=None, prober=None)
    assert s.outcome == "passed_screening"
    assert (s.judge_a_verdict, s.judge_b_verdict) == ("", "")


def test_no_judges_still_flags_deterministic_vacuity() -> None:
    pair = ExternalPair(
        "v9", "theorem", "Some n equals itself.", "theorem foo : ∃ n : ℕ, n = n := sorry"
    )
    s = score_pair(pair, verifier=None, prober=None)
    assert s.outcome == "flagged"
    assert s.reasons == ["deterministic-vacuous:reflexive-goal"]


def test_exactly_one_judge_is_an_input_error() -> None:
    with pytest.raises(ValueError, match="both judges or neither"):
        score_pair(_THM, judge_a=_FakeAid(), verifier=None, prober=None)
    with pytest.raises(ValueError, match="both judges or neither"):
        score_pair(_THM, judge_b=_FakeAid(), verifier=None, prober=None)


def test_confirm_flags_keeps_a_reproducing_flag() -> None:
    ja, jb = _FakeAid("mismatch"), _FakeAid()
    s = score_pair(_THM, judge_a=ja, judge_b=jb, verifier=None, prober=None, confirm_flags=True)
    assert s.outcome == "flagged"
    assert "flag-confirmed" in s.reasons
    assert ja.calls == 2  # original + confirmation pass


def test_confirm_flags_routes_a_one_off_flip_to_human() -> None:
    s = score_pair(
        _THM,
        judge_a=_FlippingAid(),
        judge_b=_FakeAid(),
        verifier=None,
        prober=None,
        confirm_flags=True,
    )
    assert s.outcome == "needs_human_review"
    assert "unconfirmed-judge-flag" in s.reasons


def test_statement_only_lean_gets_proof_stub() -> None:
    from leanscreen.verify import ensure_proof_stub

    assert ensure_proof_stub("theorem t : 1 = 1 :=").endswith(":= sorry")
    assert ensure_proof_stub("theorem t : 1 = 1 := by").endswith(":= by sorry")
    already = "theorem t : 1 = 1 := sorry"
    assert ensure_proof_stub(already) == already
    proved = "theorem t : 1 = 1 := rfl"
    assert ensure_proof_stub(proved) == proved
    # The in-loop drafting shape: no `:=` at all.
    assert ensure_proof_stub("theorem t : 1 = 1") == "theorem t : 1 = 1 := sorry"
    assert ensure_proof_stub("example : True") == "example : True := sorry"
    # A bodiless def is incomplete, not a statement awaiting proof — untouched.
    bodiless = "def c : ℕ"
    assert ensure_proof_stub(bodiless) == bodiless
    # A binder default's `:=` counts as a `:=` — no stub, same as before.
    optparam = "theorem t (n : ℕ := 1) : n = n"
    assert ensure_proof_stub(optparam) == optparam
