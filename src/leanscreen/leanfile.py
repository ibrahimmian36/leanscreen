"""Extract docstring-paired declarations from a Lean 4 source file.

A pragmatic scanner, not a parser. This feeds a *screen*, so the rule is:
prefer skipping something odd over mis-pairing it. What it does handle:

* ``/-- ... -/`` doc comments, including nested ``/- ... -/`` inside them;
* the declaration the doc comment documents: ``theorem``/``lemma``/
  ``example``/``def``/``abbrev``/``instance``, with any combination of
  ``protected``/``private``/``noncomputable`` prefixes and ``@[...]``
  attribute lines between the doc comment and the keyword;
* multi-line signatures: the statement runs from the declaration keyword up
  to (not including) its top-level ``:=``, ``by``, or ``where`` body, else to
  the end of the declaration's line block (a blank line, an unindented new
  declaration/doc comment/attribute, or EOF).

Deliberately NOT handled (skipped, never mis-paired): ``structure``,
``inductive``, ``class``, ``mutual`` blocks, macros/notation, and any doc
comment not immediately followed by a recognized declaration.
``private``/``protected`` are stripped from the extracted statement (they do
not elaborate standalone); ``noncomputable`` is kept (removing it changes
elaboration).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Declaration head: optional modifiers, then a recognized keyword. The
# lookahead stops `definitely`, `by'`, `theorem_ext` etc. from matching.
_DECL_RE = re.compile(
    r"(?:(?:protected|private|noncomputable)[ \t]+)*"
    r"(?P<kw>theorem|lemma|example|def|abbrev|instance)(?![\w'!?])"
)
_VISIBILITY_RE = re.compile(r"^(?:(?:protected|private)[ \t]+)+")
_NAME_RE = re.compile(r"«[^»]*»|[^\s:(\[{⦃⟨]+")


@dataclass(frozen=True, slots=True)
class DocDecl:
    """One docstring-paired declaration: the screen's (informal, lean) input."""

    name: str
    informal: str
    lean: str
    line: int  # 1-based line of the declaration keyword


@dataclass(frozen=True, slots=True)
class Extraction:
    """Everything the scanner found in one file."""

    decls: tuple[DocDecl, ...]
    skipped_no_docstring: int  # recognized declarations with no doc comment
    skipped_unrecognized: int  # doc comments whose declaration we won't pair


def extract_documented(text: str) -> Extraction:
    """Scan Lean source text for ``/-- ... -/`` + declaration pairs."""
    decls: list[DocDecl] = []
    skipped_no_doc = 0
    skipped_unrecognized = 0
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("/--", i):
            informal, after_doc = _read_doc_comment(text, i)
            if not informal:
                # An empty doc comment documents nothing; the declaration (if
                # any) will be counted below as having no docstring.
                i = after_doc
                continue
            decl_start = _skip_trivia(text, after_doc)
            decl = _read_declaration(text, decl_start, informal)
            if decl is None:
                skipped_unrecognized += 1
                i = decl_start  # always past the doc comment: the scan advances
            else:
                decls.append(decl[0])
                i = decl[1]
            continue
        if text.startswith("/-", i):
            i = _skip_block_comment(text, i)
            continue
        if text.startswith("--", i):
            i = _skip_line_comment(text, i)
            continue
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if _at_line_start(text, i):
            match = _DECL_RE.match(text, i)
            if match is not None:
                skipped_no_doc += 1
                i = _statement_end(text, match.end())
                continue
        i += 1
    return Extraction(
        decls=tuple(decls),
        skipped_no_docstring=skipped_no_doc,
        skipped_unrecognized=skipped_unrecognized,
    )


def _read_doc_comment(text: str, i: int) -> tuple[str, int]:
    """From ``i`` at ``/--``: the stripped body and the index past ``-/``.

    Lean block comments nest, so ``/- ... -/`` inside the doc comment is
    tracked with a depth counter rather than ended at the first ``-/``.
    """
    j = i + 3
    start = j
    depth = 1
    n = len(text)
    while j < n:
        if text.startswith("/-", j):
            depth += 1
            j += 2
        elif text.startswith("-/", j):
            depth -= 1
            j += 2
            if depth == 0:
                return text[start : j - 2].strip(), j
        else:
            j += 1
    return text[start:].strip(), n  # unterminated comment: tolerate, take the rest


def _skip_trivia(text: str, i: int) -> int:
    """Skip whitespace, ``@[...]`` attributes, and ordinary comments, but stop
    at a fresh ``/--`` (a new doc comment is a new pairing, never trivia)."""
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("@[", i):
            i = _skip_brackets(text, i + 1)
        elif text.startswith("/--", i):
            return i
        elif text.startswith("/-", i):
            i = _skip_block_comment(text, i)
        elif text.startswith("--", i):
            i = _skip_line_comment(text, i)
        else:
            return i
    return n


def _read_declaration(text: str, i: int, informal: str) -> tuple[DocDecl, int] | None:
    """The declaration at ``i`` as a :class:`DocDecl`, plus the resume index.

    ``None`` when whatever follows the doc comment is not a recognized
    declaration shape (skip it rather than mis-pair it).
    """
    match = _DECL_RE.match(text, i)
    if match is None:
        return None
    end = _statement_end(text, match.end())
    statement = _VISIBILITY_RE.sub("", text[match.start() : end].rstrip())
    if not statement:
        return None
    name = _decl_name(text, match.end()) or match.group("kw")
    line = text.count("\n", 0, match.start()) + 1
    return DocDecl(name=name, informal=informal, lean=statement, line=line), end


def _decl_name(text: str, i: int) -> str | None:
    """The identifier after the keyword; ``None`` for anonymous declarations
    (``example``, ``instance : Foo``)."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    match = _NAME_RE.match(text, i)
    if match is None:
        return None
    return match.group().rstrip(".")


def _statement_end(text: str, i: int) -> int:
    """Scan from just past the keyword to where the statement text ends.

    Ends at a top-level (bracket depth 0) ``:=``, ``by``, or ``where`` (the
    proof/definition body) or, failing that, at the end of the declaration's
    line block: a blank line, an unindented new declaration / doc comment /
    attribute, or EOF.
    """
    depth = 0
    n = len(text)
    while i < n:
        if text.startswith("--", i):
            i = _skip_line_comment(text, i)
            continue
        if text.startswith("/-", i):
            i = _skip_block_comment(text, i)
            continue
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0:
            if text.startswith(":=", i):
                return i
            if _word_at(text, i, "by") or _word_at(text, i, "where"):
                return i
            if ch == "\n" and _block_ends_after_newline(text, i):
                return i
        i += 1
    return n


def _block_ends_after_newline(text: str, i: int) -> bool:
    """True when the line after the newline at ``i`` starts a new block."""
    j = i + 1
    n = len(text)
    k = j
    while k < n and text[k] in " \t":
        k += 1
    if k >= n or text[k] in "\r\n":
        return True  # blank line (or EOF)
    # Unindented next line starting a sibling declaration (not a continuation).
    return k == j and (
        _DECL_RE.match(text, j) is not None or text.startswith(("/--", "/-!", "@["), j)
    )


def _word_at(text: str, i: int, word: str) -> bool:
    if not text.startswith(word, i):
        return False
    before = text[i - 1] if i > 0 else " "
    after_index = i + len(word)
    after = text[after_index] if after_index < len(text) else " "
    return not _is_ident_char(before) and not _is_ident_char(after)


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_'!?."


def _at_line_start(text: str, i: int) -> bool:
    """Only whitespace between the previous newline and ``i``."""
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    return j < 0 or text[j] == "\n"


def _skip_string(text: str, i: int) -> int:
    """From ``i`` at ``\"``: the index past the closing quote."""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
        elif text[j] == '"':
            return j + 1
        else:
            j += 1
    return n


def _skip_block_comment(text: str, i: int) -> int:
    """From ``i`` at ``/-``: the index past the matching (nested) ``-/``."""
    depth = 0
    n = len(text)
    while i < n:
        if text.startswith("/-", i):
            depth += 1
            i += 2
        elif text.startswith("-/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def _skip_line_comment(text: str, i: int) -> int:
    end = text.find("\n", i)
    return len(text) if end == -1 else end


def _skip_brackets(text: str, i: int) -> int:
    """From ``i`` at ``[``: the index past the matching ``]``."""
    depth = 0
    n = len(text)
    while i < n:
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n
