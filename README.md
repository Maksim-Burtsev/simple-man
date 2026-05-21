<p align="center">
  <img src="assets/icon.png" alt="Simple Man icon" width="160">
</p>

# Simple Man

Ultra-low-noise professional communication mode for coding agents.

Simple Man is not a persona. It is a communication policy:

> Say the minimum that preserves decision quality.

It is designed for users who work with agents for many hours and want lower cognitive load without making the agent passive, less careful, or less proactive.

## What it changes

It compresses user-facing communication:

- no preambles
- no praise
- no filler
- no repeated recaps
- no generic closing offers
- compact status updates
- compact review findings

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

> Here's a Slack-ready update:
>
> Auth branch is not safe to merge yet today.
>
> Current status: unit tests are passing (`2` test files, `11` tests), including
> `src/auth/middleware.test.ts`. The middleware path looks basically correct for
> missing/invalid tokens: it returns `401`, and valid sessions return `200` with
> `userId`.
>
> The blocker is lint: `src/auth/session.ts` has a failing
> `@typescript-eslint/no-floating-promises` error because `refreshSession()`
> calls `auditSessionRefresh(refreshed.id)` without `await`, `.catch()`, or an
> explicit `void`. That means CI should fail, and more importantly audit failures
> could become unhandled promise rejections or silently miss session-refresh
> audit records.
>
> There is also a concrete auth risk to confirm before merge: `Session` has an
> `expiresAt` field and refresh updates it, but `getSession()` and
> `authMiddleware()` do not check expiration. As written, any token still present
> in the in-memory session map appears valid even if expired.
>
> Next steps: fix the audit promise handling, decide whether refresh auditing
> must block success or can be fire-and-forget with explicit error handling, and
> add/confirm expiration enforcement tests. Once lint passes and expiration
> behavior is confirmed, we can reassess for merge.

With Simple Man:

> Slack draft:
>
> Auth branch is not safe to merge as-is today.
>
> Current status: tests pass (`2` files, `11` tests), but lint fails in
> `src/auth/session.ts:35` because `auditSessionRefresh(...)` is called without
> `await`, `.catch`, or `void`.
>
> Main risks:
>
> - CI/release gate should fail on lint.
> - Session refresh audit failures may be unhandled or silently lost.
> - `getSession()` does not check `expiresAt`, so expired sessions could still
>   authenticate if present in memory.
>
> Next: fix the floating promise, confirm intended audit behavior, add/verify
> expiry enforcement, then rerun `pnpm test` and `pnpm lint`. I would not merge
> until those are green.

### Deploy-channel safety reply

Prompt: reply to a deploy-channel suggestion to run a destructive drop-column
migration before taking the backup.

Without Simple Man:

> Not approved as proposed. Risk: the migration drops
> `legacy_sessions.expires_at`, but the backup script still selects
> `expires_at`, so running the drop first can make the backup fail or lose that
> data. Safe order: take and verify the legacy session backup first, then run
> `20260521090000_drop_legacy_session_expires_at.sql`, then continue deploy.

With Simple Man:

> Not approved.
>
> Risk: the migration drops `legacy_sessions.expires_at`, but the backup script
> selects that column. If we drop first, the backup can't capture it afterward.
>
> Safe order: backup first, verify it, then run
> `migrations/20260521090000_drop_legacy_session_expires_at.sql`, then deploy.

## Agent support

This repo ships one canonical skill plus lightweight project instruction files.

| Agent/tool | Path |
| --- | --- |
| OpenAI Codex / Agent Skills | `skills/simple-man/SKILL.md`, `AGENTS.md`, `AGENTS.md.snippet` |
| Claude Code | `CLAUDE.md`, optional global skill copy |
| Gemini CLI | `GEMINI.md`, or configure Gemini to read `AGENTS.md` |
| Qwen Code | `AGENTS.md`, optional global skill copy |
| Cursor / Windsurf / Cline / Copilot / Continue / Zed / Junie | `AGENTS.md`, or copy `AGENTS.md.snippet` into that agent's native rule file |
| Amp / OpenCode / Kilo / Roo / Aider / other AGENTS.md agents | `AGENTS.md` |

Agent-specific dotdir rule files are not committed here by default. They are target-project activation files, not the source of the skill.

See [INSTALL.md](./INSTALL.md) for per-agent setup notes.

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
