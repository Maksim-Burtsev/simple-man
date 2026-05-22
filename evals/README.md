# Simple Man Benchmark

This benchmark measures Simple Man runtime and full-skill token behavior on the
same coding-agent tasks. When a Caveman skill file is available, it also adds a
`caveman` comparison arm.

## Design

The benchmark uses five possible arms:

| Arm | Instruction |
| --- | --- |
| `control` | Neutral professional coding-agent instruction |
| `terse` | Control + generic concise-answer instruction |
| `simple_man_runtime` | `terse` + tiny always-on `AGENTS.md.snippet` policy |
| `simple_man_skill` | `terse` + full `skills/simple-man/SKILL.md` content |
| `caveman` | `terse` + an external Caveman `SKILL.md` |

The report separates three metrics:

```text
output_compression = 1 - arm_visible_output / baseline_visible_output
first_turn_net = 1 - arm_visible_total / baseline_visible_total
session_net = 1 - (control_input + arm_overhead / turns + arm_output) / control_total
```

where `visible_total = visible_input_tokens + visible_output_tokens`, counted
with `tiktoken o200k_base` over the controlled benchmark prompt and final
answer. `output_compression` is the fair Caveman-style response-length metric.
`session_net` is reported for runtime over 20/50/100 turns. Full-skill
first-turn and net costs are diagnostic because injecting a full skill into
every isolated turn is the worst-case usage pattern.

## Why Codex CLI

`codex exec --json` returns real usage metadata:

- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`

The runner records all of them. It does not double-count
`reasoning_output_tokens`. Raw Codex usage is diagnostic; the primary metric is
the controlled visible token total.

## Commands

Preview the full run without model calls:

```bash
make bench-dry-run
```

Run the canonical benchmark locally:

```bash
make bench-refresh
```

Run a 10-prompt comparison sample:

```bash
make bench-compare-sample
```

Use a specific model or cheaper trial count:

```bash
make bench-refresh MODEL=gpt-5.4-mini TRIALS=1
```

`bench-refresh` uses `uv run --with tiktoken` by default. Without `uv`, install
`tiktoken` and override `BENCH_PYTHON`:

```bash
python3 -m pip install tiktoken
make bench-refresh BENCH_PYTHON=python3
```

To include Caveman in another environment, point to its skill file:

```bash
CAVEMAN_SKILL=/path/to/caveman/SKILL.md make bench-refresh
```

Read a generated snapshot without live model calls:

```bash
make bench
```

Validate snapshot freshness, prompt coverage, and deterministic quality checks
for `simple_man_runtime` and `simple_man_skill`:

```bash
make bench-check
```

`caveman` is a comparison arm, not the default gate for this repo. To inspect
external-arm quality too:

```bash
python3 evals/measure.py --check --quality-arm simple_man_runtime --quality-arm simple_man_skill --quality-arm caveman
```

Smoke-test the runner with one prompt and one trial, writing outside the repo:

```bash
make bench-smoke
```

## Reproducibility Contract

The snapshot records:

- Codex CLI version
- model
- trial count
- prompt corpus hash
- `SKILL.md` hash
- runtime policy hash
- git commit
- per-run final text, visible token counts, and raw Codex usage
- optional external Caveman skill hash when included

After changing `skills/simple-man/SKILL.md` or
`evals/prompts/coding_tasks.jsonl`, refresh the snapshot with
`make bench-refresh`. The checker fails if the snapshot hashes do not match the
current skill or prompt corpus.

This branch ships the benchmark harness, not a prefilled canonical snapshot.
Generate and review `evals/snapshots/codex-results.json` before publishing
benchmark numbers.

## Limits

- This is a Codex integration benchmark, not a universal model claim.
- Raw Codex hidden harness overhead is recorded but not used for the headline
  visible-token metrics because it can vary with cache state and local feature
  loading.
- The quality gate is intentionally lightweight and deterministic. It catches
  obvious omissions but does not replace human review of paired outputs.
- Full default run size is `40 prompts × 5 arms × 3 trials = 600 Codex calls`
  when Caveman is available.
