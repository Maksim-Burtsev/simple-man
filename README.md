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

Same model, same repository fixture, same prompt. Only Simple Man is changed.

### Merge-readiness update

Prompt: write a Slack update to an engineering manager about whether an auth
branch can merge today after inspecting auth code plus test and lint reports.

Without Simple Man:

```md
Slack-ready update:

Auth branch is **not safe to merge as-is today**. The test report is green: `pnpm test` passed for `src/auth/middleware.test.ts` and `src/billing/charges.test.ts` with 11 tests passing. However, lint is failing in `src/auth/session.ts:35` because `auditSessionRefresh(refreshed.id)` is called without `await`, `.catch`, or `void`.

Main risks:
- CI/lint is not clean, so this should block merge.
- `refreshSession` returns before the audit request completes, and audit failures could become unhandled promise rejections or silently lose audit coverage.
- `authMiddleware` accepts any session returned by `getSession`, but `getSession` does not check `expiresAt`; if an expired session remains in the map, it can still authenticate.

Next steps before merge:
- Fix the floating promise in `refreshSession` by either awaiting audit, handling failures, or explicitly marking it fire-and-forget with `void` plus error handling.
- Confirm/implement expired-session rejection in `getSession` or `authMiddleware`.
- Re-run lint and tests. If both are green after those fixes, this branch could still be mergeable today.
```

With Simple Man:

```md
Slack draft:

> Auth branch is not merge-ready today yet. Tests are green (`pnpm test`: 2 files / 11 tests passed), but lint is failing on `src/auth/session.ts:35` because `auditSessionRefresh(refreshed.id)` is an unhandled promise.
>
> I would not merge while lint is red. There's also an auth risk: sessions have `expiresAt`, but `getSession` / `authMiddleware` never reject expired sessions, so an expired token can still authenticate if it remains in the session map.
>
> Next: handle/await the audit promise, enforce session expiry, add an expired-session test, then rerun lint + tests.
```

### Deploy-channel safety reply

Prompt: reply to a deploy-channel suggestion to run a destructive drop-column
migration before taking the backup.

Without Simple Man:

```md
Reply with:

> Not approved to run the drop-column migration before the backup. Risk: the migration drops `legacy_sessions.expires_at`, and the backup script selects that same column, so a backup afterward would no longer be possible from `legacy_sessions`. Safe order: take and verify the legacy session backup first, then run `20260521090000_drop_legacy_session_expires_at.sql`, then deploy.
```

With Simple Man:

```md
Reply:

> Not approved as proposed. Risk: `expires_at` is dropped by the migration, and `scripts/backup-legacy-sessions.sql` selects `expires_at`, so backup afterward may fail or lose the legacy expiry data. Safe order: take/export the legacy session backup first, verify it, then run `20260521090000_drop_legacy_session_expires_at.sql`, then deploy.
```

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

The benchmark compares `control`, generic `terse`, `simple_man_runtime`,
`simple_man_candidate`, `simple_man_skill`, and optional Caveman arms. Runtime
headlines use output compression and long-session net; reference headlines use
output-only compression vs `normal`.

See [evals/README.md](./evals/README.md).

## Install as a global skill

Copy the canonical skill directory:

```bash
cp -R skills/simple-man ~/.codex/skills/simple-man
cp -R skills/simple-man ~/.claude/skills/simple-man
cp -R skills/simple-man ~/.qwen/skills/simple-man
```

## Recommended usage

Test it without other brevity/persona skills enabled first.

If using it with other style rules, give Simple Man priority for final user-facing responses.
