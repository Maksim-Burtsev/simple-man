<p align="center">
  <img src="assets/icon.png" alt="Simple Man icon" width="160">
</p>

# Simple Man — High-Signal Agent Communication

[![CI](https://github.com/Maksim-Burtsev/simple-man/actions/workflows/ci.yml/badge.svg)](https://github.com/Maksim-Burtsev/simple-man/actions/workflows/ci.yml)

A communication policy for coding agents: cut the chatter, keep the work.

Simple Man is not a persona and not a token-saving trick. It is a measured
policy that removes narration, filler and recap from agent answers **without
losing the facts you act on** — blockers, failed checks, exact identifiers,
risks, and the one-line fix for every finding. Requested detail is a contract:
tutorials, reports and exact formats are written in full, never compressed.

Built for people who read agent output for hours a day and want lower cognitive
load, not a shorter-looking bill.

## Install

Three separate things ship here. Installing the skill or the plugin makes
Simple Man *available*; it **does not enable the always-on policy** — only the
installer does that.

### Claude Code

Global, for every project:

```bash
npx skills add Maksim-Burtsev/simple-man -g -a claude-code -s simple-man -y
```

Project-level only — drop the `-g`:

```bash
npx skills add Maksim-Burtsev/simple-man -a claude-code -s simple-man -y
```

Invoke it explicitly with `$simple-man`, or let the agent activate it from the request.

For always-on behaviour instead, copy [`AGENTS.md.snippet`](./AGENTS.md.snippet) into your global `~/.claude/CLAUDE.md`.

### Portable Agent Skill

The same skill installs into any supported agent by changing `-a`:

```bash
npx skills add Maksim-Burtsev/simple-man -g -a codex -s simple-man -y
```

### Codex Plugin

```bash
codex plugin marketplace add Maksim-Burtsev/simple-man --ref v0.2.0
codex plugin add simple-man@simple-man
```

### Always-on Codex policy

The installer writes `${CODEX_HOME:-$HOME/.codex}/AGENTS.md` and installs the skill. Rerunning it updates that block in place instead of duplicating it:

```bash
curl -fsSL https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/v0.2.0/install.sh | bash
```

See [INSTALL.md](./INSTALL.md) for other agents and project-level setup.

## What it changes

It compresses user-facing communication and completes it:

- no preambles, praise, filler, recaps, or generic closing offers
- answer first; fragments and compact labels when clear
- every review or security finding carries its location, consequence and one-line fix
- refusing a destructive action names the target, the missing precondition and the safe procedure
- a failed check reports the exact failure and where to look next
- an explicitly requested shape — exact counts, order, word limits — is a contract, checked before sending
- qualifiers survive: "no known remaining risks" is never shortened to "no remaining risks"

## What it does not change

It must not reduce:

- repository search
- usage search
- dependency tracing
- impact analysis
- validation
- test/lint/typecheck effort
- proactive detection of related correctness issues

## Examples

Real captured pairs from the committed v0.3.1 benchmark run: same model
(`claude-sonnet-5`), same prompt, only the policy differs. In both pairs below
**both answers kept every required fact and obeyed the requested shape** — the
work is equal, only the words differ. Full evidence in
[`evals/releases/v0.3.1/`](./evals/releases/v0.3.1/report.md).

### Review findings (`out-dev-65`)

Prompt supplies two confirmed defects with exact locations and asks for findings
only, labelled P1 and P2.

<table>
<tr><th width="50%">No policy — 950 tokens</th><th width="50%">Simple Man — 222 tokens</th></tr>
<tr valign="top">
<td>

<b>Findings</b>

<b>P1 — Panic on missing file in upload handler</b><br>
<b>Location:</b> <code>src/handlers/upload.rs:114</code>

<b>Issue:</b> The code calls <code>.unwrap()</code> on <code>Path::new(&filename).canonicalize()</code>. <code>canonicalize()</code> returns an <code>Err</code> when the target file does not exist…

<b>Impact:</b> A missing file (e.g., client references a filename that was never uploaded, was deleted, or contains a path that doesn't resolve) crashes the request-handling thread. Depending on the async runtime/executor configuration, this can: terminate only the single request (best case…), or poison shared state / take down the worker…

<b>Suggested fix:</b> Replace <code>.unwrap()</code> with proper error propagation, e.g.: <i>(nine-line code block)</i>. Return a 400/404 to the client instead of panicking. Add a test case covering a non-existent filename.

<i>(continues for P2 at similar length)</i>

</td>
<td>

P1 — <code>src/handlers/upload.rs:114</code>: <code>Path::new(&filename).canonicalize().unwrap()</code> panics if the file doesn't exist, crashing the request thread. Fix: replace <code>.unwrap()</code> with proper error handling (e.g. <code>.map_err(...)?</code> returning a 4xx/5xx response) instead of panicking.

P2 — <code>src/db/pool.rs:29</code>: creates a new <code>PgPool</code> per request instead of reusing a shared pool, exhausting DB connections under load. Fix: construct the <code>PgPool</code> once at startup (e.g. in app state) and pass a reference/handle into each request instead of instantiating per-request.

</td>
</tr>
<tr><td colspan="2"><b>77% fewer output tokens.</b> Both findings, both locations, both fixes — nothing to follow up on.</td></tr>
</table>

### Status update (`out-dev-14`)

<table>
<tr><th width="50%">No policy — 251 tokens</th><th width="50%">Simple Man — 119 tokens</th></tr>
<tr valign="top">
<td>

<b>Subject: inventory-sync — Status Update</b>

Hi [Manager],

Quick update on inventory-sync:

<ul>
<li><b>Kafka consumer rewrite</b> is deployed to canary.</li>
<li><b>Performance</b>: canary pods show p99 latency at 340ms, well within our 500ms SLO.</li>
<li><b>Issue</b>: readiness probe on pod <code>inventory-sync-7f9d8</code> is failing with <code>connection refused</code> on port 9090.</li>
<li><b>Rollout status</b>: paused before proceeding to the remaining 90% of pods, pending investigation of the probe failure.</li>
</ul>

Will update once we've root-caused the connection issue and are ready to resume the rollout.

Thanks,<br>[Your name]

</td>
<td>

Status: inventory-sync

<ul>
<li>Kafka consumer rewrite deployed to canary.</li>
<li>Canary p99 latency: 340ms (within 500ms SLO).</li>
<li>Blocker: readiness probe failing on pod inventory-sync-7f9d8 — <code>connection refused</code> on port 9090.</li>
<li>Rollout to remaining 90% paused pending fix.</li>
</ul>

</td>
</tr>
<tr><td colspan="2"><b>53% fewer output tokens.</b> The blocker is labelled as one, and both facts that change what the manager does survive.</td></tr>
</table>

### Historical examples

The earlier captured comparison (`evals/reports/codex-skill-comparison.md`) is
kept as historical evidence only, not as current release evidence. It was a
single run per arm, measured in characters rather than tokens, and the arms did
not always do the same amount of work — in the auth scenario the baseline added
a boundary test the Simple Man arm did not. It therefore cannot support a
headline claim.

## Agent support

`AGENTS.md.snippet` is the canonical runtime policy; `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` are generated from it by `scripts/sync_surfaces.py`.

Two activation surfaces ship here:

- full skill: `skills/simple-man/SKILL.md`
- compact always-on runtime policy: `AGENTS.md`, `AGENTS.md.snippet`, `CLAUDE.md`, `GEMINI.md`

| Agent/tool | Path |
| --- | --- |
| Claude Code | `skills/simple-man/SKILL.md`, or `CLAUDE.md` for always-on |
| OpenAI Codex / Agent Skills | `skills/simple-man/SKILL.md`, `AGENTS.md`, `AGENTS.md.snippet` |
| Gemini CLI | `GEMINI.md`, or configure Gemini to read `AGENTS.md` |
| Qwen Code | `AGENTS.md`, optional global skill copy |
| Cursor / Windsurf / Cline / Copilot / Continue / Zed / Junie | `AGENTS.md`, or copy `AGENTS.md.snippet` into that agent's native rule file |
| Amp / OpenCode / Kilo / Roo / Aider / other AGENTS.md agents | `AGENTS.md` |

Always-on project files do not invoke `$simple-man`; they inline the compact runtime policy to avoid loading full skill overhead on every turn.

Agent-specific dotdir rule files are not committed here. They are target-project activation files, not the source of the skill.

## Benchmark

Two preregistered live runs on `claude-sonnet-5`, 1,793 calls, all raw records
committed under [`evals/releases/`](./evals/releases/). Every published number
is rebuilt from the raw records by `make bench-v3-check`, and a test enforces
that for every release, not just the newest.

Latest run: 84 output cases across 12 categories (38% Russian), 40 activation
cases, 3 real agentic coding fixtures with hidden validators, blind pairwise
judging with position swap, and a holdout wave written by authors who never saw
earlier results.

**Quality first** — the shipped policy against its predecessor and controls:

| | previous v0.2 policy | shipped policy | one sentence of "be concise" | no policy |
| --- | ---: | ---: | ---: | ---: |
| Required facts kept | 57.1% | **66.7%** | 67.9% | 66.7% |
| Blind preference vs shipped | 8 wins | **48 wins**, 28 ties | — | — |
| False success claims | — | **0** | — | — |
| Requested format kept | 82.1% | 81.0% | 82.1% | 76.2% |
| Coding fixtures passed | 2/3 | 2/3 | 2/3 | 2/3 |

The previous policy compressed hardest (−66% output) **by dropping required
facts** — that is why it was replaced. The shipped policy restores fact
retention to the no-policy level while still removing a third of output length
(−32.4%, 95% CI [−23.2%, −43.8%]).

**On cost, honestly.** Output-token percentages are not session savings: in real
agent sessions most tokens are context and tool traffic, and JetBrains'
[measurement of the caveman skill](https://blog.jetbrains.com/ai/2026/07/speak-to-ai-agents-like-cavemen-tosave-tokens/)
on 86 real tasks found −8.5% session output tokens against an advertised 65%.
Our own three-session coding phase is consistent with that order of magnitude.
If you install Simple Man to cut your bill, one sentence of "be concise" gets
you most of the way; install it for what a sentence does not give you —
measured fact retention, findings that carry their fix, refusals that carry the
safe procedure, format contracts, and routing that knows when *not* to
compress (100% activation precision, zero false triggers on tutorials and
detailed reports).

**What did not ship, published rather than hidden:** the first candidate failed
its gates outright; the second beat the shipped policy decisively but only tied
the one-sentence control, and its promotion is an explicit owner decision over
the automated gate result, recorded with the trade-offs in
[`DECISION.md`](./evals/releases/v0.3.1/DECISION.md). Gate tables, a
mis-specified gate we scored as failed rather than quietly fixed, and both
runs' full analysis live in [`evals/releases/`](./evals/releases/).

Gates and inputs are preregistered by commit before the first call
([v0.3.1](./evals/releases/v0.3.1/preregistration.json)), and a test rehashes
every pinned input so the corpus cannot be edited after the fact.

Older Codex-based suites and what runs offline are described in
[`evals/README.md`](./evals/README.md).

## Recommended usage

Use it as the default communication layer when you want minimum user-facing words without reducing search, validation, or implementation effort.

## License

MIT — see [LICENSE](./LICENSE).
