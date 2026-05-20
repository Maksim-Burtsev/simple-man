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
