"""Unit tests for the vacuous-statement detector, using REAL observed drafts."""

from __future__ import annotations

import pytest

from leanscreen.quality import (
    declaration_count,
    has_withheld_declaration,
    is_statement_only,
    is_vacuous,
)

# Cheats observed in the first real arXiv batch — must be flagged vacuous.
_VACUOUS = [
    # goal is literally True
    "theorem markoff (p : ℕ) (hp : p.Prime) (d : ℕ) : True := by sorry",
    "theorem t (c60 c50 : ℝ) (h : c60 = 0) : True := by\n  sorry",
    # True ↔ True
    "theorem conjectures_equivalent : True ↔ True := by sorry",
    # reflexive X = X
    "theorem g : SimpleGraph.connectedComponentMk h (1 : ZMod p) = "
    "SimpleGraph.connectedComponentMk h (1 : ZMod p) := by sorry",
    # conclusion identical to a hypothesis (assume-and-conclude)
    "theorem every (p : ℕ) (h : ∀ t : Fin 3 → ℕ, L p ≤ order t → connected t (C p)) : "
    "∀ t : Fin 3 → ℕ, L p ≤ order t → connected t (C p) := by sorry",
    # reflexive X = X hidden under a ∀ … → … chain
    "theorem g : ∀ p : ℕ, p.Prime → p < 1000000 → (f (1 : ZMod p)) = (f (1 : ZMod p)) := by sorry",
    # goal is True but with an inline comment before it
    "theorem t (d0 : ℝ) (h : d0 = 0) : -- placeholder for the four cases\n  True := by sorry",
    # over-abstraction: iff between bare free Prop parameters
    "theorem conjectures_equivalent (con_a : Prop) (con_b : Prop) : con_a ↔ con_b := by sorry",
    # over-abstraction: conclusion head is a free predicate parameter
    "theorem markoff (p d : ℕ) (hp : p.Prime) (connected : ℕ → ℕ → Prop) "
    "(hbound : (d : ℝ) > 5) : connected p d := by sorry",
    # free DATA carrier: G_p, C_p replaced by arbitrary finsets (claim is false)
    "theorem card_minus (p : ℕ) (hp : 3 < p) (G C : Finset (ℕ × ℕ × ℕ)) "
    "(hC : C ⊆ G) : 4 * p ∣ (G \\ C).card := by sorry",
    # free FUNCTION carrier: 'number of common roots' is an arbitrary function
    "theorem roots (F₁ F₂ : Polynomial ℚ) (commonRoots : Polynomial ℚ → Polynomial ℚ → ℕ) "
    ": commonRoots F₁ F₂ = 2 := by sorry",
    # free predicate carrier under a non-defining constraint (BCC undefined)
    "theorem bcc (BCCPrime : ℕ → Prop) (hBCC : ∀ n, BCCPrime n → n.Prime) : "
    "{p : ℕ | p.Prime ∧ BCCPrime p}.Infinite := by sorry",
    # conclusion restates a hypothesis, hidden under ∀ + a bound type ascription
    "theorem conn (G : ℕ → SimpleGraph ℕ) (hG : ∀ p, p.Prime → (G p).Connected) : "
    "∀ p : ℕ, p.Prime → (G p).Connected := by sorry",
    # True placeholder swapped in for the real hypothesis
    "theorem bound (p t : ℕ) (hp : p.Prime) (N : Finset (ZMod t)) "
    "(hN : ∀ n ∈ N, True) : (N.card : ℝ) ≤ 3 := by sorry",
    # trivially-true existential: witness is the other side
    "theorem fac (k : Type*) [Field k] (f : Polynomial k) : ∃ g, f = g := by sorry",
    # a `sorry` left inside the STATEMENT term (object undefined)
    "theorem surj (p : ℕ) : Function.Surjective "
    "(fun v : {v : ℤ // v ^ 2 = 0} => (⟨(v : ℤ), by sorry⟩ : {w : ℤ // w ^ 2 = 0})) := by sorry",
    # carrier 'pinned' only by a ONE-DIRECTIONAL `→` hypothesis is NOT pinned
    "theorem bcc2 (BCCPrime : ℕ → Prop) "
    "(hBCC : ∀ p, BCCPrime p → p.Prime ∧ ∃ a b : ℕ, p = a ^ 2 + b ^ 2) : "
    "{p : ℕ | BCCPrime p}.Infinite := by sorry",
    # free carrier written as a dependent Pi-type `∀ q, …` (conclusion follows from hyp)
    "theorem conn2 (p : ℕ) (hp : p.Prime) (V : ℕ → Type) (G : ∀ q, SimpleGraph (V q)) "
    "(hG : ∀ q, q.Prime → (G q).Connected) : (G p).Connected := by sorry",
    # cheats proved with REAL tactics (not sorry) must still be caught
    "theorem t : True := by trivial",
    "theorem g (p : ℕ) : (1 : ZMod p) = (1 : ZMod p) := by rfl",
    "theorem t2 (n : ℕ) (h : 0 < n) : 0 < n := by exact h",
]

# Genuine faithful drafts from the same batch — must NOT be flagged.
_GENUINE = [
    "theorem infinitely_many_bcc_primes : "
    "{p : ℕ | p.Prime ∧ ∃ a b : ℕ, p = 2 * a^2 + 2 * a * b + b^2}.Infinite := by sorry",
    "theorem zmod_cond (p : ℕ) [Fact p.Prime] (z : ZMod p) : "
    "(∃ x y : ZMod p, x^2 + y^2 + z^2 - x*y*z = 0) ↔ (z ≠ 2 ∨ ∃ w : ZMod p, w^2 = -1) := by sorry",
    "theorem divisor_little_o (ε : ℝ) (hε : 0 < ε) : "
    "(fun n : ℕ => (Nat.divisors n).card : ℕ → ℝ) =o[Filter.atTop] "
    "(fun n => (n : ℝ) ^ ε) := by sorry",
    # a real (non-reflexive) equality must pass
    "theorem coeffs (a b : ℤ) : a + b = b + a := by sorry",
    # a real iff between CONCRETE props (no free Prop binders) must pass
    "theorem markov {p : ℕ} [Fact p.Prime] (z : ZMod p) : "
    "(∃ x y : ZMod p, x ^ 2 = z) ↔ z ≠ 2 ∨ IsSquare (-1 : ZMod p) := by sorry",
    # a carrier PINNED by an equational hypothesis is concrete — must pass
    "theorem pinned (χ : ℕˣ) (φ : ℕˣ → ℕ) (hφ : ∀ x, φ x = orderOf x) : φ χ ∣ 6 := by sorry",
    # commonRoots pinned to the actual root set — must pass
    "theorem pinned_roots (F₁ F₂ : Polynomial ℚ) "
    "(commonRoots : Polynomial ℚ → Polynomial ℚ → Set ℚ) "
    "(hcr : ∀ P Q, commonRoots P Q = {x | P.eval x = 0 ∧ Q.eval x = 0}) : "
    "(commonRoots F₁ F₂).Finite := by sorry",
    # a genuine existential whose witness is NOT a bound var — must pass
    "theorem genex (n : ℕ) : ∃ p : ℕ, p.Prime ∧ n < p := by sorry",
    # a genuine statement with a REAL tactic proof must not be flagged
    "theorem add_zero' (n : ℕ) : n + 0 = n := by\n  simp",
    # Lean anonymous-constructor brackets: comma inside ⟨…⟩ is not top-level
    "theorem pair_mem (v : ℤ × ℤ) (h : v = ⟨1, 2⟩) : v.1 = 1 := by sorry",
    # --- shapes that LOOK like a trivial existential and are not. Each is a
    # false positive measured against an external corpus; see
    # _is_trivial_existential. The witness is not the other side, because a
    # coercion or an inner binder stands between them.
    #
    # "this real number is rational" (ProofNetVerif, Rudin 5.4 / Shakarchi 1.13)
    "theorem is_rat (z : ℂ) (hz : ‖z‖ = 1) (n : ℤ) : ∃ q : ℚ, ‖z ^ (2 * n) - 1‖ = q := by sorry",
    # "this real number is an integer" (PutnamBench 2004 A3)
    "theorem is_int (u : ℕ → ℝ) (hu : u 0 = 1) (n : ℕ) : ∃ m : ℤ, u n = m := by sorry",
    # "this colouring is constant" — the whole content of PutnamBench 2025 B1
    "theorem is_const (color : ℕ → Bool) (h : ∀ a b, color a = color b) : "
    "∃ c, ∀ P : ℕ, color P = c := by sorry",
    # unascribed existential whose bound var appears on BOTH sides of the
    # equation: not witnessed by the other side, must pass
    "theorem has_root (n : ℕ) (hn : 0 < n) : ∃ x, 0 ≤ x ∧ x * x = n := by sorry",
]


@pytest.mark.parametrize("code", _VACUOUS)
def test_flags_vacuous(code: str) -> None:
    flagged, reason = is_vacuous(code)
    assert flagged, f"should be vacuous but passed: {code}"
    assert reason


@pytest.mark.parametrize("code", _GENUINE)
def test_passes_genuine(code: str) -> None:
    flagged, reason = is_vacuous(code)
    assert not flagged, f"genuine draft wrongly flagged ({reason}): {code}"


def test_unparseable_is_not_flagged() -> None:
    # Conservative: if we cannot find a goal, do not reject.
    assert is_vacuous("not even a theorem") == (False, "")


def test_declaration_count() -> None:
    assert declaration_count("theorem t : True := by sorry") == 1
    assert declaration_count("def P := 1\ntheorem t : P = 1 := by sorry") == 2
    assert declaration_count("lemma a : X := by simp\n\ntheorem b : Y := by sorry") == 2
    # a keyword inside a comment is not a declaration
    assert declaration_count("-- theorem in a comment\ntheorem real : X := by sorry") == 1


@pytest.mark.parametrize(
    "code",
    [
        # a helper hides a vacuous main from the first-declaration-only proof stripper
        "lemma aux (n : ℕ) : n + 0 = n := by simp\n\ntheorem main : True := by sorry",
        # a def before the claim (the probe would otherwise grant a false certificate)
        "def P (n : ℕ) : Prop := n.Prime\n\ntheorem main : {p | P p}.Infinite := by sorry",
        # two theorems — a clean sellable pair is exactly one statement
        "theorem a (n : ℕ) : n = n := by rfl\n\ntheorem b (n : ℕ) : 0 ≤ n := by sorry",
    ],
)
def test_multi_declaration_is_flagged(code: str) -> None:
    flagged, reason = is_vacuous(code)
    assert flagged and reason == "multiple-declarations"


# A multi-declaration item means one of two opposite things on an external
# corpus, and `has_withheld_declaration` is what tells them apart. Screening
# treats the first as not assessable and the second as ordinary work.
@pytest.mark.parametrize(
    "code",
    [
        # the answer is withheld in a value declaration the theorem references
        "abbrev putnam_2004_a1_solution : Prop := sorry\n"
        "theorem putnam_2004_a1 : X ↔ putnam_2004_a1_solution := sorry",
        "noncomputable abbrev sol : ℝ := sorry\ntheorem t : f = sol := by sorry",
        # our own drafter's cheat: the referenced object is left undefined
        "def P (n : ℕ) : Prop := sorry\n\ntheorem main : {p | P p}.Infinite := by sorry",
    ],
)
def test_withheld_declaration_detected(code: str) -> None:
    assert has_withheld_declaration(code)


@pytest.mark.parametrize(
    "code",
    [
        # auxiliary definitions with real bodies — the claim is fully present
        "def tetration : ℕ → ℕ → ℕ | _, 0 => 1 | b, (m + 1) => b ^ (tetration b m)\n"
        "theorem putnam_1997_b5 (n : ℕ) (hn : n ≥ 2) : tetration 2 n ≡ tetration 2 (n-1) [MOD n] "
        ":= sorry",
        "def klimited (k n : ℕ) (s : Equiv.Perm (Fin n)) := ∀ i, |((s i) : ℤ) - i| ≤ k\n"
        "theorem t (n k : ℕ) : Odd (Set.ncard {s | klimited k n s}) := sorry",
        # a lone theorem with the ordinary trailing proof stub
        "theorem t (n : ℕ) : n + 0 = n := by sorry",
    ],
)
def test_auxiliary_definitions_are_not_withheld(code: str) -> None:
    assert not has_withheld_declaration(code)


# Statement-only drafts: the proof is just `sorry` — these are the sellable shape.
_STATEMENT_ONLY = [
    "theorem t : True := by sorry",
    "theorem t : True := sorry",
    "theorem t (n : ℕ) : n = n := by\n  sorry",
    "theorem t : True := by sorry  -- trailing comment",
    "theorem foo (h : a = b) : a = b := by sorry",
]

# Proof-carrying drafts: a real proof body — must never reach the certified export.
_PROOF_CARRYING = [
    "theorem t : True := by trivial",
    "theorem t (n : ℕ) : n = n := by rfl",
    "theorem t (n : ℕ) : 0 ≤ n := by simp",
    "theorem t : True := True.intro",  # term-mode proof, no `by`, no sorry
    "theorem t : True := by\n  exact True.intro",
    "theorem t : True := by sorry; trivial",  # sorry plus a real tactic
]


@pytest.mark.parametrize("code", _STATEMENT_ONLY)
def test_is_statement_only_accepts_sorry_statements(code: str) -> None:
    assert is_statement_only(code) is True, f"sorry statement wrongly blocked: {code}"


@pytest.mark.parametrize("code", _PROOF_CARRYING)
def test_is_statement_only_rejects_real_proofs(code: str) -> None:
    assert is_statement_only(code) is False, f"proof-carrying draft slipped through: {code}"


# The surrender-draft guard (batch-4 finding: `theorem placeholder : False` compiled
# and reached the paid aid; 42 junk drafts historically). Narrow by design.
_SURRENDER = [
    ("theorem placeholder : False := by sorry", "placeholder-name"),
    ("lemma todo_finish_this (n : ℕ) : n > 0 := by sorry", "placeholder-name"),
    ("theorem dummy_1 : 1 + 1 = 2 := by sorry", "placeholder-name"),
    ("theorem no_name : False := by sorry", "bare-false"),
]


@pytest.mark.parametrize(("code", "reason"), _SURRENDER)
def test_surrender_drafts_flagged(code: str, reason: str) -> None:
    vacuous, why = is_vacuous(code)
    assert vacuous and why == reason


# Legitimate contradiction statements MUST be spared: `hypotheses : False` is the
# natural formalization of "no such object exists" (4 of 5 sampled `False` drafts
# in production were real mathematics of this shape).
_CONTRADICTION_OK = [
    "theorem no_even_prime_above_two (p : ℕ) (hp : p.Prime) (h2 : 2 < p)"
    " (heven : 2 ∣ p) : False := by sorry",
    "theorem impossible : 2 + 2 = 5 → False := by sorry",
    "theorem tmpnotjunk_named_theorem (n : ℕ) (h : 0 < n) : n ≠ 0 := by sorry",
]


@pytest.mark.parametrize("code", _CONTRADICTION_OK)
def test_contradiction_statements_spared(code: str) -> None:
    vacuous, _ = is_vacuous(code)
    assert not vacuous
