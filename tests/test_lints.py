"""Unit tests for the advisory structural lints (docs/faithfulness-patterns.md)."""

from __future__ import annotations

from leanscreen.lints import lint_statement


def _codes(lean: str) -> set[str]:
    return {f.code for f in lint_statement(lean)}


# --- hypothesis-conjunct-in-conclusion (Family A1) --------------------------------


def test_flags_conclusion_conjunct_that_is_a_hypothesis() -> None:
    # chromatic_number_ge shape: first conjunct verbatim equals hypothesis hΛC.
    lean = (
        "theorem chromatic (Λ C : SimpleGraph V) "
        "(hΛC : Λ.chromaticNumber = C.chromaticNumber) : "
        "Λ.chromaticNumber = C.chromaticNumber ∧ 3 ≤ C.chromaticNumber := by sorry"
    )
    assert "hypothesis-conjunct-in-conclusion" in _codes(lean)


def test_whole_goal_equals_hypothesis_left_to_quality() -> None:
    # Single-conjunct case is quality.is_vacuous's `conclusion-is-hypothesis` reject;
    # the lint must not duplicate it.
    lean = "theorem t (p : ℕ) (h : p ≤ 3) : p ≤ 3 := by sorry"
    assert "hypothesis-conjunct-in-conclusion" not in _codes(lean)


def test_distinct_conjuncts_not_flagged() -> None:
    lean = "theorem t (n : ℕ) (h : 2 ≤ n) : 1 ≤ n ∧ n ≤ n * n := by sorry"
    assert "hypothesis-conjunct-in-conclusion" not in _codes(lean)


# --- unused-binder (Family C3, the dead-parameter tell) ---------------------------


def test_flags_unused_explicit_binder() -> None:
    # kerneled_cover shape: a declared subject never used anywhere.
    lean = (
        "theorem cover (K : Set ℝ) (r : ℝ) (hr : 0 < r) : ∀ x : ℝ, x ∈ Metric.ball x r := by sorry"
    )
    assert "unused-binder" in _codes(lean)
    assert any(f.detail.endswith(": K") for f in lint_statement(lean))


def test_binder_used_only_in_later_hypothesis_not_flagged() -> None:
    lean = "theorem t (g : ℕ) (n : ℕ) (hn : n < g) : 0 ≤ n := by sorry"
    assert "unused-binder" not in _codes(lean)


def test_implicit_and_instance_binders_exempt() -> None:
    # {n : ℕ} unused and [Fact p.Prime] are elaboration data, not subjects.
    lean = "theorem t {n : ℕ} (p : ℕ) [Fact p.Prime] (hp : 2 ≤ p) : p ≤ p * p := by sorry"
    assert "unused-binder" not in _codes(lean)


def test_explicit_binder_used_only_inside_implicit_group_not_flagged() -> None:
    # α is referenced only inside the implicit group {x : α}: still a real use.
    lean = "theorem foo (α : Type) {x : α} (h : P x) : Q x := by sorry"
    assert "unused-binder" not in _codes(lean)


def test_definition_shaped_input_returns_no_findings() -> None:
    lean = "def D : Prop := True"
    assert lint_statement(lean) == []


def test_underscore_binder_exempt() -> None:
    lean = "theorem t (_junk : ℕ) (p : ℕ) : p ≤ p + 1 := by sorry"
    assert "unused-binder" not in _codes(lean)


def test_descriptively_named_hypothesis_exempt() -> None:
    # Field-run false-positive class: hypothesis binders with descriptive
    # (non-h-prefixed) names — their Prop-shaped types mark them as hypotheses.
    lean = (
        "theorem t (p : ℕ) (positivity : 0 < p) (case1 : p ∈ Finset.range 9) "
        "(constructible_iff : p = 2 ↔ Even p) (existence : ∃ q, q ∣ p) : "
        "p ≤ p * p := by sorry"
    )
    assert "unused-binder" not in _codes(lean)


def test_unused_data_function_still_flagged() -> None:
    # `V → ℕ` contains no Prop symbol: a dead FUNCTION subject stays flagged.
    lean = "theorem t (f : V → ℕ) (p : ℕ) : p ≤ p + 1 := by sorry"
    assert "unused-binder" in _codes(lean)


# --- exists-unique-pinned-witness (Family A3) -------------------------------------


def test_flags_funext_pinned_exists_unique() -> None:
    # burning_map_unique shape: ∃! f pinned pointwise -> uniqueness is funext.
    lean = (
        "theorem unique_map (lam : V → ℕ) : "
        "∃! f : V → ℕ, (∀ v, f v = lam v) ∧ Monotone f := by sorry"
    )
    assert "exists-unique-pinned-witness" in _codes(lean)


def test_flags_directly_pinned_exists_unique() -> None:
    lean = "theorem t (a : ℕ) : ∃! x : ℕ, x = a + 1 := by sorry"
    assert "exists-unique-pinned-witness" in _codes(lean)


def test_substantive_exists_unique_not_flagged() -> None:
    lean = "theorem inv (a : ℝ) (ha : a ≠ 0) : ∃! x : ℝ, a * x = 1 := by sorry"
    assert "exists-unique-pinned-witness" not in _codes(lean)


# --- nat-floor-div / nat-truncated-sub (Family C4) --------------------------------


def test_flags_nat_division() -> None:
    # berndt shape: informal ⌈m/2⌉ rendered as nat m / 2 (a floor).
    lean = "theorem t (m p : ℕ) (h : m / 2 ≤ p) : p ≤ p ∧ m / 2 ≤ p + 1 := by sorry"
    assert "nat-floor-div" in _codes(lean)


def test_flags_risky_nat_subtraction_shapes() -> None:
    # literal-minus-var and var-minus-var are the shapes that truncate in practice.
    assert "nat-truncated-sub" in _codes("theorem t (m : ℕ) : 2 - m ≤ 2 := by sorry")
    assert "nat-truncated-sub" in _codes("theorem t (d n : ℕ) : d - n ≤ d := by sorry")
    assert "nat-truncated-sub" in _codes("theorem t (a : ℕ) : a - 5 ≤ a := by sorry")


def test_var_minus_one_suppressed() -> None:
    # `x - 1` is overwhelmingly range-guarded (p prime, n ≥ 1, …) — pure noise.
    lean = "theorem t (n : ℕ) (h : 1 ≤ n) : n - 1 ≤ n := by sorry"
    assert "nat-truncated-sub" not in _codes(lean)


def test_real_arithmetic_not_flagged() -> None:
    lean = "theorem t (x y : ℝ) (h : x / 2 ≤ y) : x - y ≤ y := by sorry"
    assert _codes(lean) & {"nat-floor-div", "nat-truncated-sub"} == set()


def test_literal_only_arithmetic_not_flagged() -> None:
    lean = "theorem t (n : ℕ) (h : 0 < n) : n ^ (3 - 1) ≤ n ^ 2 := by sorry"
    # 3 - 1 involves no ℕ variable; nothing to warn about.
    assert "nat-truncated-sub" not in _codes(lean)


# --- robustness -------------------------------------------------------------------


def test_clean_statement_yields_no_findings() -> None:
    lean = (
        "theorem clean (p : ℕ) (hp : p.Prime) (hodd : Odd p) : "
        "∃ x y : ZMod p, x ^ 2 + y ^ 2 = 1 := by sorry"
    )
    assert lint_statement(lean) == []


def test_unparseable_draft_returns_clean() -> None:
    assert lint_statement("this is not lean at all") == []
    assert lint_statement("") == []


def test_real_ascribed_division_not_flagged() -> None:
    # (N / 2 : ℝ) is real division via leaf coercion, not a ℕ floor — field-run
    # false positive the review flagged. Same for real-ascribed subtraction.
    assert "nat-floor-div" not in _codes(
        "theorem t (N : ℕ) (h : 0 < N) : (N / 2 : ℝ) ≤ (N : ℝ) := by sorry"
    )
    assert "nat-truncated-sub" not in _codes(
        "theorem t (n : ℕ) : (n - 2 : ℝ) ≤ (n : ℝ) := by sorry"
    )


def test_nat_ascribed_division_still_flagged() -> None:
    # (m / 2 : ℕ) is explicitly ℕ — still a floor.
    assert "nat-floor-div" in _codes("theorem t (m : ℕ) : (m / 2 : ℕ) ≤ m := by sorry")


def test_unascribed_nat_division_still_flagged() -> None:
    # No ascription context (division in the goal): the true positive survives.
    assert "nat-floor-div" in _codes("theorem t (m p : ℕ) : m / 2 ≤ p := by sorry")
    assert "nat-floor-div" in _codes("theorem t (d : ℕ) : d ≤ Nat.choose d (d / 2) := by sorry")


def test_true_leaf_def_lint() -> None:
    from leanscreen.lints import lint_definition

    junk = (
        "def OpenMeshBundle (n : ℕ) (M : Fin (n + 1) → Type*) : Prop :=\n"
        "  ∀ i : Fin n, ∃ _ : M i.castSucc → M i.succ, True"
    )
    flags = lint_definition(junk)
    assert [f.code for f in flags] == ["true-leaf-def"]

    clean = "def IsPerfect (n : ℕ) : Prop := ∑ d ∈ n.properDivisors, d = n"
    assert lint_definition(clean) == []
    # `True` inside an identifier or comment must not fire.
    assert lint_definition("def isTrueName (p : Prop) : Prop := p ∧ p") == []


def test_alias_def_and_partial_operation_lints() -> None:
    from leanscreen.lints import lint_definition

    alias = "def legendreSymbolDef (a : ℤ) (p : ℕ) [Fact (Nat.Prime p)] : ℤ := legendreSym p a"
    assert [f.code for f in lint_definition(alias)] == ["alias-def"]
    partial = (
        "noncomputable def modulusOfContinuity (f : ℝ → ℝ) : ℝ → ℝ := "
        "fun δ => sSup {y : ℝ | ∃ x x' : ℝ, |x - x'| ≤ δ ∧ y = |f x - f x'|}"
    )
    assert [f.code for f in lint_definition(partial)] == ["partial-operation-default"]
    # a real body applying its OWN parameter is not an alias
    own = "def Reaches {S : Type*} (P : S → S) (a b : S) : Prop := ∃ k : ℕ, P^[k] a = b"
    assert lint_definition(own) == []


# --- extremal completeness ----------------------------------------------------

_MIN_ASK = "What is the smallest positive integer that is both a cube and a fourth power? Show that it is 4096."
_BOUND_ONLY = (
    "theorem t (n : ℕ) (h₀ : 2 ≤ n) (h₁ : ∃ x, x^3 = n) (h₂ : ∃ t, t^4 = n) :\n  4096 ≤ n := sorry"
)


def test_extremal_fires_on_bound_only_min_ask() -> None:
    from leanscreen.lints import lint_extremal

    codes = [f.code for f in lint_extremal(_MIN_ASK, _BOUND_ONLY)]
    assert codes == ["extremal-incomplete"]


def test_extremal_suppressed_by_isleast() -> None:
    from leanscreen.lints import lint_extremal

    lean = (
        "theorem t (S : Set ℕ)\n"
        "  (hS : S = {n | 0 < n ∧ ∃ x, x ^ 3 = n ∧ ∃ t, t ^ 4 = n}) :\n"
        "  IsLeast S 4096 := sorry"
    )
    assert lint_extremal(_MIN_ASK, lean) == []


def test_extremal_suppressed_by_equality_goal() -> None:
    from leanscreen.lints import lint_extremal

    lean = "theorem t (x : ℝ) (h₀ : 2 * x = 10) : x = 5 := sorry"
    assert lint_extremal("Solve for x: 2x = 10. Show that it is 5.", lean) == []


def test_extremal_ignores_qualifier_and_name_uses() -> None:
    from leanscreen.lints import lint_extremal

    lean = "theorem t (n : ℕ) (h : 3 ≤ n) : 9 ≤ n ^ 2 := sorry"
    assert lint_extremal("Show that n^2 is at least 9 when n is at least 3.", lean) == []
    assert lint_extremal("The least common multiple of a and b is at most a * b.", lean) == []


def test_extremal_clean_on_unparseable() -> None:
    from leanscreen.lints import lint_extremal

    assert lint_extremal(_MIN_ASK, "not lean at all") == []
