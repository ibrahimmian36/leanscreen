# Build prompt — extract the screen into `leanscreen`, ready for public release

> This is the brief the extraction was executed against (2026-07-28). It is
> written so a fresh Claude Code session could run it end to end; it also
> records every decision made, so future maintainers know what was deliberate.

---

I want the MCP faithfulness screener extracted from the private
`arxiv-math-platform` repo into a standalone, publicly publishable package.
The private repo stays the product; the public package is the funnel.

## What the product is

`leanscreen`: one pip-installable package, one console script (`leanscreen`),
serving the two MCP tools that already exist — `check_fast` (free,
deterministic: lints + vacuity + Lean elaboration) and `check_deep` (paid:
dual-judge strict consensus + counterexample probe, on the user's own
`ANTHROPIC_API_KEY`). Local-first, single-user, no hosting, no auth, no
telemetry.

## What moves, what stays private

**Moves** (the screen subtree — it depends only on `httpx` at runtime):
`llm.py`, `backtranslate.py` (judge prompts), `lints.py`, `quality.py`,
`falsify.py`, `verify.py`, `lean_server.py`, `logging.py`, the single-pair
`score_pair` core of `score_external.py`, and the MCP server. Unit tests for
each module come along.

**Stays private** (the commercial harness and everything upstream of it):
the batch layer (`load_pairs` / `run` / `render_report` / budget rails /
crash-safe resume), the numeric cross-evaluation probe and its sandbox, the
whole data platform (ingestion, extraction, DB, review UI, export,
benchmark, grader), the 886-verdict calibration set, and every census
artifact. The public package quotes the calibration *numbers*; the data that
produced them is the moat and never ships.

## Decisions (made, revisit deliberately)

- **Name**: `leanscreen`; package `leanscreen`, script `leanscreen`, MCP
  server name stays `lean-faithfulness-screen`.
- **License**: FSL-1.1-Apache-2.0 (revised from ELv2 on 2026-07-28 after
  license research, on Ibby's request). Why FSL won: (1) its Competing Use
  clause covers "any other product or service we offer using the Software" —
  protecting the certification/screening *service*, where ELv2 only blocks
  hosted versions of the software itself; (2) Permitted Purposes explicitly
  allow internal use, non-commercial education, and research — the mathlib
  audience; (3) each version converts to Apache 2.0 (mathlib's license)
  after 2 years, the best community optics available, and a 2-year-old
  screener without current calibration is not a competitive threat; (4) it
  is simpler than BUSL, whose Additional Use Grant ambiguity is exactly what
  Sentry created FSL to fix. (Not legal advice; have a lawyer confirm before
  anything heavy rides on it.)
- **Env prefix**: `LEANSCREEN_` (`LEAN_PROJECT_PATH`, `LEAN_REPL_PATH`,
  `ANTHROPIC_MODEL`, `JUDGE_B_MODEL`, `MAX_TOKENS`, …). A fresh, slim
  `Settings` — none of the platform's 50 knobs.
- **Judge B is now a setting** (`LEANSCREEN_JUDGE_B_MODEL`, default
  `claude-fable-5` = the calibrated configuration) instead of a hard-coded
  constant; the locked-surface 32k max_tokens rule keys off the value.
- **Numeric probe dropped** from the public `score_pair` (the MCP tools never
  used it; smaller surface, smaller audit).
- **`infer_kind` made public** (was `_infer_kind`).
- **Fork policy**: this is a fork-with-provenance, not a shared library. The
  private repo does NOT depend on the public package. When a detector
  improves in one repo, port it to the other deliberately; the commit
  message must name the source commit.

## Non-negotiable constraints (carried over verbatim)

- The screen may only reject — never certify. `CALIBRATION_DISCLOSURE` ships
  verbatim in every payload; both tool descriptions state passing is not
  certification (test-pinned).
- Evidence tiers stay ranked, never flattened: counterexample >
  deterministic > two-judge-consensus > single-judge (below the reporting
  bar).
- Every flag is a candidate, not a verdict.
- No secrets, no internal paths, no customer or reviewer names anywhere in
  the public tree — audit before every push.

## Quality gate

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest
```

Strict mypy, zero errors, same bar as the private repo.

## Acceptance criteria

1. `pip install -e .` in a fresh venv pulls only httpx, pydantic(-settings),
   and mcp (plus their transitive deps) — none of the platform's heavy deps.
2. The full test suite passes; the MCP-server tests are the same ones that
   pass in the private repo, re-pointed.
3. `leanscreen` starts over stdio with no Lean project and no API key and
   answers `check_fast`, exactly as the private server does.
4. README works for a stranger: REPL build walkthrough, generic paths,
   config snippets for Claude Code / Claude Desktop, the calibration table,
   the ELv2 summary, and a contact for certification engagements.
5. A pre-publish audit pass over the whole tree: secret scan, internal-path
   scan, license header presence, README claims match measured numbers.

## Steps reserved for Ibby (do not automate)

1. Confirm the license choice and the public contact address.
2. `gh repo create` (public) + first push — publishing is a send.
3. Any PyPI upload, registry listing, or announcement post (10am rule).

---

*Executed 2026-07-28. Origin: arxiv-math-platform @ 0f57167 (commits e39d2f9
+ 0f57167 are the MCP server and the proof-stub fix this package forked
from).*
