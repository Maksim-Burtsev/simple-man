# Install

## Portable Agent Skill

Install Simple Man without changing global communication policy:

```bash
npx skills add Maksim-Burtsev/simple-man -g -a codex -s simple-man -y
```

Invoke it explicitly with `$simple-man`, or let the agent activate it from the request.

## Codex Plugin

```bash
codex plugin marketplace add Maksim-Burtsev/simple-man --ref v0.2.0
codex plugin add simple-man@simple-man
```

Plugin install makes the skill available in Codex; it does not enable the always-on policy.

## Always-on Codex policy

```bash
curl -fsSL https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/v0.2.0/install.sh | bash
```

The installer:

- installs the skill to the first matching target below
- writes a managed Simple Man block into `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`
- replaces that managed block on rerun without duplicating it
- stages and validates both surfaces before replacing either one

Skill target precedence:

1. `$SIMPLE_MAN_SKILL_ROOT/simple-man` when `SIMPLE_MAN_SKILL_ROOT` is set to an absolute path
2. `$CODEX_HOME/skills/simple-man` when `CODEX_HOME` is explicitly set to an absolute path
3. existing legacy `$HOME/.codex/skills/simple-man`
4. `$HOME/.agents/skills/simple-man` for a new default install

An existing legacy install is updated in place; the installer does not create a second copy or migrate it to the portable root. Reruns do not leave discoverable backup skills. Empty or relative path overrides fail before any installation change.

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

`AGENTS.md.snippet` is canonical. Regenerate or verify `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and the plugin skill copy with:

```bash
python3 scripts/sync_surfaces.py --write
python3 scripts/sync_surfaces.py --check
```
