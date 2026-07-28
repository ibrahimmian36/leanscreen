"""Free structural faithfulness lints — mechanical tells from docs/faithfulness-patterns.md.

Each lint is a cheap, no-LLM check over a THEOREM draft's Lean text that flags a
*reviewer attention signal*, never a verdict. They complement (never duplicate)
:mod:`quality`'s vacuous-statement REJECTS: quality catches the blatant whole-goal
cheats at draft time; these lints surface the subtler patterns that historically
slipped through human review, so the reviewer looks at the right clause first.

Lints and their taxonomy anchors (docs/faithfulness-patterns.md):

* ``hypothesis-conjunct-in-conclusion`` (A1) — a conclusion ∧-conjunct is verbatim
  one of the hypotheses: that conjunct is assumed, not proved.
* ``unused-binder`` (C3) — an explicit named binder never referenced by any other
  binder type or the goal. Unused *named subjects* are the dead-parameter tell;
  implicit/instance binders are exempt (used by elaboration/resolution).
* ``exists-unique-pinned-witness`` (A3) — an ``∃!`` whose body pins the witness
  pointwise (``∀ x, f x = …``) or directly (``f = …``): uniqueness is funext, so
  the informal's uniqueness claim carries no content in this render.
* ``nat-floor-div`` / ``nat-truncated-sub`` (C4) — ``/`` or ``-`` between
  ℕ-typed operands in the statement: nat division floors and nat subtraction
  truncates; verify against the informal's ⌈⌉/⌊⌋ intent and range guards.

Annotate-only by design: findings are printed/exported for reviewers and never
written to verdict columns (the firewall is untouched). Definitions are out of
scope — a ``def`` legitimately has a body and none of these signals apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanscreen.quality import (
    _BLOCK_COMMENT_RE,
    _CLOSE,
    _LINE_COMMENT_RE,
    _OPEN,
    _collapse,
    _first_top_colon,
    _first_top_sep,
    _strip_outer_parens,
    _strip_proof,
)

_NAT_TYPES = frozenset({"ℕ", "Nat"})
_EXISTS_UNIQUE_RE = re.compile(r"∃!\s*([\w']+)")


@dataclass(frozen=True, slots=True)
class LintFinding:
    """One advisory signal: a pattern code plus what exactly tripped it."""

    code: str
    detail: str


def _word_re(name: str) -> re.Pattern[str]:
    """Word-boundary matcher that skips field-access suffixes (same as quality)."""
    return re.compile(rf"(?<![\w'.]){re.escape(name)}(?![\w'])")


def _explicit_binders(binders: str) -> list[tuple[str, str]]:
    """(name, type) for each EXPLICIT ``(a b : T)`` binder, in order.

    Unlike :func:`quality._binder_name_types` this keeps only parenthesis
    binders: implicit ``{…}`` and instance ``[…]`` binders are consumed by
    elaboration/typeclass resolution, so "unused" is meaningless for them.
    """
    pairs: list[tuple[str, str]] = []
    depth = 0
    start = -1
    opener = ""
    for i, c in enumerate(binders):
        if c in _OPEN:
            if depth == 0:
                start = i
                opener = c
            depth += 1
        elif c in _CLOSE:
            depth -= 1
            if depth == 0 and start >= 0:
                if opener == "(":
                    inner = binders[start + 1 : i]
                    ci = _first_top_colon(inner)
                    if ci >= 0:
                        typ = _collapse(inner[ci + 1 :])
                        for name in inner[:ci].split():
                            pairs.append((name, typ))
                start = -1
    return pairs


def _top_conjuncts(prop: str) -> list[str]:
    """Split a proposition on top-level ``∧`` into normalized conjuncts."""
    parts: list[str] = []
    rest = prop
    while True:
        idx = _first_top_sep(rest, "∧")
        if idx < 0:
            parts.append(_strip_outer_parens(_collapse(rest)))
            return parts
        parts.append(_strip_outer_parens(_collapse(rest[:idx])))
        rest = rest[idx + 1 :]


def _signature_and_goal(lean_code: str) -> tuple[str, str] | None:
    """(binders, goal) of a single-declaration theorem draft, or None."""
    code = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", lean_code))
    sig = _strip_proof(code.strip()).strip()
    colon = _first_top_colon(sig)
    if colon < 0:
        return None
    return sig[:colon], sig[colon + 1 :]


def _lint_hypothesis_conjunct(binders: str, goal: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    goal_norm = _strip_outer_parens(_collapse(goal))
    conjuncts = _top_conjuncts(goal_norm)
    if len(conjuncts) < 2:  # whole-goal ≡ hypothesis is already quality's reject
        return findings
    hyp_types = {_strip_outer_parens(_collapse(t)) for _n, t in _explicit_binders(binders)}
    for conj in conjuncts:
        if conj and conj in hyp_types:
            findings.append(
                LintFinding(
                    "hypothesis-conjunct-in-conclusion",
                    f"conclusion conjunct is verbatim a hypothesis: {conj[:120]}",
                )
            )
    return findings


# A type containing any Prop-forming symbol is a HYPOTHESIS, not data. `→` is
# excluded on purpose: `V → ℕ` is a data (function) type. First 500-draft field
# run: descriptive hypothesis names (`constructible_iff`, `case1`, `positivity`)
# dominated the false positives — the h-prefix convention alone is not enough.
_PROP_SYMBOL_RE = re.compile(r"[=≠≤<≥>∈∉⊆∤∣↔¬∧∨∃]|(?<![\w'])∀")


# Implicit/instance binder groups: their TYPES count as usage of earlier data
# binders (`(α : Type) {x : α}` uses α), but their names are never themselves
# candidates for the dead-parameter flag.
_IMPLICIT_GROUP_RE = re.compile(r"[{\[⦃]([^}\]⦄]*)[}\]⦄]")


def _lint_unused_binders(binders: str, goal: str) -> list[LintFinding]:
    pairs = _explicit_binders(binders)
    implicit_text = " ".join(_IMPLICIT_GROUP_RE.findall(binders))
    findings: list[LintFinding] = []
    for idx, (name, typ) in enumerate(pairs):
        # A named hypothesis (h-prefixed by convention, or any Prop-shaped type)
        # is consumed by the proof, so absence from the statement is normal.
        # The dead-parameter tell is about DATA subjects (K, f, obj, Λ) only.
        if name.startswith(("_", "h")) or _PROP_SYMBOL_RE.search(typ):
            continue
        rest = " ".join(t for j, (_n, t) in enumerate(pairs) if j != idx)
        if (
            _word_re(name).search(rest)
            or _word_re(name).search(goal)
            or _word_re(name).search(implicit_text)
        ):
            continue
        findings.append(LintFinding("unused-binder", f"explicit binder never used: {name}"))
    return findings


def _pins_witness(conjunct: str, witness: str) -> bool:
    """True if this conjunct forces the witness: ``w = …`` or ``∀ x…, w x… = …``.

    The equation's LEFT side must be exactly the witness (direct pin) or an
    application headed by it (pointwise pin, → funext). A witness merely
    *occurring* inside an equation (``a * x = 1``) constrains it and is fine.
    """
    body = conjunct.strip()
    if body.startswith("∀"):
        comma = _first_top_sep(body, ",")
        if comma < 0:
            return False
        body = body[comma + 1 :].strip()
    eq = _first_top_sep(body, "=")
    if eq < 0:
        return False
    lhs_tokens = _strip_outer_parens(_collapse(body[:eq])).split()
    return bool(lhs_tokens) and lhs_tokens[0] == witness


def _lint_exists_unique(goal: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for m in _EXISTS_UNIQUE_RE.finditer(goal):
        witness = m.group(1)
        after = goal[m.end() :]
        comma = _first_top_sep(after, ",")
        if comma < 0:
            continue
        body = _strip_outer_parens(_collapse(after[comma + 1 :]))
        if any(_pins_witness(conj, witness) for conj in _top_conjuncts(body)):
            findings.append(
                LintFinding(
                    "exists-unique-pinned-witness",
                    f"∃! witness `{witness}` is pinned pointwise/directly — "
                    "uniqueness is funext, not content",
                )
            )
    return findings


# Types under which `/` is true division and `-` does not truncate. A nat-looking
# `N / 2` ASCRIBED to one of these — `(N / 2 : ℝ)` — is real division by leaf
# coercion, NOT a floor (confirmed in the 2026-07-08 field review).
_FIELD_ASCRIPTION = ("ℝ≥0∞", "ℝ≥0", "ℝ", "ℚ", "ℂ", "NNReal", "ENNReal", "Real", "Rat", "Complex")


def _ascribed_to_field(goal: str, pos: int) -> bool:
    """True if the arithmetic op at ``pos`` sits in a ``(… : <field>)`` group.

    Leaf-coercion elaboration makes the ascription win: ``(N / 2 : ℝ)`` is real
    division and ``(n - 2 : ℝ)`` is real subtraction — neither is a ℕ floor or
    truncation. Finds the innermost enclosing parenthesis pair and inspects its
    top-level type ascription; conservative (returns False) when there is none.
    """
    depth = 0
    open_idx = -1
    for i in range(pos - 1, -1, -1):
        c = goal[i]
        if c in _CLOSE:
            depth += 1
        elif c in _OPEN:
            if depth == 0:
                open_idx = i
                break
            depth -= 1
    if open_idx < 0:
        return False
    depth = 0
    close_idx = -1
    for i in range(open_idx, len(goal)):
        c = goal[i]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return False
    inner = goal[open_idx + 1 : close_idx]
    ci = _first_top_colon(inner)
    if ci < 0:
        return False
    tail = inner[ci + 1 :].strip()
    return any(tail.startswith(t) for t in _FIELD_ASCRIPTION)


def _lint_nat_arith(binders: str, goal: str) -> list[LintFinding]:
    nat_names = {n for n, t in _explicit_binders(binders) if t in _NAT_TYPES}
    if not nat_names:
        return []
    operand = "|".join(re.escape(n) for n in sorted(nat_names))
    pat = re.compile(rf"(?<![\w'.])({operand}|\d+)\s*([/-])\s*({operand}|\d+)(?![\w'])")
    findings: list[LintFinding] = []
    seen: set[str] = set()
    for m in pat.finditer(goal):
        left, op, right = m.group(1), m.group(2), m.group(3)
        if left.isdigit() and right.isdigit():
            continue  # literal arithmetic, no ℕ variable involved
        if not ({left, right} & nat_names):
            continue
        # `x - 1` (var minus one) is overwhelmingly range-guarded and benign —
        # it was 80% of the subtraction noise in the first 500-draft field run.
        # The risky shapes are literal-minus-var (`2 - m`), var-minus-var
        # (`d - n`), and var-minus-larger-literal (`a - 5`).
        if op == "-" and right == "1" and left in nat_names:
            continue
        # `(N / 2 : ℝ)` is real division, not a ℕ floor — the ascription wins.
        if _ascribed_to_field(goal, m.start()):
            continue
        expr = f"{left} {op} {right}"
        if expr in seen:
            continue
        seen.add(expr)
        code = "nat-floor-div" if op == "/" else "nat-truncated-sub"
        note = "nat `/` floors" if op == "/" else "nat `-` truncates at 0"
        findings.append(LintFinding(code, f"{note}: `{expr}` — check the informal's intent"))
    return findings


_TRUE_LEAF_RE = re.compile(r"(?:,|∧|→|:=)\s*True\b|\bTrue\s*(?:∧|→)")


def lint_definition(lean_code: str) -> list[LintFinding]:
    """Advisory findings for one DEFINITION draft (empty list = no signals).

    ``true-leaf-def``: the body contains a literal ``True`` proposition leaf
    (``∃ _ : …, True``, ``… ∧ True``) — a defining condition was stubbed out
    rather than encoded. Calibration (2026-07-14): 0/300 human-FAITHFUL defs
    fire this, so it is near-pure signal; still advisory-only by contract.
    (A broader "no mathlib name in the body" heuristic was measured at 26%%
    on human-faithful defs — no discrimination — and deliberately rejected.)
    """
    stripped = _collapse(_BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", lean_code)))
    if ":=" not in stripped:
        return []
    body = stripped.split(":=", 1)[1]
    findings: list[LintFinding] = []
    if _TRUE_LEAF_RE.search(body):
        findings.append(
            LintFinding(
                "true-leaf-def",
                "the body contains a literal `True` leaf — a defining condition "
                "from the paper was stubbed out, not encoded",
            )
        )
    findings += _lint_alias_def(stripped, body)
    findings += _lint_partial_operation(body)
    return findings


# A body that merely APPLIES one existing (dotted or plain) name to the def's
# own binder names, e.g. `def legendreSymbolDef (a : ℤ) (p : ℕ) : ℤ :=
# legendreSym p a` — the 2026-07-15 consensus-breaking set's "alias, not a
# definition" escape class: it formalizes nothing, so both judges pass it.
_IDENT = r"[A-Za-z_][\w'.]*"
_ALIAS_BODY_RE = re.compile(rf"^\s*({_IDENT})((?:\s+{_IDENT})*)\s*$")


def _lint_alias_def(stripped: str, body: str) -> list[LintFinding]:
    m = _ALIAS_BODY_RE.match(body)
    if m is None:
        return []
    head, arg_text = m.group(1), m.group(2)
    args = arg_text.split()
    # binder names of the def: words introduced in ( ... : ...) groups pre-`:=`
    signature = stripped.split(":=", 1)[0]
    binders = set(re.findall(rf"[(\[{{⦃]\s*({_IDENT})(?:\s+{_IDENT})*\s*:", signature))
    for group in re.findall(r"[(\[{⦃]([^:()\[\]{}⦃⦄]*):", signature):
        binders.update(group.split())
    if head in binders:
        return []  # applying one's own parameter is a real body, not an alias
    if args and all(a in binders for a in args):
        return [
            LintFinding(
                "alias-def",
                f"the body just applies the existing name `{head}` to the def's own "
                "binders — it renames, it does not define; check the paper's concept "
                "was actually formalized",
            )
        ]
    return []


# mathlib's limUnder / sSup / sInf are TOTAL: they return a junk default (0,
# sSup ∅, …) whenever the limit does not exist or the set is unbounded/empty.
# A definition built on them silently encodes "the limit, if it happens to
# exist" — the 2026-07-15 escapes 13525 (limUnder with no existence guarantee)
# and 8185 (sSup of a possibly-unbounded set). Advisory: point the reviewer at
# the existence/boundedness obligation the informal must supply.
_PARTIAL_OP_RE = re.compile(r"\b(limUnder|lim\b|sSup|sInf|⨆|⨅)")


def _lint_partial_operation(body: str) -> list[LintFinding]:
    hits = sorted({m.group(1) for m in _PARTIAL_OP_RE.finditer(body)})
    if not hits:
        return []
    return [
        LintFinding(
            "partial-operation-default",
            f"the body uses {', '.join(f'`{h}`' for h in hits)} — total in mathlib "
            "with a junk default when the limit/sup/inf does not exist; check the "
            "informal guarantees existence/boundedness or the value is meaningless",
        )
    ]


# --- extremal completeness (bimodal: needs the informal) ---------------------
#
# The 2026-07-16 miniF2F error analysis found the dominant TRUE-miss class of
# the dual-judge screen: the informal asks for a minimum / maximum / complete
# solution set, and the Lean encodes only a one-sided bound or one implication
# direction ("find the smallest n" -> `20 ≤ n`, no attainability). Judges pass
# these because the weaker statement is TRUE — truth-preserving weakening is a
# systematic judge blind spot, so it gets a deterministic tell. 6 of the ~9
# genuine misses in that analysis match this exact shape.

# "at least/at most", "least common multiple", "greatest common divisor/factor"
# are qualifier/name uses of the keywords, not extremal asks.
_EXTREMAL_NOISE_RE = re.compile(
    r"\bat\s+(?:least|most)\b|\bleast\s+common\b|\bgreatest\s+common\b",
    re.IGNORECASE,
)
_EXTREMAL_ASK_RE = re.compile(
    r"\b(?:smallest|largest|minimum|maximum|greatest|least|minimal|maximal"
    r"|find\s+all|determine\s+all|solve)\b",
    re.IGNORECASE,
)
# Anywhere in the statement, these encode the extremum/characterization side
# the ask requires; their presence means the render carries the full content.
_COMPLETENESS_RE = re.compile(
    r"IsLeast|IsGreatest|IsLUB|IsGLB|IsMinOn|IsMaxOn|IsExtrOn|sInf|sSup|⨅|⨆"
    r"|↔|∃!|Finset\.min|Finset\.max|Nat\.find|argmin|argmax"
)
_BARE_BOUND_RE = re.compile(r"[≤<≥>]")


def lint_extremal(informal: str, lean_code: str) -> list[LintFinding]:
    """Advisory: informal asks for an extremum/complete answer set, Lean goal
    is a bare bound with no attainability/characterization anywhere. Fires only
    when both sides of the tell are present; parse failure returns clean."""
    if not _EXTREMAL_ASK_RE.search(_EXTREMAL_NOISE_RE.sub(" ", informal)):
        return []
    parsed = _signature_and_goal(lean_code)
    if parsed is None:
        return []
    _binders, goal = parsed
    if _COMPLETENESS_RE.search(lean_code):
        return []
    if not _BARE_BOUND_RE.search(goal):
        return []
    return [
        LintFinding(
            "extremal-incomplete",
            "the informal asks for an extremum or complete solution set, but the "
            "goal is a bare bound/inequality with no IsLeast/IsGreatest/iff — "
            "a true-but-weaker statement; check attainability is not required",
        )
    ]


def lint_statement(lean_code: str) -> list[LintFinding]:
    """All advisory findings for one THEOREM draft (empty list = no signals).

    Conservative on parse failure: a draft whose signature can't be split is
    returned clean — these lints must never manufacture noise from exotic syntax.
    """
    parsed = _signature_and_goal(lean_code)
    if parsed is None:
        return []
    binders, goal = parsed
    findings: list[LintFinding] = []
    findings += _lint_hypothesis_conjunct(binders, goal)
    findings += _lint_unused_binders(binders, goal)
    findings += _lint_exists_unique(goal)
    findings += _lint_nat_arith(binders, goal)
    return findings
