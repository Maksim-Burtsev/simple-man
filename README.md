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

This repo ships both a canonical `SKILL.md` package and native instruction files for popular coding agents.

| Agent/tool | Supported path |
| --- | --- |
| OpenAI Codex / Agent Skills | `skills/simple-man/SKILL.md`, `AGENTS.md`, `AGENTS.md.snippet` |
| Claude Code | `.claude/skills/simple-man/SKILL.md`, `CLAUDE.md` |
| Qwen Code | `.qwen/skills/simple-man/SKILL.md`, `QWEN.md`, `AGENTS.md` |
| Cursor | `.cursor/rules/simple-man.mdc`, `AGENTS.md` |
| Windsurf Cascade | `.windsurf/rules/simple-man.md`, `AGENTS.md` |
| GitHub Copilot | `.github/copilot-instructions.md`, `AGENTS.md` where supported |
| Cline | `.clinerules/simple-man.md`, `AGENTS.md` |
| Continue | `.continue/rules/simple-man.md` |
| Zed Agent | `.rules`, `AGENTS.md` |
| JetBrains AI / Junie | `.junie/guidelines.md`, `.junie/AGENTS.md`, `AGENTS.md` |
| Amp / OpenCode / Kilo and other AGENTS.md agents | `AGENTS.md` |
| Aider | `CONVENTIONS.md` with `aider --read CONVENTIONS.md` or `/read CONVENTIONS.md` |

## Install as a global skill

Copy the canonical skill directory to the agent you use:

```bash
cp -R skills/simple-man ~/.codex/skills/simple-man
cp -R skills/simple-man ~/.claude/skills/simple-man
cp -R skills/simple-man ~/.qwen/skills/simple-man
```

For agents that use instruction files instead of skills, copy the matching file or directory from the table above into your project or global config.

For global AGENTS.md-style installs, add `AGENTS.md.snippet` to your global or repo-level `AGENTS.md`.

## Recommended usage

Test it without other brevity/persona skills enabled first.

If using it with other style rules, give Simple Man priority for final user-facing responses.
