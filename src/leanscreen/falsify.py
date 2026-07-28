"""Counterexample falsification probe — shared by the adversarial sweep script
and the external scoring harness.

A statement is FALSIFIED only when the model exhibits concrete values that
satisfy every hypothesis while the conclusion fails, under real Lean/mathlib
semantics (junk values included). Objective evidence, not judgment: the probe
may only ever add a REJECT signal (invariant #4 — nothing here certifies).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from leanscreen.llm import LLMClient

FALSIFY_SYSTEM_PROMPT = (
    "You attempt to FALSIFY a Lean 4 theorem statement by constructing an explicit "
    "counterexample. This is a narrow, objective task — NOT a faithfulness judgment, "
    "NOT a style review, NOT a provability assessment. A statement is FALSIFIED only "
    "if you exhibit concrete values for its variables and free parameters such that "
    "every hypothesis is satisfied and the conclusion is false, all under actual "
    "Lean/mathlib semantics.\n"
    "SEARCH ORDER — try each in turn:\n"
    "1. Degenerate instantiations of unpinned parameters: the empty set, the zero "
    "function, a constant function, Set.univ, t = 0, n = 0 or 1, the trivial "
    "group/space. A parameter with no hypothesis linking it to the conclusion's "
    "objects is free — instantiate it adversarially.\n"
    "2. Junk values — these are REAL mathlib semantics and count: Real.log 0 = 0; "
    "x / 0 = 0; an `∃ C : ENNReal` may pick C = ∞ (and ∞ * 0 = 0); nat subtraction "
    "truncates at 0; nat division floors; Real.sqrt of a negative = 0; `deriv` of a "
    "non-differentiable function = 0.\n"
    "3. Boundary cases the hypotheses permit: x = -1 when the hypothesis is -1 ≤ x; "
    "equality cases of ≤; empty index ranges (empty product = 1, empty sum = 0); "
    "k = 0 exponents.\n"
    "4. Extreme but legal values: a constant large enough or small enough to break "
    "an unparameterized inequality.\n"
    "RULES:\n"
    "- The counterexample must be CHECKABLE: state the value of every relevant "
    "variable, verify EACH hypothesis holds (including typeclass assumptions like "
    "[Fact p.Prime] — pick a concrete instance), and show the conclusion fails.\n"
    "- If you cannot satisfy all hypotheses simultaneously, the statement is "
    "vacuously true — that is NOT falsified.\n"
    "- 'I cannot prove it' is NOT falsified. Generality mismatches with the "
    "informal statement are out of scope.\n"
    "- Do not claim falsified on an uncertain computation: if the conclusion's "
    "failure rests on arithmetic you are not sure of, report not falsified.\n"
    "- Prefer the SIMPLEST counterexample that works.\n"
    "- BUDGET your search: brief scratch work is fine, but keep it under ~300 words. "
    "If the search is inconclusive by then, stop and report not falsified. You MUST "
    "end your reply with the JSON verdict object — a reply without it is discarded "
    "and wasted.\n"
    "OUTPUT: end with exactly one JSON object:\n"
    '{"falsified": true|false, '
    '"counterexample": "<explicit assignment of every relevant variable, or empty>", '
    '"check": "<hypotheses hold because ...; conclusion fails because ...>", '
    '"informal_also_false": true|false|null}\n'
    "Set informal_also_false = true ONLY when falsified AND the informal statement "
    "plainly asserts the same false claim (then this may be a faithful render of a "
    "false claim — a different problem than a translation error). Use null when "
    "not falsified."
)


@dataclass(frozen=True, slots=True)
class FalsifyResult:
    """One falsification attempt's verdict."""

    falsified: bool
    counterexample: str
    check: str
    informal_also_false: bool | None


def extract_json(raw: str) -> dict[str, Any] | None:
    """Last balanced ``{...}`` in ``raw`` that parses as a JSON object.

    A backward balanced-brace scan, not a regex: the model's prose routinely
    contains Lean braces, and the verdict object is always last.
    """
    end = raw.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            if raw[i] == "}":
                depth += 1
            elif raw[i] == "{":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[i : end + 1])
                    except json.JSONDecodeError:
                        break
                    return obj if isinstance(obj, dict) else None
        end = raw.rfind("}", 0, end)
    return None


def falsify(client: LLMClient, informal: str, lean_code: str) -> FalsifyResult:
    """Run one falsification attempt; raises ValueError on an unparseable reply."""
    user = f"Informal statement:\n{informal}\n\nLean statement to falsify:\n{lean_code}"
    raw = client.complete(FALSIFY_SYSTEM_PROMPT, user)
    obj = extract_json(raw)
    if obj is None:
        raise ValueError(f"no JSON object in response: {raw[:120]!r}")
    falsified = obj.get("falsified")
    if not isinstance(falsified, bool):
        raise ValueError(f"missing/invalid falsified field: {raw[:120]!r}")
    also = obj.get("informal_also_false")
    return FalsifyResult(
        falsified=falsified,
        counterexample=str(obj.get("counterexample", "")).strip()[:600],
        check=str(obj.get("check", "")).strip()[:800],
        informal_also_false=also if isinstance(also, bool) else None,
    )
