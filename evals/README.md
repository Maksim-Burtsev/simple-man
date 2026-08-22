# Simple Man Benchmark

This repo has two historical benchmark suites plus the offline-verifiable eval
v2 release gates. "Token savings" can mean two different things:

- `runtime_economics`: Codex coding-agent cost, including instruction overhead
  and amortized long-session net.
- `reference_compression`: Caveman README-style output compression, measured
  against a normal helpful baseline and output tokens only.

## Claude Code benchmark (`evals/bench/`)

The current benchmark. It runs against the `claude` CLI in headless mode and
needs no API key: `--system-prompt-file` replaces Claude Code's default system
prompt, so an arm measures its policy and nothing else, and `--tools ""` makes
each answer case a single text-in/text-out turn.

Five arms, each the same neutral prelude plus one policy file:

| Arm | Policy |
| --- | --- |
| `N` | none — prelude only |
| `A` | frozen v0.2 policy (shipped before the v0.3.1 promotion) |
| `B` | first v0.3 candidate — failed its gates, never shipped |
| `B2` | second candidate — promoted to the shipped policy in v0.3.1 |
| `G` | one sentence of "be concise" — the control that matters |
| `C` | vendored external Caveman skill |

```bash
make bench-v3-dry-run    # print the call plan, spend nothing
make bench-v3            # run it
make bench-v3-report     # rebuild report.md from the raw records
make bench-v3-check      # fail if the committed report cannot be rebuilt
```

Billing is fail-closed on the subscription. `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN` all
take precedence over the claude.ai login, so the runner refuses to start when
any of them is set and strips them from every subprocess. `--bare` is never
used: it forces API-key auth by design.

Judging is blind by construction rather than by scanning — the judge payload
carries the task and two answer texts, and never carries an arm label. Each pair
is judged in both orderings and a win requires winning both; a split between
orderings is recorded as a tie.

The report is always recomputed from the raw JSONL. `make bench-v3-check` fails
if a published number cannot be rebuilt from the evidence.

## Historical Codex skill comparison

The earlier captured comparison
([`evals/reports/codex-skill-comparison.md`](./reports/codex-skill-comparison.md))
is kept as historical evidence only, not as current release evidence. It was a
single run per arm, measured in characters rather than tokens, and the arms did
not always do the same amount of work — in the auth scenario the baseline added
a boundary test the Simple Man arm did not. It therefore cannot support a
headline claim.

## Lessons for the next preregistration

Recorded here so the next gate table is written before any results exist:

1. `winner_coding_failures` must be **relative to the no-policy control**, not an
   absolute zero. In v0.3.1 every arm including no-policy failed the same
   fixture, so the absolute gate measured fixture difficulty and failed the
   candidate for something no policy controls.
2. Add an explicit **beats-what-is-shipped** gate. Both v0.3 gate tables asked
   only whether the candidate beats a generic control; neither asked whether it
   beats the policy users actually have, which turned out to be the decision
   that mattered.
3. Keep the vs-generic-control comparison as an **informative** metric, not a
   shipping gate: it measures marketing claims, not user benefit.
4. The next measurement leg worth paying for is real-agent sessions at scale:
   SkillsBench via the open Harbor harness, same protocol JetBrains used for
   caveman (86 tasks, A/B, 3 runs). Now run — see "Session benchmark" below and
   `evals/releases/session-v1/`.
5. **Size the corpus for the claim, not just for the pooled number.** The product
   argument names specific categories — refusals that carry the safe procedure,
   findings that carry their fix, failed checks that report the exact failure —
   but at 7 cases per category the run cannot decide any of them: one case is
   14.3 points. The "Retention by category" table in each release report shows
   this directly. A run meant to settle those claims needs its cases concentrated
   in the categories under test, not spread evenly for coverage.

## Session benchmark: real Claude Code sessions on SkillsBench (`evals/session/`)

The output benchmark above measures one answer at a time with tools off. This
leg measures what the shipped policy does to a whole agent session: Claude Code
in a Docker sandbox, solving a SkillsBench task with tools, verified by the
task's own tests. It follows the protocol JetBrains used for caveman and
benjamin-plus — paired tasks, same model and effort, Wilcoxon signed-rank on the
paired deltas, an exact sign test on the verifier reward, and a mechanical
check that the payload reached every treated session and no control session.

| | |
|---|---|
| Tasks | SkillsBench at `aafac12f` (last commit in Harbor `task.toml` format, 87 tasks); each task's own skills are injected for every arm |
| Arms | `N` Claude Code with no policy; `B2` the shipped policy (`evals/policies/v0.3/B2-runtime.md`, byte-identical to `AGENTS.md.snippet`) appended to the system prompt; `G` the one-sentence "be concise" control |
| Agent | `claude-code` pinned, `anthropic/claude-sonnet-5`, effort `low`, k=1 |
| Delivery | `--append-system-prompt`, i.e. always-on. JetBrains measured that a skill folder alone saves nothing because the agent spends turns finding it; always-on is the treatment users actually get from the `AGENTS.md`/`CLAUDE.md` snippet |
| Order | tasks shuffled by a registered seed and run in batches of 20, both arms per batch, so a budget stop still leaves a balanced sample |
| Retries | a trial that errors on one arm only is retried once for that arm; a task that errors on both arms is dropped symmetrically; nothing is retried because of its result |
| Gates | declared in `preregistration.json` before the first trial: payload delivered 100 % / 0 %, at least 60 clean pairs, and reward not significantly worse (sign test). Cost and token deltas are informative, not gates — B2 is already shipped, the question is whether it breaks real sessions |

Billing is the claude.ai subscription only, as everywhere in this repo. The
container cannot see the macOS Keychain login, so the runner requires
`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (a subscription token, not
an API key) and refuses to start if any `ANTHROPIC_*` credential is set.

```bash
uv tool install harbor
git clone https://github.com/benchflow-ai/skillsbench ~/.cache/simple-man/skillsbench
git -C ~/.cache/simple-man/skillsbench checkout aafac12f
set -a; . ~/.config/simple-man/bench.env; set +a   # CLAUDE_CODE_OAUTH_TOKEN=...
make session-dry-run ARM=B2 BATCH=0                # prints the harbor commands, no calls
make session-pilot ARM=N SESSION_RUN=pilot && make session-pilot ARM=B2 SESSION_RUN=pilot
make session-run ARM=N BATCH=0 && make session-run ARM=B2 BATCH=0
make session-collect && make session-report && make session-check
```

`session-collect` flattens Harbor's job directories into `trials.jsonl` (one
row per trial: reward, cost, tokens, turns, wall-clock, error, delivery flag);
`session-report` and `session-check` rebuild `report.md` and `gates.md` from it.
Full trajectories are not committed (tens of MB); they ship as a release asset.

## Status: what you can actually run today

**The headline numbers come from `evals/releases/`, and their raw records are
committed.** `v0.3.0` and `v0.3.1` each ship the full JSONL of every call under
`<release>/run/`, and `make bench-v3-check` fails if a published report cannot
be rebuilt from them. A test enforces that for every release, not only the
newest.

The Codex-era suites below are a separate, older path, and **that** is what has
no committed snapshot: `evals/snapshots/` holds only a README, so the
`make bench*` targets in the two tables that follow fail until you generate a
snapshot yourself. Nothing on the project's front page rests on them.

Runs offline, no credentials, no model calls:

| Command | What it does |
| --- | --- |
| `make test` | Full unit suite plus packaging checks |
| `make eval-gates-check` | Fake answer/judge/seal/reveal chain, tamper rejection, coding fixtures |
| `make eval-release-dry-run` | Prints the preregistered call plan; starts nothing |
| `make eval-foundation-check` | Foundation runner tests against a stubbed CLI |
| `make bench-dry-run`, `make bench-reference-dry-run` | Print the planned benchmark run |

Requires the `codex` CLI, OpenAI credentials, and live model calls:

| Command | Cost |
| --- | --- |
| `make bench-refresh` | up to 40 prompts × 6 arms × 3 trials = 720 calls |
| `make bench-reference-refresh` | up to 10 prompts × 6 arms × 3 trials = 180 calls |
| `make bench-smoke`, `make bench-compare-sample` | 1 and 10 prompts respectively |

`make bench`, `make bench-check`, `make bench-reference` and
`make bench-reference-check` read a snapshot. Because none is committed, they
fail immediately until you run the matching `*-refresh` target first.

The eval v2 live path is **disabled in code**: both the `answers` and `judge`
subcommands in `evals/run_eval_v2.py` raise `live calls are disabled in Phase A`
unless `--fake` is passed. The 275-call plan can be previewed but not executed
from this branch.

External comparison policies are vendored and hash-pinned under
[`policies/external/`](./policies/external/README.md) so comparison arms are
reproducible from this repository alone.

## Eval v2 release gates

Eval v2 is the release-decision path. It contains 12 output dev cases, 20
activation dev cases, three isolated coding fixtures, anonymous pairwise
judging, deterministic material-fact checks, and separate visible/cache/usage
metrics. The committed holdout is a schema only; final holdout content is
created after the PR4 candidate head is frozen.

Run every credential-free gate, including fake answer/judge/seal/reveal,
tamper rejection, pristine coding validation, and hidden black-box probes:

```bash
make eval-gates-check
```

Preview the preregistered 275-call plan under the 280-call hard cap without
reading Codex auth or starting Codex:

```bash
make eval-release-dry-run
```

The release metrics keep commentary, final, visible output, uncached input,
cached input, output usage, and latency separate. `estimated_session_net` is an
explicit estimate, not observed billing or universal cost savings.

Live coding lanes require the exact macOS filesystem, network, and process
isolation preflight. An unsupported process boundary returns `INCONCLUSIVE`
before model-generated code runs.

The production-patch gate rejects added local absolute/file path literals so a
patch cannot key behavior to the authored workspace. Deliberately obfuscated
or derived path side channels are outside the v0.3 threat model.

## Runtime Economics Suite

Default runner suite: `runtime_economics`.

It uses six possible arms:

| Arm | Instruction |
| --- | --- |
| `control` | Neutral professional coding-agent instruction |
| `terse` | Control + generic concise-answer instruction |
| `simple_man_runtime` | `terse` + compact always-on `AGENTS.md.snippet` policy |
| `simple_man_candidate` | `terse` + candidate quality-first compression policy |
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

## Reference Compression Suite

Runner suite: `reference_compression`.

This suite mirrors the Caveman README benchmark shape:

| Arm | Instruction |
| --- | --- |
| `normal` | Codex-calibrated verbose helpful baseline |
| `caveman_full` | Caveman `SKILL.md` + explicit `/caveman full` |
| `caveman_ultra` | Caveman `SKILL.md` + explicit `/caveman ultra` |
| `simple_man_runtime` | `normal` + compact always-on `AGENTS.md.snippet` policy |
| `simple_man_candidate` | `normal` + candidate quality-first compression policy |
| `simple_man_skill` | `normal` + full `skills/simple-man/SKILL.md` content |

Its headline metric is only:

```text
output_compression = 1 - arm_visible_output / normal_visible_output
```

Codex's plain `You are a helpful assistant.` output is already short, unlike
the Claude API baseline in Caveman's README. The reference suite therefore uses
a verbose normal baseline so Caveman first has to reproduce README-like
compression behavior before Simple Man numbers are interpreted.

The checker includes a Caveman sanity gate. At least one Caveman reference arm
must reach `>=50%` average output compression vs `normal`, otherwise the suite
is considered uncalibrated and Simple Man headline comparisons should not be
published from that run.

## Why Codex CLI

`codex exec --json` returns real usage metadata:

- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`

The runner records all of them. It does not double-count
`reasoning_output_tokens`. Raw Codex usage is diagnostic; the primary metric is
the controlled visible token total.

## Skill-comparison foundation runner

`evals/run_skill_comparison.py` is the isolated real-agent comparison runner.
It does not generate the historical Markdown report.

Preview a deterministic plan without authentication or model calls:

```bash
python3 evals/run_skill_comparison.py --dry-run --seed 7 \
  --variant candidate=evals/policies/simple_man_candidate_runtime.md
```

Run a single policy variant on the supported macOS isolation host:

```bash
python3 evals/run_skill_comparison.py \
  --variant candidate=evals/policies/simple_man_candidate_runtime.md \
  --model gpt-5.5 --effort xhigh --max-calls 3 --output-dir /tmp/simple-man-eval
```

Repeat `--variant NAME=PATH` to compare policies. Live mode requires `--model`,
`--effort`, and an all-plan `--max-calls` cap. `--max-usd` remains unavailable
without a verified versioned price mapping.

The output directory contains only `summary.json`: non-secret run identity,
status, and usage scalars. A sibling private directory named
`.simple-man-eval-private` contains raw stdout/stderr, durable ledgers, and
disposable run data; do not publish it. Each attempt gets a deterministic
identity. Re-run an existing identity only with `--resume`; completed attempts
are not called again, while started/failed attempts return a consumed nonzero
outcome. A fresh attempt requires a new `--output-dir`; the existing private
ledger is never overwritten. `--resume` requires the exact saved identity
(including runner, fixture, policy, model, and CLI identity); raw evidence without
its ledger is refused before a model call.

## Existing benchmark commands

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

Run the Caveman README-style reference suite:

```bash
make bench-reference-dry-run
make bench-reference-refresh
make bench-reference
make bench-reference-check
```

Validate snapshot freshness, prompt coverage, and deterministic quality checks
for `simple_man_runtime`, `simple_man_candidate`, and `simple_man_skill`:

```bash
make bench-check
```

`caveman` is a comparison arm, not the default gate for this repo. To inspect
external-arm quality too:

```bash
python3 evals/measure.py --check --quality-arm simple_man_runtime --quality-arm simple_man_candidate --quality-arm simple_man_skill --quality-arm caveman
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
- candidate policy hash
- git commit
- per-run final text, visible token counts, and raw Codex usage
- optional external Caveman skill hash when included

After changing `skills/simple-man/SKILL.md` or
`evals/prompts/coding_tasks.jsonl`, refresh the snapshot with
`make bench-refresh`. The checker fails if the snapshot hashes do not match the
current skill or prompt corpus.

After changing `evals/prompts/reference_compression.jsonl`, refresh the
reference snapshot with `make bench-reference-refresh`.

This Codex suite ships as a harness only: no prefilled snapshot is committed for
it. Generate and review `evals/snapshots/codex-results.json` before publishing
anything from it. The project's published numbers come from `evals/releases/`
instead, and are rebuilt from committed raw records by `make bench-v3-check`.

## Limits

- `run_skill_comparison.py --max-usd` is available only with a verified,
  versioned model-price mapping. This repository has none, so the runner fails
  closed when the flag is supplied; Codex subscription runs use `--max-calls`
  and token caps instead.
- This is a Codex integration benchmark, not a universal model claim.
- Raw Codex hidden harness overhead is recorded but not used for the headline
  visible-token metrics because it can vary with cache state and local feature
  loading.
- The quality gate is intentionally lightweight and deterministic. It catches
  obvious omissions but does not replace human review of paired outputs.
- Full default run size is `40 prompts × 6 arms × 3 trials = 720 Codex calls`
  when Caveman is available.
- Full reference run size is `10 prompts × 6 arms × 3 trials = 180 Codex calls`
  when Caveman is available.
- `reference_compression` is still Codex CLI, not Claude API. It uses a verbose
  normal baseline to make the comparison shape comparable; exact Caveman README
  reproduction requires the Caveman Anthropic API runner.
