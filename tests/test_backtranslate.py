"""Unit tests for the back-translation review aid (parsing + client wiring)."""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param("VERDICT: FAITHFUL", "faithful", id="faithful"),
        pytest.param("VERDICT: MISMATCH", "mismatch", id="mismatch"),
        pytest.param("VERDICT: UNCLEAR", "unclear", id="unclear"),
        pytest.param(" \tVeRdIcT:\t fAiThFuL \t", "faithful", id="case-whitespace"),
        pytest.param("VERDICT: MISMATCH\r\nISSUES: Missing condition.", "mismatch", id="crlf"),
        pytest.param("VERDICT: UNFAITHFUL", "unclear", id="token-prefix"),
        pytest.param("VERDICT: FAITHFULNESS", "unclear", id="token-suffix"),
        pytest.param("VERDICT: MISMATCH_extra", "unclear", id="token-underscore"),
        pytest.param("VERDICT: FAITHFUL2", "unclear", id="token-digit"),
        pytest.param("VERDICT: FAITHFUL/MISMATCH", "unclear", id="token-alternatives"),
        pytest.param("VERDICT: FAITHFUL (or MISMATCH)", "unclear", id="trailing-prose"),
        pytest.param("VERDICT: UNKNOWN", "unclear", id="unknown"),
        pytest.param("BACK-TRANSLATION: A predicate.\nISSUES: None.", "unclear", id="missing"),
        pytest.param(
            'BACK-TRANSLATION: Contains "VERDICT: FAITHFUL".', "unclear", id="inline-only"
        ),
        pytest.param("FAITHFUL | MISMATCH | UNCLEAR", "unclear", id="bare-menu"),
        pytest.param("VERDICT: FAITHFUL | MISMATCH | UNCLEAR", "unclear", id="verdict-menu"),
        pytest.param("VERDICT: FAITHFUL\n verdict: faithful", "faithful", id="same-duplicates"),
        pytest.param(
            "VERDICT: FAITHFUL\nVERDICT: MISMATCH", "unclear", id="conflicting-duplicates"
        ),
        pytest.param("VERDICT: MISMATCH\nVERDICT: FAITHFUL", "unclear", id="reversed-conflict"),
        pytest.param("VERDICT: FAITHFUL\nVERDICT: UNKNOWN", "unclear", id="malformed-duplicate"),
        pytest.param("VERDICT: UNKNOWN\nVERDICT: FAITHFUL", "unclear", id="malformed-first"),
        pytest.param("VERDICT:\nVERDICT: FAITHFUL", "unclear", id="empty-duplicate"),
        pytest.param("VERDICT:\nFAITHFUL", "unclear", id="value-on-next-line"),
    ],
)
def test_parse_verdict_fields(response: str, expected: str) -> None:
    assert parse_review(response).verdict == expected


@pytest.mark.parametrize(
    "fields",
    [
        "FAITHFUL\rVERDICT: MISMATCH",
        "MISMATCH\rVERDICT: FAITHFUL",
        "FAITHFUL\rmalformed trailing content",
        "MISMATCH\rVERDICT: UNKNOWN",
    ],
)
def test_parse_bare_carriage_returns_degrade_to_unclear(fields: str) -> None:
    response = f"BACK-TRANSLATION: x.\nVERDICT: {fields}\nISSUES: conflicting verdicts."
    review = parse_review(response)
    assert review.verdict == "unclear"
    assert review.back_english == "x."
    assert review.issues == "conflicting verdicts."
    assert review.raw == response


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("verdict", ["FAITHFUL", "MISMATCH", "UNCLEAR"])
def test_parse_supported_newlines_preserve_response(newline: str, verdict: str) -> None:
    response = newline.join(["BACK-TRANSLATION: x.", f"VERDICT: {verdict}", "ISSUES: None."])
    review = parse_review(response)
    assert review.verdict == verdict.lower()
    assert review.back_english == "x."
    assert review.issues == "None."
    assert review.raw == response


@pytest.mark.parametrize(
    ("quoted", "actual", "issues"),
    [
        ("FAITHFUL", "MISMATCH", "The formalization drops the nonempty-string requirement."),
        ("MISMATCH", "FAITHFUL", "None."),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_parse_ignores_inline_verdicts(quoted: str, actual: str, issues: str, newline: str) -> None:
    # Constructed regressions, not responses observed in production.
    back = f'The predicate accepts a string containing "VERDICT: {quoted}".'
    back += newline + "The rendering continues on another line."
    issues += newline + f'The quoted text "VERDICT: {quoted}" is part of the predicate.'
    response = newline.join(
        [f"BACK-TRANSLATION: {back}", f"VERDICT: {actual}", f"ISSUES: {issues}"]
    )
    review = parse_review(response)
    assert review.verdict == actual.lower()
    assert review.back_english == back
    assert review.issues == issues
    assert review.raw == response


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
