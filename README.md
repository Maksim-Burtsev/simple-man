<p align="center">
  <img src="assets/icon.png" alt="Simple Man icon" width="160">
</p>

# Simple Man — High-Signal Agent Communication

[![CI](https://github.com/Maksim-Burtsev/simple-man/actions/workflows/ci.yml/badge.svg)](https://github.com/Maksim-Burtsev/simple-man/actions/workflows/ci.yml)

High-compression professional communication mode for coding agents.

Simple Man is not a persona. It is a communication policy:

> Minimum user-facing words; same work quality.

It is designed for people who work with agents for many hours and want lower cognitive load — without making the agent passive, less careful, or less proactive.

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

It compresses user-facing communication:

- no preambles
- no praise
- no filler
- no repeated recaps
- no generic closing offers
- sentence fragments and compact labels when clear
- compact status updates, final answers, review findings, explanations and plans

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

Real captured pairs from the committed benchmark run: same model
(`claude-sonnet-5`), same prompt, only the communication policy differs. Both
answers below kept **every** fact the case required and obeyed the requested
shape — the work is equal, only the wording is shorter. Full evidence in
[`evals/releases/v0.3.0/`](./evals/releases/v0.3.0/report.md).

### Review findings (`out-dev-22`)

Prompt supplies three confirmed defects with exact locations and asks for
findings only, labelled P1–P3.

<table>
<tr><th width="50%">No policy — 804 tokens</th><th width="50%">Simple Man — 243 tokens</th></tr>
<tr valign="top">
<td>

<b>Security Findings — session-service</b>

<b>P1 — Plaintext Session Token Storage</b><br>
<b>Location:</b> <code>auth/session.py:41</code>

The session token is stored in plaintext in the <code>sessions</code> table. If the database is compromised (via SQL injection, backup leak, insider access, or misconfigured replica), an attacker can directly hijack any active session without needing to intercept traffic or crack anything.

<b>Recommendation:</b> Store only a cryptographic hash (e.g., SHA-256) of the session token, similar to password hashing practice. Validate incoming tokens by hashing them and comparing against the stored hash…

<i>(continues for P2 and P3 at similar length)</i>

</td>
<td>

P1. <code>auth/session.py:41</code> — session token stored in plaintext in <code>sessions</code> table. Compromise of DB yields usable session tokens directly. Fix: store salted hash (e.g. SHA-256) of token, compare hash on lookup.

P2. <code>auth/session.py:88</code> — session ID regenerated on login but not on privilege elevation (role change). Enables session fixation for privilege escalation: attacker-controlled session ID retained after role change. Fix: regenerate session ID on any privilege change, not just login.

P3. <code>middleware/csrf.py:19</code> — CSRF token compared with <code>==</code>. Non-constant-time comparison enables timing attack to brute-force token. Fix: use <code>hmac.compare_digest</code>.

</td>
</tr>
<tr><td colspan="2"><b>70% fewer output tokens.</b> All three findings, all three locations, all three fixes retained.</td></tr>
</table>

### Status update (`out-dev-14`)

<table>
<tr><th width="50%">No policy — 260 tokens</th><th width="50%">Simple Man — 115 tokens</th></tr>
<tr valign="top">
<td>

<b>Subject: inventory-sync — Kafka consumer rewrite: canary status update</b>

Hi [Manager],

Quick update on the inventory-sync canary rollout:

<ul>
<li><b>Deployed:</b> Kafka consumer rewrite is live on canary (10%).</li>
<li><b>Performance:</b> p99 latency at 340ms — within our 500ms SLO.</li>
<li><b>Issue:</b> Pod <code>inventory-sync-7f9d8</code> is failing its readiness probe with <code>connection refused</code> on port 9090.</li>
<li><b>Action taken:</b> Paused rollout to the remaining 90% of pods pending investigation.</li>
</ul>

Will update once we've root-caused the probe failure and confirmed it's safe to resume.

Thanks,<br>[Your name]

</td>
<td>

Subject: inventory-sync status

Kafka consumer rewrite deployed to canary. p99 latency 340ms (within 500ms SLO). Pod inventory-sync-7f9d8 failing readiness probe: connection refused on port 9090. Rollout to remaining 90% paused pending fix.

</td>
</tr>
<tr><td colspan="2"><b>56% fewer output tokens.</b> The failing probe and the paused rollout — the two facts that change what the manager does — both survive.</td></tr>
</table>

These two are illustrative, not the headline. The headline is the whole corpus,
below.

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

Two live runs on `claude-sonnet-5`, 1,448 calls total, all evidence committed
under [`evals/releases/`](./evals/releases/). `make bench-v3-check` fails if any
published number cannot be rebuilt from the raw records, and a test enforces that
for every release, not just the newest.

Latest run: 84 output cases across 12 categories, 40 activation cases, 3 coding
fixtures, 38% of prompts Russian.

Five arms, each the same neutral prelude plus one policy: **N** none, **A** the
shipped policy, **B2** a candidate, **G** one sentence of "be concise", **C** the
external Caveman skill.

| Arm | Output vs no policy | 95% CI | Facts kept | Format kept |
| --- | ---: | --- | ---: | ---: |
| A (shipped) | −66.3% | [−53.5%, −71.8%] | 57.1% | 82.1% |
| C (Caveman) | −40.3% | [−27.7%, −51.1%] | 59.5% | 82.1% |
| G ("be concise") | −33.4% | [−23.9%, −37.4%] | 67.9% | 82.1% |
| B2 (candidate) | −32.4% | [−23.2%, −43.8%] | 66.7% | 81.0% |
| N (none) | — | — | 66.7% | 76.2% |

The trade is visible in that table and we publish it rather than hide it: the
shipped policy is by far the shortest **and** keeps the fewest required facts.
Compression is not free.

On coding fixtures every arm scores 2 of 3, including the arm with no policy —
the policy neither helps nor harms real task success here.

**Neither candidate shipped.** The first cleared 10 of 11 preregistered gates,
the second 9 of 12. The second beats the shipped policy decisively — blind
preference 48 wins to 8, fact retention 66.7% against 57.1% — but finishes level
with one sentence of "be concise": 19 wins to 20, and 0.3% longer. For this model
and corpus, a 337-word policy and one sentence land in the same place. Full
reasoning, including a gate we specified wrong, is in
[`gates.md`](./evals/releases/v0.3.1/gates.md).

The holdout wave, written by authors who had seen neither the earlier results nor
any candidate, tracks the development corpus closely (+35.3% versus +27.4%
against no policy), so nothing here was tuned to the set it was developed on.

Gates and inputs are
[preregistered](./evals/releases/v0.3.1/preregistration.json) by commit before
the first call, and a test rehashes every pinned input.

Older Codex-based suites and what runs offline are described in
[`evals/README.md`](./evals/README.md).

## Recommended usage

Use it as the default communication layer when you want minimum user-facing words without reducing search, validation, or implementation effort.

## License

MIT — see [LICENSE](./LICENSE).
