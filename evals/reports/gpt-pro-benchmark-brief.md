# Simple Man Runtime Benchmark Brief

Date: 2026-07-05
Repo: `Maksim-Burtsev/simple-man`
Branch: `codex/benchmark-refresh`

## Goal

Refresh current benchmark evidence for Simple Man runtime behavior with the
current repo and Codex CLI.

This refresh is deliberately bounded and separate from skill hygiene. It does
not update skill text, package/install docs, or the historical skill comparison
report.

## Current Runtime Shape

- `AGENTS.md.snippet`: 245 words
- `evals/policies/simple_man_candidate_runtime.md`: 246 words
- `skills/simple-man/SKILL.md`: 331 words
- local Caveman `SKILL.md`: 531 words

Always-on files do not invoke `$simple-man`; they inline the runtime policy.

## Harness

Suites:

- `reference_compression`: output-only compression against verbose `normal`.
- `runtime_economics`: coding-agent visible input plus output, with amortized
  session net.

Current bounded arms:

- Reference: `normal`, `caveman_ultra`, `simple_man_runtime`
- Runtime: `control`, `terse`, `simple_man_runtime`

Quality gate:

- `measure.py --check` validates snapshot freshness, age, run count, prompt
  quality terms for Simple Man arms, and Caveman reference sanity when present.
- These checks are deterministic and bounded; they are not a substitute for
  full human paired review.

## Repro Commands

Validation:

```bash
make test
git diff --check
```

Reference sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --suite reference_compression \
  --snapshot evals/snapshots/reference-results.json \
  --overwrite \
  --trials 1 \
  --arm normal \
  --arm caveman_ultra \
  --arm simple_man_runtime

python3 evals/measure.py \
  --suite reference_compression \
  --snapshot evals/snapshots/reference-results.json \
  --prompts evals/prompts/reference_compression.jsonl \
  --check

python3 evals/measure.py \
  --suite reference_compression \
  --snapshot evals/snapshots/reference-results.json \
  --prompts evals/prompts/reference_compression.jsonl
```

Runtime sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --snapshot evals/snapshots/codex-results.json \
  --overwrite \
  --trials 1 \
  --limit 10 \
  --arm control \
  --arm terse \
  --arm simple_man_runtime

python3 evals/measure.py --snapshot evals/snapshots/codex-results.json --check
python3 evals/measure.py --snapshot evals/snapshots/codex-results.json
```

## Run Metadata

- Reference snapshot: `evals/snapshots/reference-results.json`
- Runtime snapshot: `evals/snapshots/codex-results.json`
- Codex CLI: `codex-cli 0.142.5`
- Model: `default`
- Trials: 1
- Generated: `2026-07-05T14:02:21.325700+00:00` and
  `2026-07-05T14:14:44.273493+00:00`
- Snapshot source commit: `4371a797b61e69113e3c755d7f4cf513ac9dbde6`

## Reference Compression Results

Check:

- `python3 evals/measure.py --suite reference_compression --snapshot evals/snapshots/reference-results.json --prompts evals/prompts/reference_compression.jsonl --check` passed

Headline:

| Arm | Output compression vs normal | Mean output tokens |
| --- | ---: | ---: |
| Caveman ultra | +87.1% | 176 |
| Simple Man runtime | +69.5% | 413.4 |

Per prompt:

| Prompt | Normal out | Caveman ultra out | Caveman ultra vs normal | Simple Man runtime out | Simple Man runtime vs normal |
| --- | ---: | ---: | ---: | ---: | ---: |
| async-refactor | 379 | 50 | +86.8% | 172 | +54.6% |
| auth-middleware-fix | 826 | 102 | +87.7% | 279 | +66.2% |
| docker-multi-stage | 846 | 171 | +79.8% | 291 | +65.6% |
| error-boundary | 1002 | 294 | +70.7% | 525 | +47.6% |
| git-rebase-merge | 2621 | 164 | +93.7% | 514 | +80.4% |
| microservices-monolith | 4604 | 215 | +95.3% | 497 | +89.2% |
| postgres-pool | 3046 | 376 | +87.7% | 719 | +76.4% |
| pr-security-review | 946 | 103 | +89.1% | 179 | +81.1% |
| race-condition-debug | 1495 | 204 | +86.4% | 601 | +59.8% |
| react-rerender | 1402 | 81 | +94.2% | 357 | +74.5% |

Interpretation:

- Simple Man runtime compresses reference output by `+69.5%` vs verbose normal.
- Caveman ultra remains shorter on this reference suite at `+87.1%`.

## Runtime Economics Results

Check:

- `python3 evals/measure.py --snapshot evals/snapshots/codex-results.json --check` passed

Headline:

| Metric | Value |
| --- | ---: |
| Runtime output compression vs control | +14.1% |
| Runtime session net, 20 turns | +2.3% |
| Runtime session net, 50 turns | +6.6% |
| Runtime session net, 100 turns | +8.1% |

Summary:

| Arm | Output compression | First-turn net | Amortized net at 10 turns |
| --- | ---: | ---: | ---: |
| Simple Man runtime | +14.1% | -135.9% | -5.0% |

Per prompt:

| Prompt | Category | Control out | Terse out | Simple Man runtime out | Simple Man runtime vs control | Simple Man runtime vs terse |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| async-await-refactor | implementation | 141 | 131 | 50 | +64.5% | +61.8% |
| django-monolith-split | planning | 232 | 285 | 232 | +0.0% | +18.6% |
| docker-typescript-multistage | implementation | 161 | 166 | 149 | +7.5% | +10.2% |
| express-sql-review | code-review | 78 | 151 | 99 | -26.9% | +34.4% |
| git-rebase-merge | explanation | 192 | 135 | 146 | +24.0% | -8.1% |
| jwt-expiry-date-now | debugging | 134 | 153 | 120 | +10.4% | +21.6% |
| postgres-counter-race | debugging | 212 | 273 | 158 | +25.5% | +42.1% |
| postgres-pool-node | implementation | 382 | 456 | 305 | +20.2% | +33.1% |
| react-error-boundary | implementation | 413 | 375 | 405 | +1.9% | -8.0% |
| react-rerender-object-prop | explanation | 135 | 134 | 116 | +14.1% | +13.4% |

Interpretation:

- First isolated turns are net-negative because the always-on runtime policy has
  visible input overhead.
- On this 10-prompt runtime sample, the runtime turns net-positive by 20 turns
  and improves with longer sessions.
- The weakest prompt vs control is `express-sql-review` at `-26.9%`; it still
  passed deterministic quality checks.

## Caveat

This is a bounded current sample, not a full benchmark corpus refresh. The
runtime snapshot intentionally covers `--limit 10`; the default checker passed
for that bounded snapshot.
