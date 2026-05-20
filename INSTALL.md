# Install

Simple Man has one behavior source:

- `skills/simple-man/SKILL.md`

The root files are lightweight auto-discovery hints for agents that read project instructions:

- `AGENTS.md`
- `CLAUDE.md`
- `GEMINI.md`

Agent-specific dotdir rule files are not committed by default. They are project-local activation files, not the skill source. Add them to a target project only when you want Simple Man always-on there.

## Global Skill Installs

```bash
cp -R skills/simple-man ~/.codex/skills/simple-man
cp -R skills/simple-man ~/.claude/skills/simple-man
cp -R skills/simple-man ~/.qwen/skills/simple-man
```

## Project Installs

| Agent/tool | Recommended project setup |
| --- | --- |
| OpenAI Codex | Commit `AGENTS.md`; optional: copy `skills/simple-man` to `~/.codex/skills/simple-man` |
| Claude Code | Commit `CLAUDE.md`; optional: copy `skills/simple-man` to `~/.claude/skills/simple-man` |
| Gemini CLI | Commit `GEMINI.md`, or configure Gemini to read `AGENTS.md` |
| Qwen Code | Commit `AGENTS.md`; optional: copy `skills/simple-man` to `~/.qwen/skills/simple-man` |
| Cursor | Commit `AGENTS.md`, or copy `AGENTS.md.snippet` into `.cursor/rules/simple-man.mdc` |
| Windsurf | Commit `AGENTS.md`, or copy `AGENTS.md.snippet` into `.windsurf/rules/simple-man.md` |
| Cline | Commit `AGENTS.md`, or copy `AGENTS.md.snippet` into `.clinerules/simple-man.md` |
| GitHub Copilot | Commit `AGENTS.md`, or copy `AGENTS.md.snippet` into `.github/copilot-instructions.md` |
| Continue | Copy `AGENTS.md.snippet` into a Continue rule, or use project instructions if configured |
| Zed Agent | Commit `AGENTS.md`, or point Zed rules at the same text |
| JetBrains Junie | Commit `AGENTS.md`, or copy `AGENTS.md.snippet` into Junie guidelines |
| Aider | Configure Aider to read `AGENTS.md` |
| Amp / OpenCode / Kilo / Roo / other AGENTS.md agents | Commit `AGENTS.md` |

## Qwen Note

Qwen Code supports both `QWEN.md` and `.qwen/skills/<name>/SKILL.md`, but this repo does not commit Qwen-specific mirrors. Qwen already reads `AGENTS.md`, and global skills belong in `~/.qwen/skills/simple-man`.
