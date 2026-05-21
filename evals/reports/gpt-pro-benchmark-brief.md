# Simple Man Compression Benchmark Brief

Date: 2026-05-22
Repo: `/Users/zadro/open-source/simple-man`
Branch: `codex/benchmark-simple-man`

## Goal

Simple Man originally aimed to reduce user-facing agent tokens at least around
Caveman ultra level, without lowering engineering quality. Earlier benchmarks
showed weak or negative compression. This branch tightens the skill and adds a
reproducible benchmark harness.

## What Changed

Skill changes:

- `skills/simple-man/SKILL.md` now uses explicit high-compression rules:
  sentence fragments, compact labels, direct nouns/verbs, no preamble/recap,
  strict response budgets by answer type.
- Preserves quality-critical facts: failed/skipped checks, risk, approval
  needs, exact files/commands/errors/identifiers.
- Adds verbatim preservation for concise status/security/telemetry terms:
  `pass`, `fail`, `skipped`, `not verified`, `refuse`, `authorization`,
  `tool calls`, `final status`, `errors`.
- Adds compact response patterns for status, blockers, risk, review findings,
  skipped validation, security review, and agent telemetry.
- Keeps the skill small: current `SKILL.md` is 530 words; local Caveman
  `SKILL.md` is 531 words.

Harness changes:

- Adds `control`, `terse`, `simple_man`, and optional `caveman` arms.
- Separates:
  - output compression: answer length only
  - first-turn net: includes full skill input overhead
  - amortized net: spreads skill overhead over N turns, default 10
- Uses `tiktoken` `o200k_base` for controlled visible input/output token counts.
- Records raw Codex usage separately for diagnostics.
- Adds 40 coding-agent prompts across explanation, debugging, implementation,
  planning, code-review, status, and risk categories.
- `measure.py --check` gates `simple_man` by default. Caveman is comparison,
  not this repo's CI gate. Use `--quality-arm caveman` to inspect it.
- Quality matcher now tolerates punctuation and simple morphology, e.g.
  `Unit tests: pass` matches `unit tests pass`, `tool_call_id` matches
  `tool calls`, `AS build` matches `builder`.

## Repro Commands

Fast validation:

```bash
make test
make bench-dry-run
git diff --check
```

Focused comparison sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --snapshot /tmp/simple-man-caveman-focused-after-results.json \
  --overwrite \
  --trials 1 \
  --prompt-id status-update-failing-tests \
  --prompt-id ambiguous-db-backup \
  --prompt-id destructive-table-drop \
  --prompt-id review-findings-style \
  --prompt-id final-answer-skipped-tests \
  --prompt-id express-sql-review \
  --prompt-id prompt-injection-email \
  --prompt-id cli-error-message \
  --prompt-id observability-agent-trace \
  --prompt-id frontend-overlap-bug

python3 evals/measure.py --snapshot /tmp/simple-man-caveman-focused-after-results.json
python3 evals/measure.py --snapshot /tmp/simple-man-caveman-focused-after-results.json --check
```

Canonical first-10 sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --snapshot /tmp/simple-man-caveman-sample-after-results.json \
  --overwrite \
  --trials 1 \
  --limit 10

python3 evals/measure.py --snapshot /tmp/simple-man-caveman-sample-after-results.json
python3 evals/measure.py --snapshot /tmp/simple-man-caveman-sample-after-results.json --check
```

Full public-number run, not executed in this PR pass:

```bash
make bench-refresh TRIALS=3
make bench-check
make bench
```

With local Caveman available, full default size is `40 prompts x 4 arms x 3 trials = 480 Codex calls`.

## Current Skill Hashes

- Simple Man: `a725e0c5fca9b47291b7c6fc7ea03368933fec5c477e7d943ad6983981786c14`
- Caveman local reference: `6a93e68b5d843ab6da3290dfe81cfdf26de166be7f3feca5acb52744f63db593`

## Results Summary

All numbers below are from Codex CLI default model, `trials=1`, visible token
counts, amortized net at 10 turns. Treat them as smoke/focused evidence, not a
publishable final claim.

### Focused 10 Prompt Set

This set targets categories where the old Simple Man underperformed:
status, risk, code review, implementation, planning, debugging.

Before skill rewrite:

| Arm | Output compression vs control | First-turn net | Amortized net |
| --- | ---: | ---: | ---: |
| Simple Man | +4.3% | -502.7% | -49.8% |
| Caveman | -3.2% | -603.9% | -61.5% |

After skill rewrite:

| Arm | Output compression vs control | First-turn net | Amortized net |
| --- | ---: | ---: | ---: |
| Simple Man | +28.0% | -581.7% | -46.1% |
| Caveman | +0.5% | -650.0% | -63.9% |

Focused result:

- Simple Man output compression improved by +23.7 percentage points.
- Simple Man beat Caveman by +27.5 percentage points on output compression.
- Simple Man default quality gate passed after matcher normalization.
- Caveman comparison quality check failed on this focused set:
  missing `integration tests fail`, `destructive`, `parameterized/prepared`,
  `authorization/auth`, and `do not forward/refuse` in some runs.

After per-category output compression:

| Category | Simple Man | Caveman |
| --- | ---: | ---: |
| code-review | +37.2% | +17.5% |
| debugging | +20.4% | -12.9% |
| implementation | +17.9% | +28.6% |
| planning | +39.6% | +24.5% |
| risk | +14.9% | -38.4% |
| status | +41.7% | +22.5% |

### Canonical First-10 Prompt Sample

Before skill rewrite:

| Arm | Output compression vs control | First-turn net | Amortized net |
| --- | ---: | ---: | ---: |
| Simple Man | -2.9% | -233.9% | -25.0% |
| Caveman | +6.4% | -272.5% | -22.4% |

After skill rewrite:

| Arm | Output compression vs control | First-turn net | Amortized net |
| --- | ---: | ---: | ---: |
| Simple Man | +8.7% | -290.8% | -20.1% |
| Caveman | -1.5% | -327.6% | -31.5% |

First-10 result:

- Simple Man output compression improved by +11.6 percentage points.
- Simple Man beat Caveman by +10.2 percentage points on output compression.
- Simple Man default quality gate passed after matcher normalization.
- Caveman comparison quality check failed on this sample: missing
  `authorization/auth` in `express-sql-review`.

After per-category output compression:

| Category | Simple Man | Caveman |
| --- | ---: | ---: |
| code-review | +41.5% | +3.6% |
| debugging | +23.0% | +15.8% |
| explanation | +9.5% | +18.1% |
| implementation | -11.8% | -16.9% |
| planning | +27.7% | -19.0% |

## Interpretation

Output compression is the cleanest answer to "how much shorter are responses?"
because it excludes skill prompt overhead. On the two 10-prompt smoke samples
run after the rewrite:

- Focused sample: Simple Man +28.0%, Caveman +0.5%.
- Canonical first-10 sample: Simple Man +8.7%, Caveman -1.5%.

The implementation category remains the weakest area because code-heavy answers
dominate token count and should not be aggressively shortened. This looks
acceptable: Simple Man should compress prose, not delete code needed for a
usable answer.

First-turn net remains negative because each arm injects a full skill file into
a single isolated `codex exec` turn. Amortized net is more realistic for a
multi-turn session, but still conservative because the benchmark does not model
prompt caching. Full public claims should use `40 x 4 x 3` or more.

## Open Questions For Review

1. Should the headline claim use output compression only, or should docs always
   pair it with amortized net?
2. Should the benchmark include a long-session simulation where the skill
   overhead is cached or amortized across realistic task sequences?
3. Should quality checks remain lightweight keyword/morphology checks, or should
   the repo add a rubric-based judge for higher-confidence quality scoring?
4. Should implementation prompts be scored separately from prose-heavy prompts,
   because correct code naturally limits compression?
