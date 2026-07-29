"""The one-shot CLI: dispatch, input shapes, exit codes, output modes, and
the --deep budget cap — all without a network, a Lean toolchain, or a key."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import leanscreen.server as server_mod
from leanscreen.backtranslate import BackReview
from leanscreen.cli import build_parser, main
from leanscreen.config import Settings
from leanscreen.server import DeepStack, LeanState, ScreenerRuntime


class _Aid:
    def __init__(self, verdict: str = "faithful") -> None:
        self.verdict = verdict
        self.calls = 0

    def review(self, informal: str, lean_code: str) -> BackReview:
        self.calls += 1
        return BackReview(back_english="r", verdict=self.verdict, issues="", raw="")


class _Prober:
    def complete(self, system: str, user: str, temperature=None, *, cache_prefix=None) -> str:  # type: ignore[no-untyped-def]
        return json.dumps(
            {"falsified": False, "counterexample": "", "check": "", "informal_also_false": None}
        )


def _lean_off() -> LeanState:
    state = LeanState(error="verifier unavailable: no project configured")
    state.ready.set()
    return state


def _no_paid_stack() -> DeepStack:
    raise AssertionError("the fast path must never build the paid stack")


def _fast_factory(settings: Settings) -> ScreenerRuntime:
    return ScreenerRuntime(settings, _lean_off(), _no_paid_stack)


def _deep_factory(costs: list[float], verdict: str = "faithful"):  # type: ignore[no-untyped-def]
    """A runtime whose deep stack reports the given cumulative spend readings
    (check_deep reads spend() twice per call: before and after)."""

    def factory(settings: Settings) -> ScreenerRuntime:
        it = iter(costs)
        stack = DeepStack(
            judge_a_theorem=_Aid(verdict),
            judge_b_theorem=_Aid(verdict),
            judge_a_definition=_Aid(),
            judge_b_definition=_Aid(),
            prober=_Prober(),
            spend=lambda: next(it),
        )
        return ScreenerRuntime(settings, _lean_off(), lambda: stack)

    return factory


_CLEAN = ["--informal", "n > 2 implies n ≠ 2.", "--lean", "theorem t (n : ℕ) (h : 2 < n) : n ≠ 2"]
_TRIVIAL = [
    "--informal",
    "Some n equals itself.",
    "--lean",
    "theorem foo : ∃ n : ℕ, n = n := sorry",
]


# -------------------------------------------------------------------- dispatch


def test_bare_invocation_still_runs_the_mcp_server(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server_mod, "_runtime", lambda: calls.append("runtime"))
    monkeypatch.setattr(server_mod._MCP, "run", lambda: calls.append("run"))
    monkeypatch.setattr(sys, "argv", ["leanscreen"])
    server_mod.main()
    assert calls == ["runtime", "run"]


def test_any_argument_dispatches_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["leanscreen", "check"])  # no input: usage error
    with pytest.raises(SystemExit) as excinfo:
        server_mod.main()
    assert excinfo.value.code == 2


def test_check_help_documents_the_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["check", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "exit codes" in out
    assert "NOT a certification" in out


# ------------------------------------------------------------------- fast path


def test_inline_rejected_pair_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["check", *_TRIVIAL], runtime_factory=_fast_factory)
    assert code == 1
    out = capsys.readouterr().out
    assert "REJECTED" in out
    assert "deterministic-vacuous:reflexive-goal" in out
    assert "[deterministic]" in out
    assert "1 rejected" in out


def test_inline_clean_pair_exits_0_and_reports_no_defect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["check", *_CLEAN], runtime_factory=_fast_factory)
    assert code == 0
    out = capsys.readouterr().out
    assert "no defect found" in out
    assert "not a certification" in out
    assert "certified" not in out.lower()


def test_json_mode_emits_full_payloads_on_stdout_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["check", "--json", *_TRIVIAL], runtime_factory=_fast_factory)
    assert code == 1
    captured = capsys.readouterr()
    (line,) = captured.out.splitlines()  # stdout is payloads only
    payload = json.loads(line)
    assert payload["outcome"] == "flagged"
    assert payload["name"] == "pair"
    assert "NOT CERTIFICATION" in payload["not_certification"]
    assert "calibration" in payload
    assert "screened 1 pair(s)" in captured.err  # the summary moves to stderr


def test_jsonl_file_screens_each_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text(
        json.dumps({"informal": "Some n equals itself.", "lean": "theorem foo : ∃ n : ℕ, n = n"})
        + "\n"
        + json.dumps(
            {
                "name": "fine",
                "informal": "n > 2 implies n ≠ 2.",
                "lean": "theorem t (n : ℕ) (h : 2 < n) : n ≠ 2",
                "kind": "theorem",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    code = main(["check", str(pairs)], runtime_factory=_fast_factory)
    assert code == 1
    out = capsys.readouterr().out
    assert "line 1: REJECTED" in out
    assert "fine: no defect found" in out
    assert "screened 2 pair(s): 1 rejected" in out


def test_lean_file_screens_documented_declarations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "Demo.lean"
    source.write_text(
        "/-- Some n equals itself. -/\n"
        "theorem refl_goal : ∃ n : ℕ, n = n := ⟨0, rfl⟩\n"
        "\n"
        "theorem undocumented : True := trivial\n",
        encoding="utf-8",
    )
    code = main(["check", str(source)], runtime_factory=_fast_factory)
    assert code == 1
    captured = capsys.readouterr()
    assert "refl_goal: REJECTED" in captured.out
    assert "1 declaration(s) skipped: no docstring" in captured.err


# ---------------------------------------------------------------- usage errors


@pytest.mark.parametrize(
    "argv",
    [
        ["check"],  # nothing to screen
        ["check", "--informal", "only half a pair"],
        ["check", "--informal", "   ", "--lean", "theorem t : True"],
        ["check", "--budget", "1.0", *_CLEAN],  # --budget without --deep
        ["check", "--deep", "--budget", "0", *_CLEAN],
        ["check", "no-such-file.jsonl"],
    ],
)
def test_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv, runtime_factory=_fast_factory) == 2
    assert "error" in capsys.readouterr().err


def test_both_file_and_inline_pair_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text("{}\n", encoding="utf-8")
    assert main(["check", str(pairs), *_CLEAN], runtime_factory=_fast_factory) == 2
    assert "not both" in capsys.readouterr().err


def test_malformed_jsonl_reports_the_line_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text('{"informal": "x", "lean": "theorem t : True"}\nnot json\n', encoding="utf-8")
    assert main(["check", str(pairs)], runtime_factory=_fast_factory) == 2
    assert f"{pairs}:2" in capsys.readouterr().err


def test_lean_file_with_no_documented_declarations_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "Bare.lean"
    source.write_text("theorem undocumented : True := trivial\n", encoding="utf-8")
    assert main(["check", str(source)], runtime_factory=_fast_factory) == 2
    err = capsys.readouterr().err
    assert "no docstring-paired declarations" in err


# ----------------------------------------------------------------------- deep


def test_deep_without_api_key_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = main(["check", "--deep", *_CLEAN], runtime_factory=_deep_factory([0.0, 0.2]))
    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_deep_screens_and_reports_actual_cost(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    code = main(["check", "--deep", "--json", *_CLEAN], runtime_factory=_deep_factory([0.0, 0.21]))
    assert code == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["actual_cost_usd"] == 0.21


def test_deep_budget_stops_before_the_call_that_would_exceed_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    pairs = tmp_path / "pairs.jsonl"
    obj = json.dumps(
        {"informal": "n > 2 implies n ≠ 2.", "lean": "theorem t (n : ℕ) (h : 2 < n) : n ≠ 2"}
    )
    pairs.write_text(obj + "\n" + obj + "\n" + obj + "\n", encoding="utf-8")
    # First call costs $0.30; the second would take the max-observed estimate
    # ($0.30) past the $0.50 budget, so pairs 2 and 3 are never screened.
    code = main(
        ["check", "--deep", "--budget", "0.50", str(pairs)],
        runtime_factory=_deep_factory([0.0, 0.30]),
    )
    assert code == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 2  # one verdict line + the summary
    assert "2 pair(s) NOT screened" in captured.err
    assert "2 not screened (budget)" in captured.out


def test_budget_smaller_than_one_call_screens_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    code = main(
        ["check", "--deep", "--budget", "0.10", *_CLEAN],
        runtime_factory=_deep_factory([]),  # spend() must never be consulted
    )
    assert code == 0
    assert "1 pair(s) NOT screened" in capsys.readouterr().err
