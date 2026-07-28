"""Step 7: back-translation review aid for faithfulness checking.

A draft that *typechecks* and *passes the vacuity filter* is only "clean", not
necessarily faithful: it may silently drop a hypothesis, weaken the conclusion,
or add an assumption. The cheapest way to surface that for a human reviewer is to
translate the Lean statement BACK to plain English (blind to the original) and
put the two side by side — mismatches jump out.

We make ONE model call per draft that returns a literal back-translation plus a
flagged verdict. This is an AID, never a gate: the human still decides. It exists
to make the review bottleneck (the real moat) fast, and to validate that our
"clean" proxy actually tracks faithfulness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from leanscreen.llm import LLMClient, UsageTotals

_SYSTEM_PROMPT = (
    "You are a meticulous Lean 4 / mathlib proof engineer doing FAITHFULNESS "
    "review. You are given an informal mathematical statement and a Lean 4 "
    "formalization of it (proof omitted). Do two things:\n"
    "1. Translate the LEAN statement back into plain English LITERALLY — say "
    "exactly what the Lean says, including every hypothesis and the precise "
    "conclusion. Do NOT consult the informal version while doing this; describe "
    "the Lean on its own terms.\n"
    "2. Compare your back-translation to the informal statement and decide whether "
    "the Lean faithfully captures it. Watch for: dropped or weakened hypotheses, a "
    "weakened or trivialized conclusion, extra assumptions the informal didn't "
    "state, free parameters standing in for specific defined objects, and "
    "quantifiers that changed scope.\n"
    "Respond in EXACTLY this format:\n"
    "BACK-TRANSLATION: <your literal English rendering of the Lean>\n"
    "VERDICT: FAITHFUL | MISMATCH | UNCLEAR\n"
    "ISSUES: <none, or a terse list of the specific discrepancies>"
)

# DEFINITION variant: same blind two-step and the SAME output format, but the
# comparison rules are definition-shaped (defining conditions, type/arity,
# degenerate bodies) instead of hypothesis/conclusion-shaped. Calibrate on the
# human def verdicts (scripts/eval_judge.py --definitions) BEFORE wiring it as
# a gate anywhere — the aid contract (reject-only, human decides) is unchanged.
_SYSTEM_PROMPT_DEFINITION = (
    "You are a meticulous Lean 4 / mathlib proof engineer doing FAITHFULNESS "
    "review of a DEFINITION. You are given an informal mathematical definition "
    "and a Lean 4 `def`/`abbrev`/`structure` intended to formalize it. Do two "
    "things:\n"
    "1. Translate the LEAN definition back into plain English LITERALLY — say "
    "exactly what object or predicate it defines, over what arguments and "
    "types, and what its body requires. Do NOT consult the informal version "
    "while doing this; describe the Lean on its own terms.\n"
    "2. Compare your back-translation to the informal definition and decide "
    "whether the Lean defines the SAME concept. Watch for: a defining "
    "condition from the informal that is missing, weakened, or replaced; a "
    "substantive condition the informal never stated; the wrong type, arity, "
    "or domain (a property of one object where the informal defines a "
    "relation, a global constant where the informal defines a family); a "
    "degenerate body (a literal True/False leaf standing in for a condition, "
    "a constant ignoring its arguments); and paper-specific components left "
    "as free unconstrained parameters instead of being defined.\n"
    "Do NOT flag faithful modeling choices: Finset vs Set, a structure vs a "
    "predicate, curried vs tupled arguments, meaning-preserving "
    "generalizations, inlined notation, or an equivalent re-encoding of the "
    "same condition.\n"
    "Respond in EXACTLY this format:\n"
    "BACK-TRANSLATION: <your literal English rendering of the Lean definition>\n"
    "VERDICT: FAITHFUL | MISMATCH | UNCLEAR\n"
    "ISSUES: <none, or a terse list of the specific discrepancies>"
)

# v2, gated behind ARXIV_FORMALIZE_SELFCHECK_CHECKLIST ('+sc2' cohorts): the
# docs/faithfulness-patterns.md triage run clause-by-clause. Same blind
# back-translation step and the SAME output format (everything downstream
# parses it); only the comparison instructions change. Adopt only if the
# judge-agreement eval beats the v1 baseline without raising false rejection.
_SYSTEM_PROMPT_CHECKLIST = (
    "You are a meticulous Lean 4 / mathlib proof engineer doing FAITHFULNESS "
    "review. You are given an informal mathematical statement and a Lean 4 "
    "formalization of it (proof omitted). Do two things:\n"
    "1. Translate the LEAN statement back into plain English LITERALLY — say "
    "exactly what the Lean says, including every hypothesis and the precise "
    "conclusion. Do NOT consult the informal version while doing this; describe "
    "the Lean on its own terms.\n"
    "2. Compare against the informal statement by auditing EACH hypothesis and "
    "EACH conclusion conjunct independently — most clauses being right says "
    "nothing about the rest. Checks:\n"
    "a. Does any hypothesis restate, contain, or one-step-yield the conclusion? "
    "Assuming a premise the informal itself states is fine; assuming the "
    "consequence the lemma exists to derive is a MISMATCH.\n"
    "b. Is any conclusion conjunct true for every object of its type? Watch junk "
    "values: Real.log 0 = 0, division by zero = 0, an ENNReal constant satisfied "
    "by infinity, nat subtraction truncating, nat division flooring.\n"
    "c. Did an informal decoration or index vanish — a torus/quotient norm as "
    "bare abs, a one-sided derivative as deriv, a superscript-n sequence as an "
    "n-free object, a ceiling as nat division? Judge operators by actual mathlib "
    "semantics on that exact type.\n"
    "d. Does the type lack structure the paper's regime uses (ZMod q is a field "
    "only for prime q; commutative renderings of noncommutative claims)?\n"
    "e. Is every stated hypothesis and constraint set present? Dropped "
    "regularity/integrability/continuity or quantifying over everything where "
    "the informal restricts to a constrained set usually makes it false.\n"
    "f. Is a named subject declared but never used (dead parameter)?\n"
    "g. Must a named specific ('all voters', 'the identity') be a concrete term "
    "(univ, 1) rather than a fresh variable? Does every refinement ('depending "
    "only on', 'in particular', multiplicity, 'is exactly') map to a Lean "
    "clause? Any extra Lean conjunct not forced by the informal's wording?\n"
    "h. Is every parameter pinned by a characterizing hypothesis or genuinely "
    "ambient? Mentally instantiate free parameters with something trivial "
    "(empty set, zero function, infinity): if the statement collapses or "
    "falsifies, it is a MISMATCH.\n"
    "NEVER flag these (they are faithful): pinned parameters (a defining "
    "equation or iff); hypothesis strengthening unless the informal explicitly "
    "names the weaker class; added implicit regularity (measurability, "
    "positivity); provably equivalent re-encodings (preimage for image, cleared "
    "denominators, sup-bound as forall); meaning-preserving generalizations; "
    "inlined definitions; consistent reindexing; a faithful render of a claim "
    "that is itself false (faithfulness is not truth).\n"
    "Verdict: a demonstrated defect is MISMATCH; a load-bearing object you "
    "cannot verify from what is shown (truncated informal, off-screen "
    "definition, a convention whose two readings differ) is UNCLEAR even when "
    "everything else is clean; all clauses passing including the never-flag "
    "guards is FAITHFUL.\n"
    "Respond in EXACTLY this format:\n"
    "BACK-TRANSLATION: <your literal English rendering of the Lean>\n"
    "VERDICT: FAITHFUL | MISMATCH | UNCLEAR\n"
    "ISSUES: <none, or a terse list of the specific discrepancies>"
)

_BACK_RE = re.compile(
    r"BACK-TRANSLATION:\s*(?P<v>.+?)(?=\n\s*VERDICT:|\Z)", re.DOTALL | re.IGNORECASE
)
_VERDICT_RE = re.compile(r"VERDICT:\s*(?P<v>FAITHFUL|MISMATCH|UNCLEAR)", re.IGNORECASE)
_ISSUES_RE = re.compile(r"ISSUES:\s*(?P<v>.+?)\Z", re.DOTALL | re.IGNORECASE)

_VALID_VERDICTS = {"faithful", "mismatch", "unclear"}


@dataclass(frozen=True, slots=True)
class BackReview:
    """A back-translation review of one draft (an aid, not a verdict of record)."""

    back_english: str
    verdict: str  # "faithful" | "mismatch" | "unclear"
    issues: str
    raw: str


class ReviewAid(Protocol):
    """A faithfulness-review aid, so the pre-screen can inject a fake in tests."""

    def review(self, informal: str, lean_code: str) -> BackReview:
        """Return a back-translation review for one informal/Lean pair."""
        ...


def build_user_prompt(informal: str, lean_code: str) -> str:
    """Compose the review prompt for one informal/Lean pair."""
    return f"Informal statement:\n{informal.strip()}\n\nLean formalization:\n{lean_code.strip()}"


def parse_review(response: str) -> BackReview:
    """Parse the structured response; degrade gracefully if the format slips."""
    back = _BACK_RE.search(response)
    verdict = _VERDICT_RE.search(response)
    issues = _ISSUES_RE.search(response)
    v = verdict.group("v").strip().lower() if verdict else "unclear"
    if v not in _VALID_VERDICTS:
        v = "unclear"
    return BackReview(
        back_english=back.group("v").strip() if back else response.strip(),
        verdict=v,
        issues=issues.group("v").strip() if issues else "",
        raw=response,
    )


class BackTranslator:
    """Produces a back-translation faithfulness aid via an injected LLM client.

    ``checklist=True`` selects the clause-by-clause v2 prompt (identical output
    format, '+sc2' cohorts); the default stays the calibrated v1 prompt.
    """

    def __init__(
        self, client: LLMClient, *, checklist: bool = False, definition: bool = False
    ) -> None:
        self._client = client
        if definition:
            self._system = _SYSTEM_PROMPT_DEFINITION
        else:
            self._system = _SYSTEM_PROMPT_CHECKLIST if checklist else _SYSTEM_PROMPT

    @property
    def usage(self) -> UsageTotals:
        """Tokens spent by the aid's client (zero for test fakes)."""
        return getattr(self._client, "usage", UsageTotals())

    def review(self, informal: str, lean_code: str) -> BackReview:
        """Return a back-translation review for one informal/Lean pair."""
        response = self._client.complete(self._system, build_user_prompt(informal, lean_code))
        return parse_review(response)
