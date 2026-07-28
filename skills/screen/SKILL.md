---
name: screen
description: Screen informal↔Lean 4 statement pairs for faithfulness defects. Use whenever writing, editing, translating, or reviewing Lean 4 theorem or definition statements that are meant to formalize informal mathematics: after drafting a statement, before committing formalizations, when auditing a benchmark file, or when the user asks to check whether a Lean statement says what the mathematics says.
---

# Screening Lean statements for faithfulness

A Lean statement that compiles is not the same as a Lean statement that says
what the English says. This plugin's MCP server (`leanscreen`) provides two
tools that screen informal↔formal pairs for that gap. Your job is to use them
habitually, and to report what they say honestly.

## The invariant, verbatim

A pass is "no defect found by this harness". It is never a certification of
faithfulness. Automated screeners may only reject. Only a human certifies.
Never tell the user a statement "is faithful," "is correct," or "passed
verification" because it passed screening. Say what was checked and what was
found.

## When to screen

**After drafting.** Whenever you write or materially edit a Lean 4 statement
that is meant to mean something informal (a theorem from a paper, a
formalized definition, a benchmark item), call `check_fast` on the pair
(informal text + Lean statement). It is deterministic, free, local, and ~0.1s
once the REPL is warm. There is no reason to skip it. Treat every flag as a
candidate: fix what the lint names, re-run, iterate until clean or until the
flag is understood and deliberately accepted.

**Before anything ships.** When formalizations are about to be committed to a
dataset, submitted to a benchmark, or put in a PR, offer `check_deep` on the
final statements. It adds two independent LLM judges under strict consensus
and an adversarial counterexample probe, running on the user's own
`ANTHROPIC_API_KEY` at a measured $0.17–0.27 per statement. **Ask before
running it on more than ~5 statements, state the estimated cost, and never
loop it silently.** Report the actual spend from `actual_cost_usd`.

## How to read results

Rank evidence by tier, strongest first: **counterexample** > **deterministic
lint/vacuity** > **two-judge consensus** > single-judge (below the reporting
bar; mention it only as "one judge dissented," never as a finding).

Relay the calibration honestly when summarizing: measured against 886 frozen
human verdicts, human-rejected pairs still passed the full screen 17.0% of
the time for theorems and 35.6% for definitions, and human-certified pairs
were flagged 15–18% of the time. The counterexample probe can confabulate.
Check its counterexamples by hand before repeating them as fact.

## Explicit invocation

When invoked as `/leanscreen:screen $ARGUMENTS`:

- If `$ARGUMENTS` names a file: read it, extract the informal↔Lean pairs it
  contains, run `check_fast` on each, and summarize the flags in a compact
  table (statement, lean status, flags, evidence tier), worst first.
- If `$ARGUMENTS` contains `--deep`: after the fast pass, ask to confirm cost
  (count × $0.17–0.27), then run `check_deep` on the statements the user
  confirms.
- If `$ARGUMENTS` is a single statement or pair: check it directly and report
  the full payload in prose.

## If the tools are missing

If the leanscreen tools are not available, the server isn't running: tell the
user to `pip install leanscreen` (Python ≥3.12,<3.14) and re-enable the
plugin, and point them at https://github.com/ibrahimmian36/leanscreen for
Lean/mathlib setup (elaboration needs `LEANSCREEN_LEAN_PROJECT_PATH` and
ideally `LEANSCREEN_LEAN_REPL_PATH`). Do not silently skip screening: say the
screen did not run.
