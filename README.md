# leanscreen

A calibrated faithfulness screen for informal↔Lean 4 statement pairs, on the
command line and over [MCP](https://modelcontextprotocol.io), so you or
Claude (Code, Desktop, or any MCP client) can check statements while they
are being drafted.

```console
$ leanscreen check Demo.lean
exists_perfect_number: REJECTED  lean=valid_in_our_env  flags=deterministic-vacuous:reflexive-goal [deterministic]
even_add_even: no defect found  lean=valid_in_our_env
screened 2 pair(s): 1 rejected, 0 needs human review, 1 passed screening (no defect found, not a certification)
```

That first theorem compiles and is even provable. Its docstring says "there
exists a natural number equal to the sum of its proper divisors"; its
statement says `∃ n : ℕ, n = n`. The compiler has no objection. That gap is
what this tool screens for.

The one thing to understand before using it: this screen may only *reject*.
`passed_screening` means "no defect found by this harness". It is not a
certification of faithfulness. Measured against 886 frozen human verdicts,
statements a human reviewer had rejected still passed the full screen 17.0%
of the time for theorems and 35.6% for definitions; statements a human had
certified faithful were flagged 15–18% of the time. Every response carries
this calibration verbatim.

## Two tools

`check_fast` is deterministic only: lints (unused binders, trivially
satisfiable existentials, pinned `∃!` witnesses, suspicious ℕ-arithmetic,
and so on), vacuity checks (reflexive goals, `True` goals, withheld
declarations), and Lean 4 elaboration against your own mathlib environment.
Zero API calls, no key needed, about 0.1s per statement once the REPL is
warm. Call it constantly while drafting.

`check_deep` runs everything in `check_fast`, plus two independent LLM
judges under strict consensus (a back-translation judge and a
clause-by-clause checklist judge on separate models) and an adversarial
counterexample probe. It uses your own `ANTHROPIC_API_KEY`. Measured cost is
roughly $0.17–0.27 per statement, taking 30–60 seconds, and the response
reports actual spend as `actual_cost_usd`. Call it deliberately, before
something ships.

Both take `informal` (the natural-language statement), `lean` (the Lean 4
statement), and an optional `kind` (`theorem` | `definition`, inferred from
the declaration head when omitted). Responses rank their evidence:
`counterexample` > `deterministic` > `two-judge-consensus` > `single-judge`.
A single-judge flag is explicitly labeled as below the reporting bar.

## Install

```bash
pip install leanscreen
```

Requires Python ≥3.12. Runtime dependencies are `httpx`, `pydantic`,
`pydantic-settings`, and `mcp`. Nothing else.

## Command line

`leanscreen check` screens once and exits; the bare `leanscreen` command
still runs the MCP server. Three input shapes:

```bash
leanscreen check --informal "The sum of two even integers is even." --lean "theorem t (a b : Int) (ha : Even a) (hb : Even b) : Even (a + b)"
```

```bash
leanscreen check pairs.jsonl
```

```bash
leanscreen check MyFile.lean
```

The `.lean` form pairs each `theorem`/`lemma`/`def` with the `/-- ... -/`
doc comment above it and screens every documented declaration in the file;
undocumented declarations are skipped with a note. The default is the free
fast screen. `--deep` adds the judges and probe on your own
`ANTHROPIC_API_KEY`, with `--budget USD` as a hard stop. `--json` writes
one full payload object per line to stdout, everything else to stderr.

Exit codes are a CI contract: `0` means nothing was rejected (no defect
found, which is not a certification), `1` means at least one pair was
rejected on reject-tier evidence, `2` means a usage or configuration
error. A formalization repo can run `leanscreen check src/*.lean` in CI
and fail the build on unscreened defects.

## Claude Code plugin

This repo is also a Claude Code plugin, and its own marketplace. Beyond
registering the MCP server for you, the plugin ships a skill that makes
Claude screen habitually: `check_fast` after drafting any Lean statement,
`check_deep` offered (with its cost stated) before formalizations ship, and
results always reported as screening rather than certification.

```bash
pip install leanscreen
```

then inside Claude Code:

```text
/plugin marketplace add ibrahimmian36/leanscreen
/plugin install leanscreen@millennium-research
```

`/leanscreen:screen <file>` runs a fast pass over every pair in a file
(`--deep` opts into the paid judges after a cost confirmation). Uninstall
with `/plugin uninstall leanscreen`. The `pip install` still matters, since
the plugin launches the `leanscreen` command from your PATH.

## Lean setup (optional but recommended)

Without a Lean project the server still runs; `check_fast` does lints +
vacuity and says plainly that elaboration was skipped. With one, statements
are elaborated for real:

1. A Lean 4 project with mathlib, built: `lake build` inside it.
2. The [community REPL](https://github.com/leanprover-community/repl),
   built against the same toolchain: `lake build` inside the repl repo
   gives you `.lake/build/bin/repl`.
3. `lake` on the server's PATH.

mathlib imports once at server startup, taking about 100 seconds in the
background. Calls arriving mid-warm-up answer immediately with a "still
warming" note, then each check takes ~0.1s.

## Configuration

Environment variables (or a `.env` in the working directory), all
`LEANSCREEN_`-prefixed:

| Variable | Default | Meaning |
|---|---|---|
| `LEANSCREEN_LEAN_PROJECT_PATH` | unset | Lean 4 + mathlib project (elaboration off when unset) |
| `LEANSCREEN_LEAN_REPL_PATH` | unset | community REPL binary; without it every check pays a full `lake env lean` |
| `LEANSCREEN_LEAN_TIMEOUT_SECONDS` | `180` | per-statement Lean budget |
| `LEANSCREEN_ANTHROPIC_MODEL` | `claude-opus-4-8` | judge A + probe (the calibrated default) |
| `LEANSCREEN_JUDGE_B_MODEL` | `claude-fable-5` | checklist judge (calibrated default; locked-surface models get a 32k token budget automatically) |
| `LEANSCREEN_MAX_TOKENS` | `4096` | judge A response budget |
| `ANTHROPIC_API_KEY` | unset | needed for `check_deep` only |

Claude Code (`.mcp.json` in your project) or Claude Desktop
(`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "lean-faithfulness-screen": {
      "command": "leanscreen",
      "env": {
        "LEANSCREEN_LEAN_PROJECT_PATH": "/path/to/your/lean-mathlib-project",
        "LEANSCREEN_LEAN_REPL_PATH": "/path/to/repl/.lake/build/bin/repl",
        "ANTHROPIC_API_KEY": "sk-ant-…"
      }
    }
  }
}
```

## What this does not guarantee

The judge configuration was calibrated 2026-07-15 against 886 frozen human
verdicts (595 faithful / 291 unfaithful) from a production research-math
corpus. Under strict two-judge consensus, human-rejected pairs still passed
17.0% (theorems) / 35.6% (definitions) of the time, and human-certified
pairs were flagged 15–18% of the time. Both judges are Anthropic-family
models, so correlated blind spots cannot be ruled out. The counterexample
probe confabulates: on one PutnamBench sample its counterexamples were wrong
4 times out of 5. Treat every flag as a candidate for human confirmation and
every pass as "nothing found", never "faithful."

Human certification, meaning an expert reviewer confirming that the Lean
means the informal statement, is what this screen deliberately does not
automate. We offer it as a service: contact ibrahimnmian@gmail.com.

## License

[FSL-1.1-Apache-2.0](LICENSE) (the Functional Source License): free to use,
copy, modify, and redistribute, including internal commercial use,
non-commercial education and research, and professional services, but not
to offer as a competing commercial product or service. Each version
automatically becomes Apache 2.0 two years after its release, the same
license as mathlib. It is not OSI-approved until the conversion, so read it
before building on it commercially.

## Provenance

Extracted from Millennium Research's private formalization platform
(2026-07-28); the detector stack, judge prompts, and calibration figures are
the ones behind our benchmark audits. The miniF2F and ProofNet# filings are
public, and the PutnamBench, ProofNetVerif, and CLEVER audits have been
shared with their maintainers. The calibration *data* is not included.

Project page: [millenniumresearch.ai/leanscreen](https://millenniumresearch.ai/leanscreen)
