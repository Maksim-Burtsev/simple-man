# Simple Man Runtime Benchmark Brief

Date: 2026-05-22
Repo: `/Users/zadro/open-source/simple-man`
Branch: `codex/benchmark-simple-man`
PR: https://github.com/Maksim-Burtsev/simple-man/pull/2

## Goal

Fix the main issue from the GPT review: full `SKILL.md` is too expensive as an
always-on instruction. The PR now separates:

- `simple_man_runtime`: tiny always-on policy from `AGENTS.md.snippet`
- `simple_man_skill`: full explicit skill from `skills/simple-man/SKILL.md`

Headline numbers should use runtime output compression and runtime long-session
net. Full-skill first-turn/net metrics are diagnostic only.

## Current Runtime Shape

- `AGENTS.md.snippet`: 123 words
- `skills/simple-man/SKILL.md`: 287 words
- local Caveman `SKILL.md`: 528 words

Always-on files do not invoke `$simple-man`; they inline the tiny runtime
policy. The full skill remains available for explicit skill installs.

Current hashes:

- runtime policy: `a1bf7f5845c42827148411853f64822ebe1bc030d2f6b39a11d4a8c3c013cd54`
- full skill: `ca54634f8adaeda5bffbddad08cef0cca64aca81b943bf2a9ae981aa5c2b40e3`
- Caveman reference: `6a93e68b5d843ab6da3290dfe81cfdf26de166be7f3feca5acb52744f63db593`

## Harness

Arms:

- `control`
- `terse`
- `simple_man_runtime`
- `simple_man_skill`
- `caveman`

Metrics:

- `output_compression`: answer length only
- `first_turn_net`: isolated turn with full instruction overhead
- `session_net`: instruction overhead amortized over 20/50/100 turns

Default full run size with Caveman: `40 prompts x 5 arms x 3 trials = 600 Codex calls`.

Quality gate:

- default `measure.py --check` gates `simple_man_runtime` and `simple_man_skill`
- Caveman can be added with `--quality-arm caveman`
- current focused and first-10 samples pass both default and Caveman-inclusive checks

## Repro Commands

Fast validation:

```bash
make test
make bench-dry-run
git diff --check
```

Focused sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --snapshot /tmp/simple-man-runtime-focused-results.json \
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

python3 evals/measure.py --snapshot /tmp/simple-man-runtime-focused-results.json
python3 evals/measure.py --snapshot /tmp/simple-man-runtime-focused-results.json --check
```

Canonical first-10 sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --snapshot /tmp/simple-man-runtime-sample-results.json \
  --overwrite \
  --trials 1 \
  --limit 10

python3 evals/measure.py --snapshot /tmp/simple-man-runtime-sample-results.json
python3 evals/measure.py --snapshot /tmp/simple-man-runtime-sample-results.json --check
```

## Results

All live numbers below are Codex CLI default model, `trials=1`, visible token
counts, generated on 2026-05-22. Treat as smoke/focused evidence; public claims
still need the full `40 x 5 x 3` run.

### Focused 10 Prompt Set

Target: status, risk, code review, implementation, planning, debugging.

| Arm | Output compression vs control | First-turn net | Amortized net at 10 turns |
| --- | ---: | ---: | ---: |
| Simple Man runtime | +7.3% | -150.8% | -8.2% |
| Simple Man skill | +5.6% | -329.2% | -27.8% |
| Caveman | -28.2% | -711.4% | -76.0% |

Runtime headline:

| Metric | Value |
| --- | ---: |
| runtime output compression vs control | +7.3% |
| runtime output compression vs Caveman | +16.7% |
| runtime session net, 20 turns | -0.3% |
| runtime session net, 50 turns | +4.4% |
| runtime session net, 100 turns | +6.0% |

Focused quality:

- `python3 evals/measure.py --snapshot /tmp/simple-man-runtime-focused-results.json --check` passed
- Caveman-inclusive quality check also passed

### Canonical First-10 Prompt Sample

| Arm | Output compression vs control | First-turn net | Amortized net at 10 turns |
| --- | ---: | ---: | ---: |
| Simple Man runtime | +10.1% | -66.0% | +0.8% |
| Simple Man skill | +15.9% | -144.8% | -3.6% |
| Caveman | -6.7% | -330.4% | -32.6% |

Runtime headline:

| Metric | Value |
| --- | ---: |
| runtime output compression vs control | +10.1% |
| runtime output compression vs Caveman | +10.4% |
| runtime session net, 20 turns | +4.5% |
| runtime session net, 50 turns | +6.8% |
| runtime session net, 100 turns | +7.5% |

First-10 quality:

- `python3 evals/measure.py --snapshot /tmp/simple-man-runtime-sample-results.json --check` passed
- Caveman-inclusive quality check also passed

## Interpretation

The GPT review was directionally right: the full skill should not be treated as
the always-on runtime. After the split, runtime overhead is small enough that
session net becomes positive in the first-10 sample and near break-even by 20
turns in the focused sample.

The runtime arm is also shorter than Caveman on both live samples:

- focused: runtime beats Caveman by +16.7% output compression
- first-10: runtime beats Caveman by +10.4% output compression

The full skill still compresses output on some prompts, but its input overhead
is too large for headline economics. Keep it as explicit install/use surface,
not always-on project runtime.

## Open Questions

1. Should public docs publish only `simple_man_runtime` numbers and keep
   `simple_man_skill` as diagnostic appendix?
2. Should full benchmark snapshots be committed after a `600`-call run, or kept
   out of git with reports only?
3. Should the next eval add a long-session simulation with prompt caching,
   rather than only isolated `codex exec` turns?
