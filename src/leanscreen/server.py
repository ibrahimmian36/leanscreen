"""MCP server exposing the Lean faithfulness screen as two in-loop tools.

``check_fast`` — the free deterministic screen: lints, vacuity checks, and
(when a Lean+mathlib REPL is configured) elaboration. No API calls, no key,
~0.1s per statement once the REPL is warm. For constant use while drafting.

``check_deep`` — everything in ``check_fast`` plus two independent LLM judges
under strict consensus and an adversarial counterexample probe. Paid
(~17–27¢/statement measured; the response reports actual spend) and slow
(30–60s). For deliberate use before something ships.

The standing invariant: the screen may only REJECT. A ``passed_screening``
outcome means "no defect found by this harness", never "this statement is
faithful" — every payload and both tool descriptions say so, and carry the
measured calibration verbatim.

Startup warms the Lean REPL in the background (mathlib import is ~100s once,
then ~0.1s/check). A ``check_fast`` arriving mid-warm-up returns lint +
vacuity results immediately with a "still warming" note instead of blocking;
``check_deep`` waits (bounded) since it is already a 30–60s call.
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from leanscreen.backtranslate import BackTranslator, ReviewAid
from leanscreen.config import Settings
from leanscreen.errors import ConfigError, LeanScreenError, VerifierError
from leanscreen.lean_server import ReplVerifier
from leanscreen.llm import AnthropicClient, LLMClient
from leanscreen.logging import configure_logging, get_logger
from leanscreen.score import (
    CALIBRATION_DISCLOSURE,
    ExternalPair,
    PairScore,
    infer_kind,
    score_pair,
)
from leanscreen.verify import LeanVerifier, Verifier, VerifierStatus

_log = get_logger(__name__)

# One trivial statement proves the REPL imported mathlib and answers checks.
_WARM_STATEMENT = "example : True := by trivial"

# check_deep is already a 30–60s call, so waiting out most of a cold mathlib
# import (~100s) is better than silently skipping elaboration.
_DEEP_WARM_WAIT_SECONDS = 240.0

_NOT_CERTIFICATION = (
    "PASSING IS NOT CERTIFICATION. This screen may only reject: "
    "'passed_screening' means no defect was found by this harness, NOT that "
    "the statement is faithful. Every flag is a candidate requiring human "
    "confirmation, not a verdict — on one PutnamBench sample our own "
    "counterexample probe was wrong 4 times out of 5."
)

_EVIDENCE_TIER_NOTE = (
    "Evidence tiers, strongest first: counterexample (the statement is false "
    "as written) > deterministic (a mechanical vacuity/lint proof) > "
    "two-judge-consensus > single-judge. A single-judge flag is below this "
    "project's own reporting bar — weigh it accordingly."
)


def build_verifier(settings: Settings) -> Verifier:
    """The REPL verifier when configured, else the subprocess fallback."""
    if settings.lean_project_path is None:
        raise ConfigError(
            "LEANSCREEN_LEAN_PROJECT_PATH must point at your Lean 4 + mathlib project"
        )
    if settings.lean_repl_path is not None:
        return ReplVerifier(
            settings.lean_project_path,
            settings.lean_repl_path,
            timeout_seconds=settings.lean_timeout_seconds,
        )
    return LeanVerifier(settings.lean_project_path, timeout_seconds=settings.lean_timeout_seconds)


@dataclass
class LeanState:
    """The Lean verifier and where it is in its lifecycle.

    ``error`` non-empty means elaboration is off for this server's lifetime
    (no project configured, build failed, warm-up failed) — the message is
    surfaced verbatim to callers. ``ready`` is set once warm-up finishes,
    successfully or not; ``verifier`` is non-None only after a VALID warm-up.
    """

    verifier: Verifier | None = None
    error: str = ""
    ready: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.monotonic)


def warm_verifier(lean: LeanState, verifier: Verifier) -> None:
    """Run the one-statement warm-up and mark ``lean`` ready, pass or fail.

    Shared by the MCP server (called on a background thread so startup does
    not block on the ~100s mathlib import) and the one-shot CLI (called
    synchronously, so the first check never races the warm-up).
    """
    try:
        result = verifier.verify(_WARM_STATEMENT)
    except VerifierError as exc:
        lean.error = f"verifier failed to warm: {exc}"
    else:
        if result.status is VerifierStatus.VALID:
            lean.verifier = verifier
            _log.info("lean REPL warm in %.0fs", time.monotonic() - lean.started)
        else:
            lean.error = f"warm-up check returned {result.status.value}: {result.output[:200]}"
    if lean.error:
        _log.warning("lean elaboration disabled: %s", lean.error)
    lean.ready.set()


@dataclass(slots=True)
class DeepStack:
    """The paid detector stack, built once per server lifetime.

    ``spend`` returns cumulative USD across every client in the stack, so a
    before/after delta prices one call (actual usage-block accounting, not
    planning estimates).
    """

    judge_a_theorem: ReviewAid
    judge_b_theorem: ReviewAid
    judge_a_definition: ReviewAid
    judge_b_definition: ReviewAid
    prober: LLMClient
    spend: Callable[[], float]


def _build_deep_stack(settings: Settings) -> DeepStack:
    """Judges + prober in the calibrated configuration.

    Judge A is the v1 prompt on the default model, judge B the checklist
    prompt on the configured judge-B model (Fable by default — a
    locked-surface model where thinking is forced on, so it needs the large
    max_tokens rather than a thinking config), prober on the default model.
    """
    judge_b_max_tokens = (
        32000
        if settings.judge_b_model.startswith(("claude-fable", "claude-mythos"))
        else max(settings.max_tokens, 4096)
    )
    # Server-lifetime client; never closed (the process owns it until exit).
    http = httpx.Client(timeout=settings.anthropic_timeout_seconds)
    client_a = AnthropicClient(
        http,
        model=settings.anthropic_model,
        base_url=settings.anthropic_base_url,
        version=settings.anthropic_version,
        max_tokens=settings.max_tokens,
        temperature=0.0,
        cache_system=True,
        thinking={"type": "disabled"},
    )
    client_b = AnthropicClient(
        http,
        model=settings.judge_b_model,
        base_url=settings.anthropic_base_url,
        version=settings.anthropic_version,
        max_tokens=judge_b_max_tokens,
        temperature=0.0,
        cache_system=True,
    )
    prober = AnthropicClient(
        http,
        model=settings.anthropic_model,
        base_url=settings.anthropic_base_url,
        version=settings.anthropic_version,
        max_tokens=6000,
        temperature=0.0,
        cache_system=True,
        thinking={"type": "disabled"},
    )
    clients = (client_a, client_b, prober)

    def spend() -> float:
        return sum(c.usage.estimated_cost_usd() for c in clients)

    return DeepStack(
        judge_a_theorem=BackTranslator(client_a),
        judge_b_theorem=BackTranslator(client_b, checklist=True),
        judge_a_definition=BackTranslator(client_a, definition=True),
        judge_b_definition=BackTranslator(client_b, definition=True),
        prober=prober,
        spend=spend,
    )


def _make_pair(informal: str, lean: str, kind: str | None) -> ExternalPair:
    if not informal.strip():
        raise ValueError("informal must be a non-empty natural-language statement")
    if not lean.strip():
        raise ValueError("lean must be a non-empty Lean 4 statement")
    if kind is not None and kind not in ("theorem", "definition"):
        raise ValueError("kind must be 'theorem' or 'definition' (omit to infer)")
    digest = hashlib.sha256(f"{informal}\x00{lean}".encode()).hexdigest()[:10]
    return ExternalPair(
        pair_id=f"mcp-{digest}",
        kind=kind or infer_kind({}, lean),
        informal=informal.strip(),
        lean=lean.strip(),
    )


def _evidence_tier(score: PairScore) -> str:
    reasons = set(score.reasons)
    if "falsified-by-counterexample" in reasons:
        return "counterexample"
    if any(r.startswith("deterministic-vacuous") for r in reasons):
        return "deterministic"
    judge_flags = reasons & {"judge-a-mismatch", "judge-b-mismatch"}
    if len(judge_flags) == 2:
        return "two-judge-consensus"
    if judge_flags:
        return "single-judge"
    return "none"


def _payload(
    score: PairScore,
    *,
    checks_skipped: list[str],
    actual_cost_usd: float | None = None,
) -> dict[str, Any]:
    """The full evidence record plus the disclosures no response may omit."""
    result: dict[str, Any] = asdict(score)
    result["evidence_tier"] = _evidence_tier(score)
    result["evidence_tier_note"] = _EVIDENCE_TIER_NOTE
    result["not_certification"] = _NOT_CERTIFICATION
    result["calibration"] = CALIBRATION_DISCLOSURE
    result["checks_skipped"] = checks_skipped
    if actual_cost_usd is not None:
        result["actual_cost_usd"] = round(actual_cost_usd, 4)
    return result


class ScreenerRuntime:
    """Owns the warming Lean verifier and the lazily-built paid stack.

    ``deep_factory`` is injected so tests exercise both tools without a
    network or a Lean toolchain; :meth:`start` wires the real ones.
    """

    def __init__(
        self,
        settings: Settings,
        lean: LeanState,
        deep_factory: Callable[[], DeepStack],
    ) -> None:
        self._settings = settings
        self._lean = lean
        self._deep_factory = deep_factory
        self._deep: DeepStack | None = None
        # One lock for stack build AND scoring: deep calls serialize so the
        # before/after spend delta prices exactly one call.
        self._deep_lock = threading.Lock()

    @classmethod
    def start(cls, settings: Settings) -> ScreenerRuntime:
        """Build the runtime and kick off the background REPL warm-up."""
        lean = LeanState()
        try:
            verifier = build_verifier(settings)
        except (LeanScreenError, OSError, ValueError) as exc:
            lean.error = f"verifier unavailable: {exc}"
            lean.ready.set()
            _log.warning("lean elaboration disabled: %s", exc)
        else:
            threading.Thread(
                target=warm_verifier, args=(lean, verifier), daemon=True, name="lean-warm"
            ).start()
        return cls(settings, lean, lambda: _build_deep_stack(settings))

    def check_fast(self, informal: str, lean: str, kind: str | None = None) -> dict[str, Any]:
        pair = _make_pair(informal, lean, kind)
        skipped = [
            "llm-judges: not run — check_fast is the free deterministic screen; "
            "run check_deep before anything ships"
        ]
        verifier: Verifier | None = None
        if self._lean.error:
            skipped.append(f"lean-elaboration: skipped ({self._lean.error})")
        elif not self._lean.ready.is_set():
            elapsed = int(time.monotonic() - self._lean.started)
            skipped.append(
                f"lean-elaboration: Lean still warming, {elapsed} seconds elapsed — "
                "returning lint + vacuity results only"
            )
        else:
            verifier = self._lean.verifier
        score = score_pair(pair, verifier=verifier, prober=None)
        return _payload(score, checks_skipped=skipped)

    def check_deep(self, informal: str, lean: str, kind: str | None = None) -> dict[str, Any]:
        pair = _make_pair(informal, lean, kind)
        skipped: list[str] = []
        verifier: Verifier | None = None
        if not self._lean.ready.wait(timeout=_DEEP_WARM_WAIT_SECONDS):
            elapsed = int(time.monotonic() - self._lean.started)
            skipped.append(
                f"lean-elaboration: REPL still importing mathlib after {elapsed} "
                "seconds — scored without elaboration"
            )
        elif self._lean.error:
            skipped.append(f"lean-elaboration: skipped ({self._lean.error})")
        else:
            verifier = self._lean.verifier
        if pair.kind == "definition":
            skipped.append(
                "counterexample-probe: not run (the falsification prompt is "
                "theorem-shaped; definitions are judge-screened only)"
            )
        with self._deep_lock:
            if self._deep is None:
                self._deep = self._deep_factory()
            stack = self._deep
            judge_a, judge_b = (
                (stack.judge_a_definition, stack.judge_b_definition)
                if pair.kind == "definition"
                else (stack.judge_a_theorem, stack.judge_b_theorem)
            )
            before = stack.spend()
            score = score_pair(
                pair,
                judge_a=judge_a,
                judge_b=judge_b,
                verifier=verifier,
                prober=stack.prober,
            )
            cost = stack.spend() - before
        return _payload(score, checks_skipped=skipped, actual_cost_usd=cost)


_MCP = FastMCP(
    "lean-faithfulness-screen",
    instructions=(
        "Faithfulness screen for informal↔Lean 4 statement pairs. check_fast "
        "is free, deterministic, and safe to call constantly while drafting; "
        "check_deep adds two paid LLM judges and a counterexample probe for "
        "use before something ships. Both may only REJECT: passing is never a "
        "certification of faithfulness, and every flag is a candidate for "
        "human confirmation, not a verdict."
    ),
)

_RUNTIME: ScreenerRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def _runtime() -> ScreenerRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = ScreenerRuntime.start(Settings())
        return _RUNTIME


@_MCP.tool()
def check_fast(informal: str, lean: str, kind: str | None = None) -> dict[str, Any]:
    """Deterministic faithfulness screen for one informal↔Lean 4 pair: lints,
    vacuity/triviality checks, and (when a Lean+mathlib REPL is configured
    and warm) elaboration. Free — no API calls, no key — and ~0.1s once the
    REPL is warm, so call it constantly while drafting. Catches statements
    that do not compile, trivially satisfiable existentials, vacuously-true
    specs, and claims hidden behind an unused declaration.

    This screen may only REJECT: an outcome of 'passed_screening' means no
    defect was found by this harness — it is NOT a certification of
    faithfulness (measured against human verdicts, human-rejected pairs
    still passed the full screen 17.0% of the time for theorems, 35.6% for
    definitions). Every flag is a candidate for human confirmation, not a
    verdict.

    ``kind`` is 'theorem' or 'definition'; omit it to infer from the Lean
    declaration head.
    """
    return _runtime().check_fast(informal, lean, kind)


@_MCP.tool()
def check_deep(informal: str, lean: str, kind: str | None = None) -> dict[str, Any]:
    """Full faithfulness screen: everything in check_fast plus two independent
    LLM judges under strict consensus and an adversarial counterexample probe.
    Costs roughly 17–27¢ per statement (actual spend is reported in the
    response as actual_cost_usd) and takes 30–60 seconds — call it
    deliberately, before something ships. Requires ANTHROPIC_API_KEY.

    This screen may only REJECT: an outcome of 'passed_screening' means no
    defect was found by this harness, NOT a certification of faithfulness
    (measured against human verdicts, human-rejected pairs still passed
    17.0% of the time for theorems, 35.6% for definitions). Every flag —
    including a probe counterexample — is a candidate requiring human
    confirmation, not a verdict. The response ranks its evidence: a
    counterexample outranks a deterministic lint, which outranks two-judge
    consensus; a single-judge flag is below our own reporting bar.

    ``kind`` is 'theorem' or 'definition'; omit it to infer from the Lean
    declaration head.
    """
    return _runtime().check_deep(informal, lean, kind)


def main() -> None:
    """Entry point for the ``leanscreen`` console script.

    Bare ``leanscreen`` (no arguments) runs the MCP stdio server — the
    invocation every MCP client config uses, so it must stay argument-free.
    Any argument (``check ...``, ``--help``) dispatches to the one-shot CLI
    in :mod:`leanscreen.cli` instead.

    For the server: logging goes to stderr (stdout is the MCP protocol
    channel), and the runtime is built eagerly so the REPL starts importing
    mathlib NOW, not on the user's first call.
    """
    if len(sys.argv) > 1:
        from leanscreen.cli import main as cli_main  # deferred: avoids a cycle

        raise SystemExit(cli_main(sys.argv[1:]))
    configure_logging()
    _runtime()
    _MCP.run()
