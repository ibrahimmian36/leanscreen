"""One-shot command line: ``leanscreen check``.

The bare ``leanscreen`` command stays the MCP stdio server (that invocation
is baked into every client config); any argument routes here instead. The
``check`` subcommand screens pairs once and exits with a CI-friendly code:

* ``0``: nothing rejected (a pass means "no defect found by this harness",
  never a certification of faithfulness);
* ``1``: at least one pair rejected (reject-tier flags only; advisory lints
  never fail the run);
* ``2``: usage or configuration error.

Three input shapes: one inline pair (``--informal``/``--lean``), a JSONL
file of ``{informal, lean, kind?}`` objects (the MCP tools' schema), or a
``.lean`` file whose ``/-- ... -/`` doc comments are screened against the
declarations they document.

Human-readable verdict lines go to stdout by default; ``--json`` switches
stdout to one full payload object per line (everything else, meaning warm-up
status, skip notes, and the summary, goes to stderr so stdout stays parseable).
Scoring is the exact ``check_fast``/``check_deep`` path from the MCP server;
nothing is re-implemented here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanscreen.config import Settings
from leanscreen.errors import LeanScreenError
from leanscreen.leanfile import extract_documented
from leanscreen.logging import configure_logging
from leanscreen.server import (
    LeanState,
    ScreenerRuntime,
    _build_deep_stack,
    build_verifier,
    warm_verifier,
)

# Conservative per-pair estimate for the FIRST deep call, before any actual
# cost has been observed, seeded at the top of the measured 17-27 cent range. After the
# first call the largest observed per-pair cost replaces it.
_DEEP_COST_ESTIMATE_USD = 0.27

_EXIT_CODES_EPILOG = """\
exit codes:
  0  nothing rejected: no defect found by this harness (NOT a certification
     of faithfulness; this screen may only reject)
  1  at least one pair rejected (reject-tier flags only; advisory lints and
     needs-human-review outcomes do not fail the run)
  2  usage or configuration error
"""

RuntimeFactory = Callable[[Settings], ScreenerRuntime]


class _UsageError(Exception):
    """Bad invocation or unusable input file; exit code 2."""


@dataclass(frozen=True, slots=True)
class _CliPair:
    """One pair queued for screening, with the name the report will use."""

    name: str
    informal: str
    lean: str
    kind: str | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanscreen",
        description=(
            "Faithfulness screen for informal<->Lean 4 statement pairs. "
            "With no arguments, runs the MCP stdio server."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    check = sub.add_parser(
        "check",
        help="screen pairs once from the command line and exit",
        # Pre-wrapped: RawDescriptionHelpFormatter (needed for the epilog's
        # exit-code table) prints the description verbatim too.
        description=(
            "Screen informal<->Lean 4 pairs once and exit. Default is the free fast\n"
            "screen (lints + vacuity + Lean elaboration when configured); --deep adds\n"
            "two LLM judges under strict consensus and a counterexample probe, billed\n"
            "to your own ANTHROPIC_API_KEY. This screen may only reject: a pass means\n"
            "'no defect found by this harness', never a certification of faithfulness."
        ),
        epilog=_EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check.add_argument(
        "path",
        nargs="?",
        type=Path,
        help=(
            "a .jsonl file of {informal, lean, kind?} objects (one per line), or a "
            ".lean file whose /-- ... -/ docstrings are screened against the "
            "declarations they document"
        ),
    )
    check.add_argument("--informal", help="natural-language statement (requires --lean)")
    check.add_argument("--lean", help="Lean 4 statement (requires --informal)")
    check.add_argument(
        "--kind",
        choices=("theorem", "definition"),
        help="kind of the inline pair (default: inferred from the Lean head)",
    )
    check.add_argument(
        "--deep",
        action="store_true",
        help=(
            "run the paid deep screen (two LLM judges + counterexample probe, "
            "~17-27 cents per pair, 30-60s each; requires ANTHROPIC_API_KEY)"
        ),
    )
    check.add_argument(
        "--budget",
        type=float,
        metavar="USD",
        help=(
            "spend cap for --deep: stop before the call that would exceed it "
            "(remaining pairs are reported as not screened)"
        ),
    )
    check.add_argument(
        "--json",
        action="store_true",
        help="emit one full payload object per line on stdout instead of verdict lines",
    )
    return parser


def main(
    argv: Sequence[str] | None = None, *, runtime_factory: RuntimeFactory | None = None
) -> int:
    """Argparse entry point. ``runtime_factory`` is injected by tests so the
    whole flow runs without a Lean toolchain, a network, or an API key."""
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        pairs = _collect_pairs(args)
        _validate_deep_flags(args)
    except _UsageError as exc:
        print(f"leanscreen check: error: {exc}", file=sys.stderr)
        return 2
    runtime = (runtime_factory or _start_runtime)(Settings())
    try:
        payloads, budget_stopped = _screen(runtime, pairs, args)
    except LeanScreenError as exc:
        print(f"leanscreen check: error: {exc}", file=sys.stderr)
        return 2
    _summarize(payloads, skipped_by_budget=budget_stopped, json_mode=args.json)
    rejected = sum(1 for p in payloads if p["outcome"] == "flagged")
    return 1 if rejected else 0


def _validate_deep_flags(args: argparse.Namespace) -> None:
    if args.budget is not None and not args.deep:
        raise _UsageError("--budget only applies to --deep")
    if args.budget is not None and args.budget <= 0:
        raise _UsageError("--budget must be a positive dollar amount")
    if args.deep and not os.environ.get("ANTHROPIC_API_KEY"):
        raise _UsageError(
            "--deep needs your own API key: export ANTHROPIC_API_KEY and rerun "
            "(the deep screen bills the two judges and the probe to it, "
            "~17-27 cents per pair)"
        )


def _collect_pairs(args: argparse.Namespace) -> list[_CliPair]:
    inline = args.informal is not None or args.lean is not None
    if inline and args.path is not None:
        raise _UsageError("give either a file or --informal/--lean, not both")
    if inline:
        if not (args.informal or "").strip() or not (args.lean or "").strip():
            raise _UsageError("--informal and --lean are both required (and non-empty)")
        return [_CliPair(name="pair", informal=args.informal, lean=args.lean, kind=args.kind)]
    if args.path is None:
        raise _UsageError("nothing to screen: give a .jsonl/.lean file or --informal/--lean")
    if args.kind is not None:
        raise _UsageError("--kind only applies to an inline --informal/--lean pair")
    path: Path = args.path
    if not path.is_file():
        raise _UsageError(f"no such file: {path}")
    if path.suffix == ".jsonl":
        return _pairs_from_jsonl(path)
    if path.suffix == ".lean":
        return _pairs_from_lean(path)
    raise _UsageError(f"unsupported file type {path.suffix!r} (expected .jsonl or .lean)")


def _pairs_from_jsonl(path: Path) -> list[_CliPair]:
    pairs: list[_CliPair] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _UsageError(f"{path}:{lineno}: not valid JSON ({exc})") from exc
        if not isinstance(obj, dict):
            raise _UsageError(f"{path}:{lineno}: expected an object, got {type(obj).__name__}")
        informal, lean, kind = obj.get("informal"), obj.get("lean"), obj.get("kind")
        if not isinstance(informal, str) or not informal.strip():
            raise _UsageError(f"{path}:{lineno}: 'informal' must be a non-empty string")
        if not isinstance(lean, str) or not lean.strip():
            raise _UsageError(f"{path}:{lineno}: 'lean' must be a non-empty string")
        if kind is not None and kind not in ("theorem", "definition"):
            raise _UsageError(f"{path}:{lineno}: 'kind' must be 'theorem' or 'definition'")
        name = obj.get("name")
        pairs.append(
            _CliPair(
                name=name if isinstance(name, str) and name.strip() else f"line {lineno}",
                informal=informal,
                lean=lean,
                kind=kind,
            )
        )
    if not pairs:
        raise _UsageError(f"{path}: no pairs found")
    return pairs


def _pairs_from_lean(path: Path) -> list[_CliPair]:
    extraction = extract_documented(path.read_text(encoding="utf-8"))
    if extraction.skipped_no_docstring:
        print(
            f"leanscreen check: {extraction.skipped_no_docstring} declaration(s) "
            "skipped: no docstring",
            file=sys.stderr,
        )
    if extraction.skipped_unrecognized:
        print(
            f"leanscreen check: {extraction.skipped_unrecognized} doc comment(s) "
            "skipped: not followed by a recognized declaration",
            file=sys.stderr,
        )
    if not extraction.decls:
        raise _UsageError(f"{path}: no docstring-paired declarations found")
    return [
        _CliPair(name=d.name, informal=d.informal, lean=d.lean, kind=None) for d in extraction.decls
    ]


def _start_runtime(settings: Settings) -> ScreenerRuntime:
    """The real runtime, warmed SYNCHRONOUSLY: a one-shot run has no
    background to warm in, and stdout must stay clean for --json, so the
    only chatter is one status line on stderr."""
    lean = LeanState()
    try:
        verifier = build_verifier(settings)
    except (LeanScreenError, OSError, ValueError) as exc:
        lean.error = f"verifier unavailable: {exc}"
        lean.ready.set()
        print(
            f"leanscreen check: Lean elaboration off ({exc}); lint + vacuity screens only",
            file=sys.stderr,
        )
    else:
        print(
            "leanscreen check: warming the Lean REPL (mathlib import, ~100s on the first run)",
            file=sys.stderr,
        )
        warm_verifier(lean, verifier)
        if lean.error:
            print(f"leanscreen check: Lean elaboration off ({lean.error})", file=sys.stderr)
    return ScreenerRuntime(settings, lean, lambda: _build_deep_stack(settings))


def _screen(
    runtime: ScreenerRuntime, pairs: list[_CliPair], args: argparse.Namespace
) -> tuple[list[dict[str, Any]], int]:
    """Screen every pair, honoring the --deep budget. Returns the payloads
    (with ``name`` attached) and how many pairs the budget cut off."""
    payloads: list[dict[str, Any]] = []
    spent = 0.0
    largest_observed = 0.0
    for index, pair in enumerate(pairs):
        if args.deep:
            estimate = largest_observed or _DEEP_COST_ESTIMATE_USD
            if args.budget is not None and spent + estimate > args.budget:
                remaining = len(pairs) - index
                print(
                    f"leanscreen check: stopping before the budget is exceeded "
                    f"(spent ${spent:.2f} of ${args.budget:.2f}, next call estimated "
                    f"${estimate:.2f}); {remaining} pair(s) NOT screened",
                    file=sys.stderr,
                )
                return payloads, remaining
            payload = runtime.check_deep(pair.informal, pair.lean, pair.kind)
            cost = float(payload.get("actual_cost_usd") or 0.0)
            spent += cost
            largest_observed = max(largest_observed, cost)
        else:
            payload = runtime.check_fast(pair.informal, pair.lean, pair.kind)
        payload["name"] = pair.name
        payloads.append(payload)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(_verdict_line(payload))
    return payloads, 0


def _verdict_line(payload: dict[str, Any]) -> str:
    outcome = str(payload["outcome"])
    label = {
        "flagged": "REJECTED",
        "needs_human_review": "NEEDS HUMAN REVIEW",
        "passed_screening": "no defect found",
    }.get(outcome, outcome)
    parts = [f"{payload['name']}: {label}", f"lean={payload['lean_status']}"]
    if payload["reasons"]:
        parts.append(f"flags={','.join(payload['reasons'])} [{payload['evidence_tier']}]")
    if payload["lints"]:
        parts.append(f"advisory-lints={','.join(payload['lints'])}")
    if "actual_cost_usd" in payload:
        parts.append(f"cost=${payload['actual_cost_usd']:.2f}")
    return "  ".join(parts)


def _summarize(payloads: list[dict[str, Any]], *, skipped_by_budget: int, json_mode: bool) -> None:
    counts = Counter(str(p["outcome"]) for p in payloads)
    summary = (
        f"screened {len(payloads)} pair(s): {counts['flagged']} rejected, "
        f"{counts['needs_human_review']} needs human review, "
        f"{counts['passed_screening']} passed screening "
        "(no defect found, not a certification)"
    )
    if skipped_by_budget:
        summary += f"; {skipped_by_budget} not screened (budget)"
    # In --json mode stdout carries payload objects ONLY.
    print(summary, file=sys.stderr if json_mode else sys.stdout)
