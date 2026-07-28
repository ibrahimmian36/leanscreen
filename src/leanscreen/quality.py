"""Detect *vacuous* Lean statements — drafts that typecheck but say nothing.

On hard statements the model often games the verifier: it makes the conclusion
``True``, a reflexive ``X = X`` / ``True ↔ True``, or simply restates a
hypothesis. All of these are VALID (they compile) yet UNFAITHFUL by construction.
We detect the blatant cases automatically so they never reach a human reviewer
and are never sold.

This is intentionally conservative: a draft we cannot confidently parse is left
alone (returns not-vacuous), so genuine attempts are never wrongly rejected.
"""

from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")
_PROOF_HEAD_RE = re.compile(r"\s*(?:by|sorry)\b")
# Bracket pairs tracked by the depth counters. Lean's anonymous-constructor
# brackets ⟨⟩ are included so a comma/equals inside ⟨1, 2⟩ is not top-level.
_OPEN = "([{⟨"
_CLOSE = ")]}⟩"
_BLOCK_COMMENT_RE = re.compile(r"/-.*?-/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_PREDICATE_TYPE_RE = re.compile(r"→\s*Prop\s*$")
_DECL_NAME_RE = re.compile(r"\s*(theorem|lemma)\s+([\w'.]+)")
_JUNK_NAME_RE = re.compile(r"(?i)(placeholder|todo|dummy|stub|tmp|temp)(_[\w']*)?")
_SORRY_RE = re.compile(r"(?<![\w'])sorry(?![\w'])")
_BY_HEAD_RE = re.compile(r"^by\b\s*")


def has_sorry(lean_code: str) -> bool:
    """True if ``lean_code`` contains a real ``sorry`` token (word-boundary match).

    Word-boundary, so identifiers/strings that merely contain the substring
    (``sorryFree``, ``"no sorry here"``) are NOT flagged — unlike a bare ``in``.
    """
    return _SORRY_RE.search(lean_code) is not None


# A top-level Lean declaration head at the start of a line (after optional
# modifiers). Used to detect MULTI-declaration drafts: the proof-stripper and the
# triviality probe both reason about a SINGLE statement, so a helper `lemma`/`def`
# (or a leading comment containing `:=`) ahead of the real claim makes them inspect
# the wrong text — masking a vacuous claim and, worse, handing a false
# non-triviality certificate to a statement that was never actually probed.
_DECL_HEAD_RE = re.compile(
    r"(?m)^\s*(?:@\[[^\]]*\]\s*)?"
    r"(?:noncomputable\s+|private\s+|protected\s+|scoped\s+|local\s+|partial\s+|unsafe\s+)*"
    r"(?:theorem|lemma|example|def|abbrev|instance|structure|inductive|class)\b"
)


_CLAIM_HEAD_RE = re.compile(r"(?:@\[[^\]]*\]\s*)?(?:\w+\s+)*?(?:theorem|lemma|example)\b")


def declaration_count(lean_code: str) -> int:
    """Number of top-level declarations in a (comment-stripped) Lean draft."""
    code = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", lean_code))
    return len(_DECL_HEAD_RE.findall(code))


def claim_count(lean_code: str) -> int:
    """Number of top-level ``theorem``/``lemma``/``example`` declarations.

    Distinct from :func:`declaration_count`, which counts ``def``s and
    ``abbrev``s too. An item with several claims has no single statement to
    screen against its informal text; an item with one claim and several
    supporting definitions does.
    """
    code = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", lean_code))
    return len(
        [m for m in _DECL_HEAD_RE.finditer(code) if _CLAIM_HEAD_RE.match(m.group(0).strip())]
    )


def has_withheld_declaration(lean_code: str) -> bool:
    """True when a NON-theorem declaration is defined as ``sorry``.

    This separates the two things a multi-declaration item can mean, which
    matter in opposite directions when screening an external corpus:

    * ``abbrev foo_solution : ℝ := sorry`` withholds a value the theorem then
      references. The statement's content is genuinely absent, so faithfulness
      cannot be assessed and abstaining is right. PutnamBench uses this shape
      for every "determine the value" problem.
    * ``def tetration : ℕ → ℕ → ℕ | _, 0 => 1 | …`` is an auxiliary definition
      with a real body. The theorem together with its definitions IS the claim,
      and it is perfectly assessable.

    A trailing ``:= sorry`` on the theorem itself is the ordinary proof stub
    and is ignored, so only the non-theorem declarations are inspected.
    """
    code = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", lean_code))
    heads = list(_DECL_HEAD_RE.finditer(code))
    for i, head in enumerate(heads):
        if _CLAIM_HEAD_RE.match(head.group(0).strip()):
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(code)
        body = code[head.end() : end]
        colon_eq = body.find(":=")
        if colon_eq >= 0 and _SORRY_RE.search(body[colon_eq:]):
            return True
    return False


def _strip_proof(code: str) -> str:
    """Cut the trailing proof at the first top-level ``:= by`` / ``:= sorry``.

    A simple ``:= sorry$`` regex misses real-tactic proofs (``:= by trivial``,
    ``:= by rfl``) and would leave the proof text inside the parsed signature,
    blinding every downstream check. ``:=`` tokens inside binders or ``let``
    bindings are skipped: only one followed by ``by``/``sorry`` ends a statement.
    """
    depth = 0
    for i, c in enumerate(code):
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif (
            depth == 0
            and c == ":"
            and code[i + 1 : i + 2] == "="
            and _PROOF_HEAD_RE.match(code, i + 2)
        ):
            return code[:i]
    return code


def is_statement_only(lean_code: str) -> bool:
    """True if a THEOREM draft's proof is only ``sorry`` (no real proof body).

    The certified theorem product ships statements, not proofs. A draft that
    typechecks because it carries a real proof (``:= by simp``, ``:= fun x => h``,
    ``:= rfl``) is not a statement pair and must never reach the export. Returns
    True only for ``:= sorry`` / ``:= by sorry`` (modulo whitespace/comments), and
    False for any real proof term or when no trailing ``:= by/sorry`` is found.

    Definitions are NOT covered here — a ``def`` legitimately has a body, is not a
    proof, and is exported on a separate, proof-free path.
    """
    code = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", lean_code))
    sig = _strip_proof(code)
    if sig == code:  # no top-level ':= by/sorry' — a bare term proof or malformed
        return False
    body = _BY_HEAD_RE.sub("", code[len(sig) + 2 :].strip())  # drop ':=', then leading 'by'
    return body == "sorry"


def is_vacuous(lean_code: str) -> tuple[bool, str]:
    """Return (is_vacuous, reason). Reason is '' when not vacuous."""
    code = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", lean_code))
    # A sellable pair is ONE informal statement -> ONE Lean theorem. A draft with
    # a second declaration (a helper lemma, a `def`, an `example`) is rejected:
    # every check below reasons about a single declaration, and a helper ahead of
    # the real claim makes `_strip_proof` cut in the wrong place (hiding a vacuous
    # claim and corrupting the triviality probe). The model is told to inline any
    # needed definition, so a multi-declaration draft is itself a red flag.
    if len(_DECL_HEAD_RE.findall(code)) > 1:
        return (True, "multiple-declarations")
    sig = _strip_proof(code.strip()).strip()
    colon = _first_top_colon(sig)
    if colon < 0:
        return (False, "")
    # A `sorry` left INSIDE the statement (not the trailing proof, already stripped)
    # means the stated object is undefined — the claim is ill-formed, not sellable.
    if _SORRY_RE.search(sig):
        return (True, "sorry-in-statement")
    # A junk-named declaration is the drafter surrendering (`theorem placeholder
    # : False`, `lemma todo_1 …`) — never a faithful formalization of anything.
    # 42 such drafts exist historically; 32 were caught only by the PAID aid.
    name_match = _DECL_NAME_RE.match(sig)
    if name_match and _JUNK_NAME_RE.fullmatch(name_match.group(2)):
        return (True, "placeholder-name")
    goal = _strip_outer_parens(_collapse(sig[colon + 1 :]))
    if not goal:
        return (False, "")
    binders = sig[:colon]
    # A NAKED `: False` — no hypotheses at all — is a surrender draft. A real
    # contradiction statement keeps its premises (`(h₁ : …) : False` or
    # `: P → False`) and is spared: only the bare, premise-free form is junk.
    if goal == "False" and not _binder_types(binders):
        return (True, "bare-false")
    # A hypothesis whose body is literally `True` is a dropped premise — the model
    # replaced the real assumption with a placeholder, gutting the statement.
    for typ in _binder_types(binders):
        if _final_consequent(typ) == "True":
            return (True, "true-hypothesis")
    # Check the goal AND its final consequent (peeling `∀ …,` and `… →` prefixes),
    # so cheats hidden under quantifiers/implications are still caught.
    for candidate in (goal, _final_consequent(goal)):
        if candidate == "True":
            return (True, "goal-is-True")
        if _is_reflexive(candidate):
            return (True, "reflexive-goal")
    if goal in _binder_types(binders):
        return (True, "conclusion-is-hypothesis")
    # `∃ x, a = x` (or `x = a`) is trivially true — witness is the other side.
    if _is_trivial_existential(goal):
        return (True, "trivial-existential")
    consequent = _final_consequent(goal)
    if _is_over_abstracted(consequent, binders) or _has_free_carrier(consequent, binders):
        return (True, "over-abstracted")
    return (False, "")


def _has_free_carrier(consequent: str, binders: str) -> bool:
    """Flag laundering through a free *data* parameter (the dominant real cheat).

    The model parameterizes a paper-specific object (a graph, a divisor count, a
    predicate like "BCC prime") as an arbitrary binder — ``(G C : Finset …)``,
    ``(commonRoots : … → ℕ)``, ``(BCCPrime : ℕ → Prop)`` — and states a claim
    about it. The claim is then vacuous/false because the object is unconstrained.

    We flag a binder whose type is a *carrier* (a function ``→``, a ``Prop``, or a
    ``Finset``/``Set``) that is USED in the conclusion and is NOT *pinned* by an
    equational hypothesis (``name … = …``). Pinning makes it concrete and faithful
    (e.g. ``(φ : … → ℕ) (hφ : ∀ x, φ x = orderOf x)``), so those are spared.
    """
    pinned = _pinned_names(binders)
    for name, typ in _binder_name_types(binders):
        if name in pinned or not _is_carrier_type(typ):
            continue
        if re.search(rf"(?<![\w'.]){re.escape(name)}(?![\w'])", consequent):
            return True
    return False


def _is_carrier_type(typ: str) -> bool:
    """A type that *carries* meaning: a function, a bare ``Prop``, or a finite set."""
    t = typ.strip()
    if t == "Prop":
        return True
    # A function arrow `→` or a dependent Pi-type `∀ q, …` is a free function.
    if t.startswith("∀") or _first_top_sep(t, "→") >= 0:
        return True
    head = t.split()[0] if t.split() else ""
    return head in {"Finset", "Set", "Multiset"}


def _pinned_names(binders: str) -> set[str]:
    """Names *defined* by an equational hypothesis ``name … = …`` (so: concrete).

    The equation must genuinely DEFINE the head symbol: its left side must start
    with the name and must not be gated behind an implication. A one-directional
    `name x → … = …` only constrains the symbol — it does not pin it — so such a
    hypothesis (e.g. ``∀ p, BCCPrime p → … p = a^2 + b^2``) does NOT count.
    """
    pinned: set[str] = set()
    for _name, typ in _binder_name_types(binders):
        body = _peel_quantifiers(typ)
        idx = _first_top_sep(body, "=")
        if idx < 0:
            continue
        lhs = body[:idx]
        if _first_top_sep(lhs, "→") >= 0:  # the `=` is in an implication's consequent
            continue
        tokens = lhs.split()
        if tokens:
            pinned.add(tokens[0].strip("()"))
    return pinned


def _peel_quantifiers(goal: str) -> str:
    """Peel leading ``∀ …,`` / ``∃ …,`` binders only (keep ``→`` antecedents)."""
    g = goal.strip()
    while g.startswith(("∀", "∃")):
        comma = _first_top_comma(g)
        if comma < 0:
            break
        g = g[comma + 1 :].strip()
    return g


_BOUND_NAME_RE = re.compile(r"^[A-Za-z_][\w'!?₀-₉]*$")


def _is_trivial_existential(goal: str) -> bool:
    """True for ``∃ x …, a = x`` where one side IS a bound var absent from the other.

    Two shapes look trivial and are not. Firing on either rejects a faithful
    draft, and since screeners may only reject, that cost is silent:

    * **An ascribed binder may sit across a coercion.** ``∃ q : ℚ, ‖z‖ = q``
      is the standard way to say a real number is rational, and ``‖z‖`` is a
      witness only if it really is rational, which is the entire claim. Types
      cannot be inferred from source text, so an ascribed binder is left
      alone. The unascribed form this lint was written for (``∃ g, f = g``)
      is unaffected.
    * **A binder inside the body hides the witness.** ``∃ c, ∀ P, f P = c``
      says ``f`` is constant; ``c := f P`` is no witness, because ``P`` is
      quantified inside and the witness must be uniform in it.

    Measured before these guards: 14 false positives across two external
    corpora (6 of 322 in PutnamBench, 8 of 2,300 in ProofNetVerif), including
    Putnam 2025 B1, whose entire content is that a colouring is constant.
    """
    g = goal.strip()
    while True:
        if g.startswith("∀"):
            comma = _first_top_comma(g)
            if comma < 0:
                return False
            g = g[comma + 1 :].strip()
            continue
        arrow = _first_top_sep(g, "→")
        if arrow >= 0:
            g = g[arrow + len("→") :].strip()
            continue
        break
    if not g.startswith("∃"):
        return False
    comma = _first_top_comma(g)
    if comma < 0:
        return False
    binder = g[1:comma]
    if ":" in binder:
        # Ascribed: a coercion may stand between the two sides. See docstring.
        return False
    vars_ = binder.split()
    if not all(_BOUND_NAME_RE.match(v) for v in vars_):
        # Not a plain variable list. A bounded binder (`∃ x ∈ S`, `∃ x > 0`)
        # additionally demands the witness satisfy the bound, so the other
        # side is no witness on its own. Splitting on whitespace would also
        # read the bound's literals (`Set.Icc 0 1`) as variable names and
        # match them against a `0` on the right.
        return False
    body = _strip_outer_parens(g[comma + 1 :].strip())
    if "∀" in body or "∃" in body:
        # A quantifier in the body means the witness must be uniform in a
        # variable it cannot depend on.
        return False
    idx = _first_top_sep(body, "=")
    if idx < 0:
        return False
    left = _strip_outer_parens(_collapse(body[:idx]))
    right = _strip_outer_parens(_collapse(body[idx + 1 :]))
    for v in vars_:
        if left == v and not re.search(rf"(?<![\w'.]){re.escape(v)}(?![\w'])", right):
            return True
        if right == v and not re.search(rf"(?<![\w'.]){re.escape(v)}(?![\w'])", left):
            return True
    return False


def _is_over_abstracted(consequent: str, binders: str) -> bool:
    """Flag the clearest over-abstraction cheats (conservative, high precision):

    * the conclusion is ``P ↔ Q`` / ``P = Q`` / ``P`` where P, Q are free
      ``Prop`` parameters, or
    * the conclusion's head is a free predicate parameter (type ``… → Prop``).

    A *specific* claim never binds its objects as bare ``Prop``/predicate
    parameters, so this does not flag genuine statements.
    """
    name_types = _binder_name_types(binders)
    prop_names = {n for n, t in name_types if t == "Prop"}
    pred_names = {n for n, t in name_types if _PREDICATE_TYPE_RE.search(t)}

    # (a) bare free-Prop claim
    if prop_names:
        if consequent in prop_names:
            return True
        for sep in ("↔", "="):
            idx = _first_top_sep(consequent, sep)
            if idx >= 0:
                left = _strip_outer_parens(_collapse(consequent[:idx]))
                right = _strip_outer_parens(_collapse(consequent[idx + len(sep) :]))
                if left in prop_names and right in prop_names:
                    return True
    # (b) conclusion head is a free predicate parameter
    head = consequent.split()[0] if consequent.split() else ""
    return head in pred_names


def _binder_name_types(binders: str) -> list[tuple[str, str]]:
    """Yield (name, type) for each binder, expanding ``(a b : T)`` to a, b."""
    pairs: list[tuple[str, str]] = []
    depth = 0
    start = -1
    for i, c in enumerate(binders):
        if c in _OPEN:
            if depth == 0:
                start = i
            depth += 1
        elif c in _CLOSE:
            depth -= 1
            if depth == 0 and start >= 0:
                inner = binders[start + 1 : i]
                ci = _first_top_colon(inner)
                if ci >= 0:
                    typ = _collapse(inner[ci + 1 :])
                    for name in inner[:ci].split():
                        pairs.append((name, typ))
                start = -1
    return pairs


def _final_consequent(goal: str) -> str:
    """Peel leading ``∀ …,`` / ``∃ …,`` binders and ``A →`` antecedents."""
    g = goal.strip()
    changed = True
    while changed:
        changed = False
        if g.startswith(("∀", "∃")):
            comma = _first_top_comma(g)
            if comma >= 0:
                g = g[comma + 1 :].strip()
                changed = True
                continue
        arrow = _first_top_sep(g, "→")
        if arrow >= 0:
            g = g[arrow + len("→") :].strip()
            changed = True
    return _strip_outer_parens(g)


def _first_top_comma(s: str) -> int:
    depth = 0
    for i, c in enumerate(s):
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == "," and depth == 0:
            return i
    return -1


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _strip_outer_parens(s: str) -> str:
    """Remove balanced outer parentheses (``(True)`` -> ``True``)."""
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        balanced = True
        for j, c in enumerate(s):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and j < len(s) - 1:
                    balanced = False
                    break
        if balanced and depth == 0:
            s = s[1:-1].strip()
        else:
            break
    return s


def _first_top_colon(s: str) -> int:
    """Index of the goal-separating ``:`` (depth 0, not part of ``:=``)."""
    depth = 0
    for i, c in enumerate(s):
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == ":" and depth == 0:
            if i + 1 < len(s) and s[i + 1] == "=":
                continue  # ":=" (e.g. a `let`) — not the goal colon
            return i
    return -1


def _first_top_sep(s: str, sep: str) -> int:
    """Index of the first ``sep`` at bracket depth 0 (guards ``=`` vs ``:=``/``==``)."""
    depth = 0
    n = len(s)
    i = 0
    while i <= n - len(sep):
        c = s[i]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif depth == 0 and s[i : i + len(sep)] == sep:
            if sep == "=":
                prev = s[i - 1] if i > 0 else " "
                nxt = s[i + 1] if i + 1 < n else " "
                if prev in ":=<>!≤≥≠" or nxt == "=":
                    i += 1
                    continue
            return i
        i += 1
    return -1


def _is_reflexive(goal: str) -> bool:
    """True if the goal is ``X ↔ X`` or ``X = X`` (both sides identical)."""
    for sep in ("↔", "="):
        idx = _first_top_sep(goal, sep)
        if idx >= 0:
            left = _strip_outer_parens(_collapse(goal[:idx]))
            right = _strip_outer_parens(_collapse(goal[idx + len(sep) :]))
            if left and left == right:
                return True
    return False


def _binder_types(binders: str) -> list[str]:
    """Extract the normalized TYPE of each ``(... : TYPE)`` / ``{... : TYPE}`` binder."""
    types: list[str] = []
    depth = 0
    start = -1
    for i, c in enumerate(binders):
        if c in _OPEN:
            if depth == 0:
                start = i
            depth += 1
        elif c in _CLOSE:
            depth -= 1
            if depth == 0 and start >= 0:
                inner = binders[start + 1 : i]
                ci = _first_top_colon(inner)
                if ci >= 0:
                    types.append(_strip_outer_parens(_collapse(inner[ci + 1 :])))
                start = -1
    return types
