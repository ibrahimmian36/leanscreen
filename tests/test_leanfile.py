"""The .lean docstring scanner: doc-comment pairing, statement cutting,
nesting, modifiers, and the skip-over-mis-pair rule."""

from __future__ import annotations

from leanscreen.leanfile import DocDecl, extract_documented

# A realistic slice of a mathlib-style file: docstrings, a multi-line
# signature, a same-line `:=` body, a nested comment, an undocumented
# theorem, and a `private` helper.
_FIXTURE = """\
import Mathlib

namespace Demo

/-- Every natural number greater than 2 is not equal to 2. -/
theorem gt_two_ne_two (n : ℕ) (h : 2 < n) : n ≠ 2 := by
  omega

/-- A multi-line signature:
the sum of two odd numbers is even. -/
theorem odd_add_odd
    (m n : ℕ) (hm : Odd m) (hn : Odd n) :
    Even (m + n) := by
  sorry

/-- The constant two. -/
def two : ℕ := 2

/-- A doc comment with a nested /- block comment -/ inside it. -/
lemma nested_ok : two = 2 := rfl

theorem undocumented (n : ℕ) : n = n := rfl

/-- A private helper lemma. -/
private theorem helper : 1 + 1 = 2 := by norm_num

end Demo
"""


def _by_name(decls: tuple[DocDecl, ...]) -> dict[str, DocDecl]:
    return {d.name: d for d in decls}


def test_fixture_pairs_every_documented_declaration() -> None:
    result = extract_documented(_FIXTURE)
    names = [d.name for d in result.decls]
    assert names == ["gt_two_ne_two", "odd_add_odd", "two", "nested_ok", "helper"]
    assert result.skipped_no_docstring == 1  # `undocumented` only
    assert result.skipped_unrecognized == 0


def test_statement_cut_before_proof_body() -> None:
    decl = _by_name(extract_documented(_FIXTURE).decls)["gt_two_ne_two"]
    assert decl.informal == "Every natural number greater than 2 is not equal to 2."
    assert decl.lean == "theorem gt_two_ne_two (n : ℕ) (h : 2 < n) : n ≠ 2"
    assert decl.line == 6


def test_multi_line_signature_kept_whole() -> None:
    decl = _by_name(extract_documented(_FIXTURE).decls)["odd_add_odd"]
    assert decl.informal.startswith("A multi-line signature:")
    assert decl.lean.startswith("theorem odd_add_odd")
    assert decl.lean.endswith("Even (m + n)")
    assert ":=" not in decl.lean
    assert "\n" in decl.lean  # the signature spans lines


def test_def_with_same_line_body_cut_at_assignment() -> None:
    decl = _by_name(extract_documented(_FIXTURE).decls)["two"]
    assert decl.lean == "def two : ℕ"
    assert decl.informal == "The constant two."


def test_nested_block_comment_inside_docstring() -> None:
    decl = _by_name(extract_documented(_FIXTURE).decls)["nested_ok"]
    assert decl.informal == "A doc comment with a nested /- block comment -/ inside it."
    assert decl.lean == "lemma nested_ok : two = 2"


def test_private_modifier_stripped_noncomputable_kept() -> None:
    decl = _by_name(extract_documented(_FIXTURE).decls)["helper"]
    assert decl.lean == "theorem helper : 1 + 1 = 2"
    text = "/-- A choice function. -/\nprotected noncomputable def pick : ℕ := 0\n"
    (only,) = extract_documented(text).decls
    assert only.lean == "noncomputable def pick : ℕ"


def test_bare_statement_ends_at_next_unindented_declaration() -> None:
    text = (
        "/-- A statement with no proof body. -/\n"
        "theorem bare (n : ℕ) : n ≠ n + 1\n"
        "/-- The next one. -/\n"
        "theorem next_one : True := trivial\n"
    )
    result = extract_documented(text)
    assert [d.name for d in result.decls] == ["bare", "next_one"]
    assert result.decls[0].lean == "theorem bare (n : ℕ) : n ≠ n + 1"


def test_anonymous_example_and_instance_named_by_keyword() -> None:
    text = (
        "/-- Truth is provable. -/\n"
        "example : True := trivial\n"
        "\n"
        "/-- Points are inhabited. -/\n"
        "instance : Inhabited Point where\n"
        "  default := origin\n"
    )
    result = extract_documented(text)
    assert [d.name for d in result.decls] == ["example", "instance"]
    assert result.decls[0].lean == "example : True"
    assert result.decls[1].lean == "instance : Inhabited Point"  # cut at `where`


def test_attribute_between_docstring_and_declaration() -> None:
    text = "/-- Simp-friendly. -/\n@[simp]\ntheorem s : 0 + 0 = 0 := rfl\n"
    (decl,) = extract_documented(text).decls
    assert decl.name == "s"
    assert decl.lean == "theorem s : 0 + 0 = 0"


def test_doc_comment_over_unrecognized_construct_is_skipped_not_mispaired() -> None:
    text = (
        "/-- A structure we do not screen. -/\n"
        "structure Point where\n"
        "  x : ℕ\n"
        "\n"
        "/-- Documented and screenable. -/\n"
        "theorem fine : True := trivial\n"
    )
    result = extract_documented(text)
    assert [d.name for d in result.decls] == ["fine"]
    assert result.skipped_unrecognized == 1


def test_decl_keyword_inside_string_or_comment_not_counted() -> None:
    text = (
        '/-- Uses a string. -/\ndef msg : String := "theorem not_a_decl : False"\n'
        "-- theorem also_not_a_decl : False\n"
        "/- block comment:\ntheorem still_not_a_decl : False\n-/\n"
    )
    result = extract_documented(text)
    assert [d.name for d in result.decls] == ["msg"]
    assert result.skipped_no_docstring == 0


def test_empty_docstring_declaration_counts_as_undocumented() -> None:
    result = extract_documented("/-- -/\ntheorem t : True := trivial\n")
    assert result.decls == ()
    assert result.skipped_no_docstring == 1


def test_empty_and_declaration_free_files() -> None:
    for text in ("", "import Mathlib\n\nnamespace Foo\nend Foo\n"):
        result = extract_documented(text)
        assert result.decls == ()
        assert result.skipped_no_docstring == 0
