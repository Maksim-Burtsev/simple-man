# Install

Simple Man is designed to be always-on.

**Important: Simple Man is always-on after install. This is expected.**

## Codex

```bash
curl -fsSL https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/master/install.sh | bash
```

The installer:

- copies `skills/simple-man` to `~/.codex/skills/simple-man`
- writes a managed Simple Man block into `~/.codex/AGENTS.md`
- replaces that managed block on rerun without duplicating it

Restart Codex after installing so new sessions load the global instructions.

## Project Files

This repo also ships lightweight always-on project policies for agents that read repository instructions:

| Agent/tool | Recommended project setup |
| --- | --- |
| OpenAI Codex | Commit `AGENTS.md`; optional: run the global Codex installer above |
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

Always-on project files inline the runtime policy instead of invoking `$simple-man`, so agents do not need to load the full skill on every turn.
