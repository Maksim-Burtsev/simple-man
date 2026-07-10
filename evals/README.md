# Simple Man Benchmark

This repo has two benchmark suites because "token savings" can mean two
different things:

- `runtime_economics`: Codex coding-agent cost, including instruction overhead
  and amortized long-session net.
- `reference_compression`: Caveman README-style output compression, measured
  against a normal helpful baseline and output tokens only.

> **Evidence status:** use `review-automatic` for current candidate readiness.
> The older runtime/reference runners below are retained for migration work;
> they are not authoritative skill-quality evidence until they use the same
> isolated execution contract.

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

## Commands

### Blind communication review

Generate the 10-prompt EN/RU smoke set with two matched low-verbosity arms:

```bash
make review-smoke-dry-run
make review-smoke
make review-serve
```

The smoke run makes 20 sequential Codex calls: native low verbosity and native
low verbosity plus the current runtime policy. Override the pinned execution
inputs explicitly when needed:

```bash
make review-smoke REVIEW_MODEL=gpt-5.5 REVIEW_EFFORT=high REVIEW_OUTPUT=/tmp/review-run
```

The runner creates a fresh `HOME`, `CODEX_HOME`, working directory, and temp
auth state for every answer. It disables optional integrations, uses read-only
sandboxing, validates the exact model-visible prompt, records raw JSONL and
usage privately, and supports identity-checked resume.

Review data is split into:

- `public/bundle.json`: opaque shuffled A/B pairs only.
- `private/manifest.json`: execution contract plus random blinding secret.
- `private/key.json`: arm mapping, used only after review is sealed.
- `private/ratings.json` and `private/results.json`: resumable judgments and the
  final reveal.

Pair IDs, order, and block-balanced left/right assignment use the private HMAC
secret. The localhost review UI stores side-specific quality flags, survives a
same-tab refresh, and does not return arm identities before every pair is rated
and the review is sealed.

This 10-pair run validates the harness and review UX. It is not enough to claim
a stable skill-quality win; publishable evaluation needs a larger held-out
corpus, repeated trials, and calibrated judging.

### Automatic held-out gate

Run iteration C without human ratings:

```bash
make review-quality-dry-run
make review-auto-dry-run
make review-automatic
```

`review-automatic` runs two gates in order. The cheaper repo-quality gate runs
first; the communication gate starts only if it passes.

#### Repo-quality gate

`make review-quality` uses three seeded repositories: Node session expiry,
Python payment idempotency, and SQLite migration ordering. Each matched arm
runs twice: `3 projects × 2 arms × 2 trials = 12 Codex calls`.

Every call gets a fresh `HOME`, `CODEX_HOME`, working tree, and Git repository;
the model, effort, low verbosity, prompt, permissions, and disabled network are
pinned. A custom Codex permission profile denies model-tool reads and writes to
the entire isolated `CODEX_HOME`, the real user home, this source worktree, and
the common Git repository. The runner locally proves workspace write access and
credential-read denial before every remote call; prompt preflight proves the
same profile and network restriction are active. Non-macOS live runs fail
closed.

The runner accepts only production-file changes, requires a production diff
and clean `git diff --check`, and recognizes only the exact canonical test
command in the raw trace. After Codex exits and its auth-bearing directory is
deleted, canonical and hidden validation run in separate pristine copies with a
fresh no-auth environment, disabled network, source/home denies, wall/output/
file/workspace/patch limits, process-group cleanup, and a read-only workspace.
Each hidden case gets its own pristine copy and randomized observation worker.
The worker contains no expected answer: it emits one strict JSON observation,
which the trusted parent compares with preregistered expected data outside the
sandbox. Missing/multiple output, forged test-runner summaries, and a clean
early `exit(0)` fail. macOS Seatbelt is a practical local boundary, not the
hostile-code isolation of an ephemeral VM.

Result semantics:

- both arms pass 6/6: `PASS`
- native control below 6/6: `INCONCLUSIVE`
- native 6/6 and candidate below 6/6: `FAIL`
- infrastructure/API failure: `INCONCLUSIVE`

The source commit must be clean before and after the live run. The manifest
records the commit, exact embedded config, runner/policy/fixture/worker and
executable hashes, Codex CLI version, and Python, Node, npm, Git, and SQLite
versions. Fixture hashes and copies come only from the committed `git ls-files`
manifest, so ignored caches cannot change a run. Each scheduled key has exactly
one immutable attempt: no automatic or selective retry. An interrupted or
infrastructure-failed attempt is consumed and yields `INCONCLUSIVE`; rerunning
remote work requires a new output directory. Completed attempts can be
resumed, but the final gate reparses raw traces, reapplies saved patches, reruns
isolated validation, and verifies the exact attempt inventory and artifact
hashes instead of trusting stored booleans. Exit codes are 0 for `PASS`, 1 for
candidate `FAIL`, 2 for `INCONCLUSIVE`, and 3 for integrity or configuration
`ERROR`.

#### Communication gate

The explicit sequence is:

```bash
make review-auto-generate
make review-auto-judge-dry-run
make review-auto-judge
make review-auto-reveal
make review-auto-gate
```

`make review-auto` runs that sequence. Defaults: 24 unique held-out tasks, one
answer draw for each of two matched arms, and two judge trials in both A/B
orientations. This is 48 answer calls plus 164 judge calls. Answer generation
uses `gpt-5.5/high`; a separate same-provider-family judge uses
`gpt-5.4/medium`.

`review_auto_v1.jsonl` is the development corpus used to diagnose the candidate
policy. `review_auto_holdout_v1.jsonl` is the 24-task forward-test corpus; do
not tune the policy after revealing its arm results. Run generation and judging
from the same clean preregistration commit. The gate rejects a dirty or
different source commit; canonical answer and judge runners fail before model
calls if the checkout is dirty and recheck it after the run.

The judge sees only the public anonymous bundle. It runs in a fresh isolated
environment with tools disabled, validates a strict JSON schema, and must pass
17 calibration cases before benchmark judging starts. Exact flag anchors cover
every material flag used by the release gate: factual error, safety risk,
constraint violation, missing required content, unsupported claim, ambiguity,
and language/tone mismatch. Calibration also covers relevance, explicit-detail
requests, ties, `both_bad`, and untrusted response injection.

Each pair receives four independent judgments: two forward and two with A/B
swapped. A verdict or flag is stable only with at least three votes and support
from both orientations. Everything else is `unstable`, never coerced to a tie.

The deterministic gate requires:

- at least 24 pairs from 24 unique tasks
- at least 90% stable pairs
- clean verdict calibration and exact coverage of every material flag
- no consensus material defect on the candidate
- no `both_bad` verdict on any pair
- no candidate loss or instability on safety/detail-override cases
- candidate wins greater than or equal to losses
- at least 30% median paired character reduction against native low verbosity

These release thresholds and protected category prefixes are fixed in the
committed gate config; the release CLI has no post-reveal threshold overrides.
The report records the gate-config and gate-script hashes.

A failed calibration or blind reliability check is `INCONCLUSIVE`; reliability
failure remains sealed in `blind-results.json` and stops before reveal. A valid
reveal that misses a preregistered quality threshold is `FAIL`: the candidate
is not ready under this gate. Manual blind review starts only after both
automatic gates pass.

Override pinned inputs explicitly when needed:

```bash
make review-auto AUTO_REVIEW_MODEL=gpt-5.5 JUDGE_MODEL=gpt-5.4 AUTO_REVIEW_TRIALS=1 JUDGE_TRIALS=2
```

The model output itself is stochastic, so a fresh generation is not expected
to be bit-identical. “Reproducible” here means auditable and replayable:
versioned corpora/policies/schemas, a clean source commit, exact hashes and run
identity, isolated execution, raw JSONL traces, cost caps, resumable calls, a
separate reveal, and a deterministic gate. The final checker rebuilds the
public bundle from raw answer runs, rebuilds blind judgments from raw judge
runs, then rebuilds the reveal from the committed key before scoring it.

The local fixture data is small and tests finish quickly. The material cost is
224 remote calls (12 repo-quality + 48 answers + 164 judgments), bounded by
configured caps. Answer/judge calls are resumable across quota windows;
repo-quality keeps completed attempts but never retries a started attempt. Full
live feasibility is established only by a completed saved run. The one answer
draw per arm/task makes this a bounded readiness gate, not an estimate of
answer-generation variance; judge repeats measure judge stability only.

### Legacy token runners

Preview the legacy runtime-economics run without model calls:

```bash
make bench-dry-run
```

Run the legacy runtime-economics benchmark locally:

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
- The automatic held-out gate is a readiness check, not a universal model or
  statistical superiority claim. Its result must name the answer model, judge
  model, Codex CLI version, corpus hash, policy hash, and sample size.
- The automatic judge is a different model but remains in the same provider
  family. It is not independent human or cross-provider confirmation.
- Full default run size is `40 prompts × 6 arms × 3 trials = 720 Codex calls`
  when Caveman is available.
- Full reference run size is `10 prompts × 6 arms × 3 trials = 180 Codex calls`
  when Caveman is available.
- `reference_compression` is still Codex CLI, not Claude API. It uses a verbose
  normal baseline to make the comparison shape comparable; exact Caveman README
  reproduction requires the Caveman Anthropic API runner.
