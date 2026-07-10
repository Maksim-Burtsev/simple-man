<p align="center">
  <img src="assets/icon.png" alt="Simple Man icon" width="160">
</p>

# Simple Man

High-compression professional communication mode for coding agents.

Simple Man is not a persona. It is a communication policy:

> Minimum user-facing words; same work quality.

It is designed for users who work with agents for many hours and want lower cognitive load without making the agent passive, less careful, or less proactive.

## What it changes

It compresses user-facing communication:

- no preambles
- no praise
- no filler
- no repeated recaps
- no generic closing offers
- sentence fragments and compact labels when clear
- compact status updates
- compact final answers
- compact review findings
- compact explanations and plans

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

Same model, same failing mini-project, same task. Only the communication policy changed. These are real captured Codex final answers from [`evals/reports/codex-skill-comparison.md`](./evals/reports/codex-skill-comparison.md).

### Expired-session auth bug

Prompt: inspect a Node auth API where expired sessions are still accepted, fix it, run tests, and hand off root cause, files changed, validation, and remaining risk.

<table>
<tr>
<th width="50%">No Simple Man</th>
<th width="50%">With Simple Man</th>
</tr>
<tr>
<td>
<p><strong>Engineering Handoff</strong></p>
<p><strong>Root cause:</strong> <code>authenticate</code> accepted any session returned by <code>store.get(token)</code> and never checked <code>session.expiresAt</code> against the store clock.</p>
<p><strong>Files changed:</strong></p>
<ul>
<li><code>src/middleware.js</code>: rejects sessions with missing/non-numeric <code>expiresAt</code> or <code>expiresAt &lt;= store.now()</code>.</li>
<li><code>test/auth.test.js</code>: added coverage for the expiration boundary where <code>expiresAt === now</code>.</li>
</ul>
<p><strong>Validation:</strong> <code>npm test</code> passed, 3/3 tests.</p>
<p><strong>Remaining risk:</strong> This assumes <code>expiresAt</code> and <code>store.now()</code> use the same timestamp unit, and that auth store implementations provide the existing <code>now()</code> API.</p>
</td>
<td>
<p>Fixed.</p>
<ul>
<li>Root cause: <code>authenticate</code> trusted <code>store.get(token)</code> and never checked <code>session.expiresAt</code>.</li>
<li>Files changed: <code>src/middleware.js</code></li>
<li>Validation: <code>npm test</code> -&gt; pass, <code>2/2</code> tests.</li>
<li>Remaining risk: none identified.</li>
</ul>
</td>
</tr>
<tr>
<td colspan="2"><strong>Result:</strong> both fixed the bug and passed <code>npm test</code>. Answer length: 601 chars vs 221 chars, 63% shorter.</td>
</tr>
</table>

### Payment idempotency retry bug

Prompt: inspect a Python payment ledger where retrying with the same idempotency key creates another local charge, fix it, run tests, and hand off root cause, files changed, validation, and remaining risk.

<table>
<tr>
<th width="50%">No Simple Man</th>
<th width="50%">With Simple Man</th>
</tr>
<tr>
<td>
<p><strong>Root cause</strong></p>
<p>The charge path was append-only. After a gateway timeout, the fake provider had already accepted a charge, but retrying with the same idempotency key created a new provider charge and the ledger had no local idempotency lookup to return an existing local charge.</p>
<p><strong>Files Changed</strong></p>
<ul>
<li><code>ledger.py</code>: added idempotency-key indexes in <code>FakeGateway</code> and <code>PaymentLedger</code>.
<ul>
<li>Provider retries now replay the original remote charge.</li>
<li>Ledger retries now return the existing local charge instead of appending another.</li>
</ul>
</li>
<li><code>test_ledger.py</code>: extended the retry test to verify no extra gateway call or local charge is created after the local charge exists.</li>
</ul>
<p><strong>Validation</strong></p>
<p>Command: <code>PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v</code></p>
<p>Result: passed, 1 test run.</p>
<p><strong>Remaining Risk</strong></p>
<p>This is still an in-memory implementation. A production version would need persistent idempotency storage with transaction/concurrency protection, and likely validation that reused idempotency keys match the original customer and amount.</p>
</td>
<td>
<p>Root cause: no idempotency-key lookup; timeout lost the accepted provider charge locally, and retries created new charge records.</p>
<p>Files changed:</p>
<ul>
<li><code>ledger.py</code>: added provider/local idempotency maps; same key returns existing charge; mismatched reuse raises <code>ValueError</code>.</li>
<li><code>test_ledger.py</code>: added regression for repeated same-key retry.</li>
</ul>
<p>Validation: <code>PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v</code> -&gt; passed, 2 tests.</p>
<p>Remaining risk: idempotency state is in-memory only; no durable/concurrent store in this fixture.</p>
</td>
</tr>
<tr>
<td colspan="2"><strong>Result:</strong> both fixed the retry bug and passed <code>python3 -m unittest -v</code>. Answer length: 1022 chars vs 520 chars, 49% shorter.</td>
</tr>
</table>

### Unsafe migration rollout order

Prompt: inspect a SQLite rollout where a migration drops <code>legacy_sessions.expires_at</code> before the backup reads it, fix it, run tests, and hand off root cause, files changed, validation, and remaining risk.

<table>
<tr>
<th width="50%">No Simple Man</th>
<th width="50%">With Simple Man</th>
</tr>
<tr>
<td>
<p><strong>Root Cause</strong></p>
<p><code>rollout.py</code> applied <code>migrations/001_drop_expires_at.sql</code> before running <code>backup_legacy_sessions.sql</code>. The backup query still selects <code>legacy_sessions.expires_at</code>, so the column was gone before the backup could read it.</p>
<p><strong>Files Changed</strong></p>
<p><code>rollout.py</code>: moved <code>backup_legacy_sessions(conn)</code> before <code>apply_drop_migration(conn)</code>.</p>
<p><strong>Validation</strong></p>
<p>Command: <code>python3 -m unittest test_rollout.py</code></p>
<p>Result: passed, 1 test ran OK.</p>
<p><strong>Remaining Risk</strong></p>
<p>The focused test now covers the required order: backup reads <code>expires_at</code>, then the migration drops it. Remaining risk is limited to behavior outside this small rollout path, such as real backup storage durability or additional migrations not represented in the current test.</p>
</td>
<td>
<p>Root cause: <code>rollout()</code> ran <code>apply_drop_migration()</code> before <code>backup_legacy_sessions()</code>, so <code>expires_at</code> was dropped before the backup query selected it.</p>
<p>Files changed: <code>rollout.py</code></p>
<p>Validation: <code>python3 -m unittest test_rollout.py</code> passed, 1 test.</p>
<p>Remaining risk: none known; only the focused repo test exists and was run.</p>
</td>
</tr>
<tr>
<td colspan="2"><strong>Result:</strong> both fixed the rollout order and passed <code>python3 -m unittest -v</code>. Answer length: 736 chars vs 324 chars, 56% shorter.</td>
</tr>
</table>

## Agent support

This repo ships two activation surfaces:

- full skill: `skills/simple-man/SKILL.md`
- compact always-on runtime policy: `AGENTS.md`, `AGENTS.md.snippet`, `CLAUDE.md`, `GEMINI.md`

| Agent/tool | Path |
| --- | --- |
| OpenAI Codex / Agent Skills | `skills/simple-man/SKILL.md`, `AGENTS.md`, `AGENTS.md.snippet` |
| Claude Code | `CLAUDE.md`, optional global skill copy |
| Gemini CLI | `GEMINI.md`, or configure Gemini to read `AGENTS.md` |
| Qwen Code | `AGENTS.md`, optional global skill copy |
| Cursor / Windsurf / Cline / Copilot / Continue / Zed / Junie | `AGENTS.md`, or copy `AGENTS.md.snippet` into that agent's native rule file |
| Amp / OpenCode / Kilo / Roo / Aider / other AGENTS.md agents | `AGENTS.md` |

Always-on project files do not invoke `$simple-man`; they inline a compact runtime
policy to avoid loading full skill overhead on every turn.

Agent-specific dotdir rule files are not committed here by default. They are target-project activation files, not the source of the skill.

See [INSTALL.md](./INSTALL.md) for per-agent setup notes.

## Benchmark

This repo includes two Codex-based token benchmark suites:

- `runtime_economics`: coding-agent cost, including instruction overhead and
  long-session amortized net.
- `reference_compression`: Caveman README-style output compression against a
  verbose normal helpful baseline.

```bash
make review-automatic
make review-quality
make review-auto
make bench-dry-run
make bench-refresh
make bench
make bench-check
make bench-compare-sample
make bench-reference-dry-run
make bench-reference-refresh
make bench-reference
make bench-reference-check
```

`review-automatic` runs the repo-quality gate first, then the held-out
communication gate. Both save replayable raw evidence and must pass before the
candidate policy is promoted or human blind review starts.

The benchmark compares `control`, generic `terse`, `simple_man_runtime`,
`simple_man_candidate`, `simple_man_skill`, and optional Caveman arms. Runtime
headlines use output compression and long-session net; reference headlines use
output-only compression vs `normal`.

See [evals/README.md](./evals/README.md).

## Install

Install Simple Man as an always-on Codex communication policy:

```bash
curl -fsSL https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/master/install.sh | bash
```

**Important: Simple Man is always-on after install. This is expected.**

The installer copies `skills/simple-man` to `~/.codex/skills/simple-man` and writes the compact runtime policy into `~/.codex/AGENTS.md`.

## Codex plugin package

To add the repo-local plugin package to Codex:

```bash
codex plugin marketplace add Maksim-Burtsev/simple-man --ref master
codex plugin add simple-man@simple-man
```

Plugin install makes the skill available in Codex. Always-on behavior comes from the installer writing global Codex instructions.

## Recommended usage

Use it as the default communication layer for Codex when you want minimum user-facing words without reducing search, validation, or implementation effort.
