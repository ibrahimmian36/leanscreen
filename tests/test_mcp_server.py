"""The MCP screening server: warm-up degradation, tool payloads, the
zero-network property of check_fast, and the non-certification disclosures."""

from __future__ import annotations

import json

import pytest

from leanscreen.backtranslate import BackReview
from leanscreen.config import Settings
from leanscreen.score import CALIBRATION_DISCLOSURE
from leanscreen.server import (
    DeepStack,
    LeanState,
    ScreenerRuntime,
    check_deep,
    check_fast,
)
from leanscreen.verify import VerifyResult


class _Aid:
    def __init__(self, verdict: str = "faithful", issues: str = "") -> None:
        self.verdict = verdict
        self.issues = issues
        self.calls = 0

    def review(self, informal: str, lean_code: str) -> BackReview:
        self.calls += 1
        return BackReview(back_english="r", verdict=self.verdict, issues=self.issues, raw="")


class _Verifier:
    def __init__(self, status: str = "valid") -> None:
        self._status = status

    def verify(self, lean_code: str) -> VerifyResult:
        return VerifyResult(status=self._status, output="", seconds=0.0)  # type: ignore[arg-type]

    def verify_batch(self, drafts: list[str]) -> list[VerifyResult]:
        return [self.verify(d) for d in drafts]


class _Prober:
    def __init__(self, falsified: bool = False) -> None:
        self._falsified = falsified

    def complete(self, system: str, user: str, temperature=None, *, cache_prefix=None) -> str:  # type: ignore[no-untyped-def]
        return json.dumps(
            {
                "falsified": self._falsified,
                "counterexample": "n := 0" if self._falsified else "",
                "check": "hypotheses hold; conclusion fails" if self._falsified else "",
                "informal_also_false": None,
            }
        )


class _ForbiddenProber:
    """A prober that must never be consulted (definition-kind pairs)."""

    def complete(self, system: str, user: str, temperature=None, *, cache_prefix=None) -> str:  # type: ignore[no-untyped-def]
        raise AssertionError("prober must not run on a definition")


def _no_paid_stack() -> DeepStack:
    raise AssertionError("check_fast must never build the paid stack")


def _lean_off(reason: str = "verifier unavailable: no project configured") -> LeanState:
    state = LeanState(error=reason)
    state.ready.set()
    return state


def _lean_warm(status: str = "valid") -> LeanState:
    state = LeanState(verifier=_Verifier(status))
    state.ready.set()
    return state


def _runtime(lean: LeanState, deep_factory=None) -> ScreenerRuntime:  # type: ignore[no-untyped-def]
    return ScreenerRuntime(Settings(), lean, deep_factory or _no_paid_stack)


def _stack(ja: _Aid, jb: _Aid, jad: _Aid, jbd: _Aid, prober, costs: list[float]) -> DeepStack:  # type: ignore[no-untyped-def]
    it = iter(costs)
    return DeepStack(
        judge_a_theorem=ja,
        judge_b_theorem=jb,
        judge_a_definition=jad,
        judge_b_definition=jbd,
        prober=prober,
        spend=lambda: next(it),
    )


_THM_INFORMAL = "Every natural number greater than 2 is not equal to 2."
_THM_LEAN = "theorem t (n : ℕ) (h : 2 < n) : n ≠ 2"


# ---------------------------------------------------------------- check_fast


def test_fast_flags_trivial_statement_without_lean_judges_or_network() -> None:
    out = _runtime(_lean_off()).check_fast(
        "Some n equals itself.", "theorem foo : ∃ n : ℕ, n = n := sorry"
    )
    assert out["outcome"] == "flagged"
    assert out["reasons"] == ["deterministic-vacuous:reflexive-goal"]
    assert out["evidence_tier"] == "deterministic"
    assert (out["judge_a_verdict"], out["judge_b_verdict"]) == ("", "")


def test_fast_reports_lean_unavailable_plainly() -> None:
    out = _runtime(_lean_off("verifier unavailable: no project configured")).check_fast(
        _THM_INFORMAL, _THM_LEAN
    )
    assert out["lean_status"] == "not_checked"
    assert any("no project configured" in note for note in out["checks_skipped"])
    assert out["outcome"] == "passed_screening"


def test_fast_during_warm_up_answers_with_warming_note() -> None:
    warming = LeanState(verifier=None)  # ready never set: mathlib still importing
    out = _runtime(warming).check_fast(_THM_INFORMAL, _THM_LEAN)
    assert out["lean_status"] == "not_checked"
    assert any("still warming" in note for note in out["checks_skipped"])


def test_fast_uses_the_warm_verifier() -> None:
    out = _runtime(_lean_warm("valid")).check_fast(_THM_INFORMAL, _THM_LEAN)
    assert out["lean_status"] == "valid_in_our_env"
    out2 = _runtime(_lean_warm("invalid")).check_fast(_THM_INFORMAL, _THM_LEAN)
    assert out2["lean_status"] == "did_not_elaborate_in_our_env"
    assert out2["outcome"] == "passed_screening"  # env mismatch alone never flags


def test_fast_payload_carries_the_disclosures() -> None:
    out = _runtime(_lean_off()).check_fast(_THM_INFORMAL, _THM_LEAN)
    assert out["calibration"] == CALIBRATION_DISCLOSURE
    assert "NOT CERTIFICATION" in out["not_certification"]
    assert any("check_deep" in note for note in out["checks_skipped"])


def test_fast_rejects_bad_inputs() -> None:
    rt = _runtime(_lean_off())
    with pytest.raises(ValueError, match="kind"):
        rt.check_fast(_THM_INFORMAL, _THM_LEAN, kind="lemma")
    with pytest.raises(ValueError, match="informal"):
        rt.check_fast("   ", _THM_LEAN)
    with pytest.raises(ValueError, match="lean"):
        rt.check_fast(_THM_INFORMAL, "")


# ---------------------------------------------------------------- check_deep


def test_deep_runs_theorem_judges_probe_and_reports_actual_cost() -> None:
    ja, jb = _Aid(), _Aid("mismatch", "dropped hypothesis")
    jad, jbd = _Aid(), _Aid()
    stack = _stack(ja, jb, jad, jbd, _Prober(falsified=False), costs=[0.0, 0.21])
    out = _runtime(_lean_off(), lambda: stack).check_deep(_THM_INFORMAL, _THM_LEAN)
    assert out["outcome"] == "flagged"
    assert out["reasons"] == ["judge-b-mismatch"]
    assert out["evidence_tier"] == "single-judge"
    assert out["probe_falsified"] is False
    assert out["actual_cost_usd"] == 0.21
    assert (ja.calls, jb.calls, jad.calls, jbd.calls) == (1, 1, 0, 0)


def test_deep_two_judge_consensus_outranks_single_judge() -> None:
    stack = _stack(_Aid("mismatch"), _Aid("mismatch"), _Aid(), _Aid(), _Prober(), costs=[0.0, 0.2])
    out = _runtime(_lean_off(), lambda: stack).check_deep(_THM_INFORMAL, _THM_LEAN)
    assert out["evidence_tier"] == "two-judge-consensus"


def test_deep_counterexample_is_the_strongest_tier() -> None:
    stack = _stack(
        _Aid("mismatch"), _Aid("mismatch"), _Aid(), _Aid(), _Prober(True), costs=[0.0, 0.25]
    )
    out = _runtime(_lean_off(), lambda: stack).check_deep(_THM_INFORMAL, _THM_LEAN)
    assert out["evidence_tier"] == "counterexample"
    assert "falsified-by-counterexample" in out["reasons"]


def test_deep_definition_uses_definition_judges_and_skips_probe() -> None:
    ja, jb = _Aid(), _Aid()
    jad, jbd = _Aid(), _Aid("mismatch")
    stack = _stack(ja, jb, jad, jbd, _ForbiddenProber(), costs=[0.0, 0.05])
    out = _runtime(_lean_off(), lambda: stack).check_deep("A def.", "def c : ℕ := 0")
    assert out["kind"] == "definition"
    assert out["reasons"] == ["judge-b-mismatch"]
    assert (ja.calls, jb.calls, jad.calls, jbd.calls) == (0, 0, 1, 1)
    assert any("theorem-shaped" in note for note in out["checks_skipped"])


def test_deep_builds_the_paid_stack_exactly_once() -> None:
    built = {"n": 0}

    def factory() -> DeepStack:
        built["n"] += 1
        return _stack(_Aid(), _Aid(), _Aid(), _Aid(), _Prober(), costs=[0.0, 0.1, 0.1, 0.2])

    rt = _runtime(_lean_off(), factory)
    rt.check_deep(_THM_INFORMAL, _THM_LEAN)
    rt.check_deep(_THM_INFORMAL, _THM_LEAN)
    assert built["n"] == 1


# ---------------------------------------------------------------- disclosures


def test_tool_descriptions_state_passing_is_not_certification() -> None:
    for tool in (check_fast, check_deep):
        doc = (tool.__doc__ or "").lower()
        assert "may only reject" in doc
        assert "not" in doc and "certification" in doc
