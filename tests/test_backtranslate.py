"""Unit tests for the back-translation review aid (parsing + client wiring)."""

from __future__ import annotations

from leanscreen.backtranslate import (
    BackTranslator,
    build_user_prompt,
    parse_review,
)


class _FakeClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.seen_system = ""
        self.seen_user = ""

    def complete(
        self,
        system: str,
        user: str,
        temperature: float | None = None,
        *,
        cache_prefix: str | None = None,
    ) -> str:
        self.seen_system = system
        self.seen_user = user
        return self.response


def test_parse_well_formed_response() -> None:
    resp = (
        "BACK-TRANSLATION: For every prime p, the map is surjective.\n"
        "VERDICT: MISMATCH\n"
        "ISSUES: the Lean drops the hypothesis p > 3"
    )
    review = parse_review(resp)
    assert review.back_english == "For every prime p, the map is surjective."
    assert review.verdict == "mismatch"
    assert "drops the hypothesis" in review.issues


def test_parse_faithful_verdict() -> None:
    review = parse_review(
        "BACK-TRANSLATION: d(n) is little-o of n^e.\nVERDICT: FAITHFUL\nISSUES: none"
    )
    assert review.verdict == "faithful"
    assert review.issues == "none"


def test_parse_unknown_verdict_degrades_to_unclear() -> None:
    # A malformed / missing verdict must not crash; default to unclear.
    review = parse_review("the model rambled without the format")
    assert review.verdict == "unclear"
    assert review.back_english  # falls back to the raw text


def test_translator_passes_both_statements_to_client() -> None:
    client = _FakeClient("BACK-TRANSLATION: x.\nVERDICT: FAITHFUL\nISSUES: none")
    review = BackTranslator(client).review("informal here", "theorem t : True := by sorry")
    assert review.verdict == "faithful"
    assert "informal here" in client.seen_user
    assert "theorem t" in client.seen_user


def test_user_prompt_contains_both() -> None:
    prompt = build_user_prompt("the informal", "the lean")
    assert "the informal" in prompt
    assert "the lean" in prompt


def test_checklist_flag_selects_the_v2_prompt() -> None:
    from leanscreen import backtranslate as bt

    class _Capture:
        def __init__(self) -> None:
            self.system = ""

        def complete(self, system: str, user: str, temperature=None, *, cache_prefix=None) -> str:
            self.system = system
            return "BACK-TRANSLATION: x\nVERDICT: FAITHFUL\nISSUES: none"

    v1_client, v2_client = _Capture(), _Capture()
    bt.BackTranslator(v1_client).review("inf", "lean")
    bt.BackTranslator(v2_client, checklist=True).review("inf", "lean")
    assert v1_client.system == bt._SYSTEM_PROMPT
    assert v2_client.system == bt._SYSTEM_PROMPT_CHECKLIST
    assert "auditing EACH hypothesis" in v2_client.system
    # Same response contract: both prompts demand the identical output format.
    for sys_prompt in (v1_client.system, v2_client.system):
        assert "BACK-TRANSLATION:" in sys_prompt
        assert "VERDICT: FAITHFUL | MISMATCH | UNCLEAR" in sys_prompt
