"""Score one informal↔Lean 4 pair through the full detector stack.

The single-pair core of the screen:

1. deterministic screens — vacuity + lints (free);
2. best-effort Lean elaboration in YOUR mathlib environment (a differing
   prelude downgrades to ``did_not_elaborate_in_our_env`` — it is never
   called invalid);
3. optional dual-judge strict consensus — the exact judge configuration whose
   error rates were measured against 886 human verdicts (2026-07-15), so
   every verdict carries known error bars;
4. optional counterexample falsification probe.

The standing invariant: this harness FLAGS or PASSES SCREENING. It never
certifies faithful — only a human does that, and :data:`CALIBRATION_DISCLOSURE`
says so in as many words.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field

from leanscreen.backtranslate import ReviewAid
from leanscreen.errors import LLMError, VerifierError
from leanscreen.falsify import FalsifyResult, falsify
from leanscreen.lints import lint_definition, lint_extremal, lint_statement
from leanscreen.llm import LLMClient
from leanscreen.logging import get_logger
from leanscreen.quality import claim_count, has_withheld_declaration, is_vacuous
from leanscreen.verify import (
    Verifier,
    ensure_import,
    ensure_proof_stub,
    extract_errors,
    normalize_legacy_syntax,
)

_log = get_logger(__name__)

# Serializes access to the (stateful, single-process) Lean verifier.
_VERIFIER_LOCK = threading.Lock()

# Judge-level calibration measured 2026-07-15 against 886 frozen human verdicts
# (595 faithful / 291 unfaithful; strict two-judge consensus, both must pass).
# This constant exists so every result carries its own error bars.
CALIBRATION_DISCLOSURE = (
    "**What a verdict is worth (measured, not promised).** This screen's judge "
    "configuration was calibrated 2026-07-15 against 886 frozen human verdicts "
    "(595 faithful / 291 unfaithful) on our own production corpus. Under strict "
    "two-judge consensus: pairs a human reviewer had rejected still PASSED the "
    "screen 17.0% of the time for theorems and 35.6% for definitions (23.4% "
    "combined); pairs a human had certified faithful were flagged 15–18% of "
    "the time. Therefore `passed_screening` means “no defect found by this "
    "harness” — it is NOT a certification of faithfulness. Certification "
    "requires human review (our standing invariant: automated screens may only "
    "reject; only humans certify). Both judges are Anthropic-family models; "
    "correlated blind spots cannot be ruled out. Human certification of any "
    "slice of this corpus is available as a follow-on engagement."
)


@dataclass(frozen=True, slots=True)
class ExternalPair:
    """One informal↔Lean pair to screen."""

    pair_id: str
    kind: str  # "theorem" | "definition"
    informal: str
    lean: str


@dataclass(slots=True)
class PairScore:
    """Everything the harness learned about one pair — the evidence record."""

    pair_id: str
    kind: str
    outcome: str  # "flagged" | "needs_human_review" | "passed_screening"
    reasons: list[str] = field(default_factory=list)
    # confirmation pass (confirm_flags): repeat judge verdicts on a judge-only flag
    confirm_verdicts: str = ""
    # valid_in_our_env | did_not_elaborate_in_our_env | not_checked
    lean_status: str = "not_checked"
    lean_detail: str = ""
    vacuous: bool = False
    vacuous_reason: str = ""
    lints: list[str] = field(default_factory=list)
    judge_a_verdict: str = ""  # faithful | mismatch | unclear | error
    judge_a_issues: str = ""
    judge_a_back: str = ""
    judge_b_verdict: str = ""
    judge_b_issues: str = ""
    judge_b_back: str = ""
    probe_falsified: bool | None = None
    probe_counterexample: str = ""
    probe_check: str = ""  # the probe's hypotheses-hold/conclusion-fails justification
    probe_informal_also_false: bool | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def infer_kind(obj: dict[str, object], lean: str) -> str:
    """``theorem`` or ``definition``, from an explicit field or the Lean head."""
    raw = obj.get("kind")
    if isinstance(raw, str) and raw.strip().lower() in ("theorem", "definition"):
        return raw.strip().lower()
    head = lean.lstrip()
    if head.startswith(("def ", "noncomputable def ", "abbrev ", "structure ", "inductive ")):
        return "definition"
    return "theorem"


def score_pair(
    pair: ExternalPair,
    *,
    judge_a: ReviewAid | None = None,
    judge_b: ReviewAid | None = None,
    verifier: Verifier | None,
    prober: LLMClient | None,
    confirm_flags: bool = False,
) -> PairScore:
    """Run the full detector stack on one pair. Deterministic screens first;
    a deterministic-vacuous pair skips the paid judges (already flagged, and
    the aid was never calibrated on that population).

    Judges are both-or-neither: the measured calibration is for STRICT
    two-judge consensus, so a lone judge would carry error bars nobody has
    measured. With neither judge the function is the free deterministic
    screen (lints + vacuity + Lean) — the ``check_fast`` path."""
    if (judge_a is None) != (judge_b is None):
        raise ValueError(
            "supply both judges or neither — the calibration is for strict "
            "two-judge consensus, not a single judge"
        )
    score = PairScore(pair_id=pair.pair_id, kind=pair.kind, outcome="passed_screening")

    lint_fn = lint_definition if pair.kind == "definition" else lint_statement
    score.lints = [f.code for f in lint_fn(pair.lean)]
    if pair.kind == "theorem":
        score.lints += [f.code for f in lint_extremal(pair.informal, pair.lean)]

    if pair.kind == "theorem":
        vac, vac_reason = is_vacuous(pair.lean)
        # Over-abstraction is advisory (measured 27% false-positive / 73%
        # precision against ProofNetVerif's human labels, 2026-07-26 — good
        # enough to rank a queue, not to reject on); every other vacuity
        # reason is deterministic.
        if vac and vac_reason != "over-abstracted":
            score.vacuous, score.vacuous_reason = True, vac_reason
            score.reasons.append(f"deterministic-vacuous:{vac_reason}")
        elif vac:
            score.vacuous_reason = vac_reason

    if verifier is not None:
        # The REPL verifier is ONE stateful process — concurrent access
        # interleaves its stdio and kills it. Serialize every Lean check.
        try:
            with _VERIFIER_LOCK:
                result = verifier.verify(
                    ensure_import(ensure_proof_stub(normalize_legacy_syntax(pair.lean)))
                )
        except VerifierError as exc:
            # A toolchain hiccup must degrade like an env mismatch, never
            # abort the run: the semantic verdicts rest on the judges alone.
            score.lean_status = "not_checked"
            score.lean_detail = f"verifier unavailable: {exc}"[:200]
        else:
            if result.status == "valid":
                score.lean_status = "valid_in_our_env"
            else:
                # Their prelude/mathlib pin may simply differ from ours; report
                # the diagnostics, do not call the statement invalid.
                score.lean_status = "did_not_elaborate_in_our_env"
                score.lean_detail = extract_errors(result.output, max_chars=500)

    if score.reasons == ["deterministic-vacuous:multiple-declarations"]:
        # External corpora legitimately bundle several declarations per item —
        # a corpus convention, not a drafting cheat. Which of three shapes it
        # is decides everything:
        if claim_count(pair.lean) != 1 or has_withheld_declaration(pair.lean):
            # Either several claims, so there is no single statement to screen
            # against the informal text, or a `sorry`-valued value declaration
            # withholding content the theorem depends on. Neither is assessable.
            # Abstain, and skip the paid stages (the aid was never calibrated
            # on this population).
            score.outcome = "needs_human_review"
            return score
        # One claim plus auxiliary definitions with real bodies: the theorem
        # together with its definitions IS the claim. Keep the lint's verdict
        # as an advisory note and let the judges read the whole text. The
        # remaining single-declaration vacuity checks stay suppressed: the
        # proof-stripper reasons about one declaration and would inspect the
        # leading `def` rather than the theorem.
        score.reasons.clear()
        score.vacuous = False
    if score.reasons:  # deterministic-vacuous: flagged, skip paid stages
        score.outcome = "flagged"
        return score

    judges: tuple[tuple[str, ReviewAid], ...] = (
        (("a", judge_a), ("b", judge_b)) if judge_a is not None and judge_b is not None else ()
    )
    for label, aid in judges:
        try:
            review = aid.review(pair.informal, pair.lean)
            verdict, issues, back = review.verdict, review.issues, review.back_english
        except LLMError as exc:
            verdict, issues, back = "error", str(exc)[:200], ""
        if label == "a":
            score.judge_a_verdict, score.judge_a_issues, score.judge_a_back = (
                verdict,
                issues,
                back,
            )
        else:
            score.judge_b_verdict, score.judge_b_issues, score.judge_b_back = (
                verdict,
                issues,
                back,
            )
        if verdict == "mismatch":
            score.reasons.append(f"judge-{label}-mismatch")

    if prober is not None and pair.kind == "theorem":
        # The falsification prompt is theorem-shaped (hypotheses hold,
        # conclusion fails); a definition has neither, so probing one is paid
        # spend for a category-error "counterexample". Theorems only.
        try:
            probe: FalsifyResult | None = falsify(prober, pair.informal, pair.lean)
        except (LLMError, ValueError) as exc:
            probe = None
            _log.warning("probe failed on %s: %s", pair.pair_id, exc)
        if probe is not None:
            score.probe_falsified = probe.falsified
            score.probe_counterexample = probe.counterexample
            score.probe_check = probe.check
            score.probe_informal_also_false = probe.informal_also_false
            if probe.falsified:
                score.reasons.append("falsified-by-counterexample")

    if score.reasons:
        score.outcome = "flagged"
    elif "unclear" in (score.judge_a_verdict, score.judge_b_verdict) or "error" in (
        score.judge_a_verdict,
        score.judge_b_verdict,
    ):
        score.outcome = "needs_human_review"

    if (
        confirm_flags
        and judges
        and score.outcome == "flagged"
        and set(score.reasons) <= {"judge-a-mismatch", "judge-b-mismatch"}
    ):
        # Judge-only flags carry the stochastic noise (~7% run-to-run flip-on,
        # measured 2026-07-16 on unchanged statements). Mechanical evidence
        # (counterexample / deterministic) is reproducible and never re-asked.
        # One repeat pass: a reproducing flag is kept and marked; a one-off
        # flip routes to a human instead of standing as a flag.
        repeats: list[str] = []
        for label, aid in judges:
            try:
                verdict = aid.review(pair.informal, pair.lean).verdict
            except LLMError as exc:
                verdict = "error"
                _log.warning("confirm pass failed on %s: %s", pair.pair_id, exc)
            repeats.append(f"{label}={verdict}")
        score.confirm_verdicts = " ".join(repeats)
        if any(r.endswith("=mismatch") for r in repeats):
            score.reasons.append("flag-confirmed")
        elif all(r.endswith("=error") for r in repeats):
            # Confirmation UNAVAILABLE is not confirmation FAILED: an API
            # error is no evidence the original flag was a one-off flip.
            # The flag stands, marked so a human knows the repeat never ran.
            score.reasons.append("confirm-unavailable")
        else:
            score.outcome = "needs_human_review"
            score.reasons.append("unconfirmed-judge-flag")
    return score
