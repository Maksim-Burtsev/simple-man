# Simple Man Runtime Benchmark Brief

Date: 2026-05-22
Repo: `Maksim-Burtsev/simple-man`
Branch: `codex/benchmark-simple-man`
PR: https://github.com/Maksim-Burtsev/simple-man/pull/2

## Goal

Improve compression without material quality loss.

The PR now separates:

- `simple_man_runtime`: compact always-on policy from `AGENTS.md.snippet`
- `simple_man_candidate`: diagnostic A/B policy for future compression trials
- `simple_man_skill`: full explicit skill from `skills/simple-man/SKILL.md`

Headline metrics:

- Reference compression: output tokens vs verbose `normal`
- Runtime economics: output compression plus long-session net
- Full-skill first-turn/net cost: diagnostic only

## Current Runtime Shape

- `AGENTS.md.snippet`: 245 words
- `evals/policies/simple_man_candidate_runtime.md`: 246 words
- `skills/simple-man/SKILL.md`: 331 words
- local Caveman `SKILL.md`: 528 words

Always-on files do not invoke `$simple-man`; they inline the runtime policy.

## Harness

Suites:

- `runtime_economics`: coding-agent cost, visible input + output, with amortized session net.
- `reference_compression`: Caveman README-style output-only compression against a verbose normal baseline.

Default arms:

- Runtime: `control`, `terse`, `simple_man_runtime`, `simple_man_candidate`, `simple_man_skill`, optional `caveman`
- Reference: `normal`, optional `caveman_full`, `caveman_ultra`, `simple_man_runtime`, `simple_man_candidate`, `simple_man_skill`

Quality gate:

- Default `measure.py --check` gates Simple Man arms present in the snapshot.
- Caveman is gated by reference sanity only: at least one Caveman reference arm must reach `>=50%` output compression vs `normal`.
- Deterministic checks are not a substitute for human paired review.

## Repro Commands

Fast validation:

```bash
make test
git diff --check
```

Final reference sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --suite reference_compression \
  --snapshot /tmp/simple-man-final-reference-results.json \
  --overwrite \
  --trials 1 \
  --arm normal \
  --arm caveman_ultra \
  --arm simple_man_runtime

python3 evals/measure.py --snapshot /tmp/simple-man-final-reference-results.json --check
python3 evals/measure.py --snapshot /tmp/simple-man-final-reference-results.json
```

Final runtime-economics sample:

```bash
uv run --with tiktoken python evals/run_codex.py \
  --snapshot /tmp/simple-man-final-runtime-sample-results.json \
  --overwrite \
  --trials 1 \
  --limit 10 \
  --arm control \
  --arm terse \
  --arm simple_man_runtime

python3 evals/measure.py --snapshot /tmp/simple-man-final-runtime-sample-results.json --check
python3 evals/measure.py --snapshot /tmp/simple-man-final-runtime-sample-results.json
```

## Results

Live Codex CLI default model, `trials=1`, visible token counts, generated on 2026-05-22.

### Reference Compression

Snapshot: `/tmp/simple-man-final-reference-results.json`

Check:

- `python3 evals/measure.py --snapshot /tmp/simple-man-final-reference-results.json --check` passed

Headline:

| Arm | Output compression vs normal | Mean output tokens |
| --- | ---: | ---: |
| Caveman ultra | +79.7% | 195.1 |
| Simple Man runtime | +67.7% | 310.0 |

Per prompt:

| Prompt | Normal out | Caveman ultra out | Caveman ultra vs normal | Simple Man runtime out | Simple Man runtime vs normal |
| --- | ---: | ---: | ---: | ---: | ---: |
| async-refactor | 241 | 130 | +46.1% | 172 | +28.6% |
| auth-middleware-fix | 600 | 128 | +78.7% | 294 | +51.0% |
| docker-multi-stage | 525 | 161 | +69.3% | 207 | +60.6% |
| error-boundary | 973 | 247 | +74.6% | 422 | +56.6% |
| git-rebase-merge | 2596 | 223 | +91.4% | 303 | +88.3% |
| microservices-monolith | 3457 | 183 | +94.7% | 404 | +88.3% |
| postgres-pool | 3052 | 450 | +85.3% | 487 | +84.0% |
| pr-security-review | 1019 | 90 | +91.2% | 150 | +85.3% |
| race-condition-debug | 907 | 244 | +73.1% | 459 | +49.4% |
| react-rerender | 1352 | 95 | +93.0% | 202 | +85.1% |

Interpretation:

- Simple Man runtime now compresses output substantially: `+67.7%` vs verbose normal.
- Caveman ultra is still shorter on this suite: `+79.7%`.
- Simple Man runtime mean output improved from the previous PR reference result `410.4` to `310.0` tokens: `24.5%` shorter than the old runtime policy on the calibrated reference suite shape.

### Runtime Economics

Snapshot: `/tmp/simple-man-final-runtime-sample-results.json`

Check:

- `python3 evals/measure.py --snapshot /tmp/simple-man-final-runtime-sample-results.json --check` passed

Headline:

| Metric | Value |
| --- | ---: |
| Runtime output compression vs control | +14.6% |
| Runtime session net, 20 turns | +2.5% |
| Runtime session net, 50 turns | +6.7% |
| Runtime session net, 100 turns | +8.2% |

Summary:

| Arm | Output compression | First-turn net | Amortized net at 10 turns |
| --- | ---: | ---: | ---: |
| Simple Man runtime | +14.6% | -132.9% | -4.7% |

Interpretation:

- First isolated turns are still net-negative because any always-on policy has input overhead.
- On this 10-prompt runtime sample, the promoted runtime becomes net-positive by 20 turns and improves with longer sessions.

## Quality Notes

Manual spot review focused on the weakest or riskiest prompts:

- `pr-security-review`: preserved SQL injection, parameterized query, authorization/IDOR, overexposure, validation, and error handling.
- `race-condition-debug`: preserved atomic `UPDATE ... RETURNING`, bad read-modify-write pattern, upsert, and `FOR UPDATE` transaction fallback.
- `error-boundary`: preserved fallback UI, retry, `componentDidCatch`, logging hook, and React boundary limitations.
- `async-refactor`: preserved `async/await`, parameterized query, not-found path, callback-only wrapper, and caller `try/catch`.

No material quality loss found in these checked pairs.
