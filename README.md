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

Measured before/after examples are being regenerated against a pinned model and a
committed benchmark snapshot. Until that lands, this section is intentionally
empty rather than showing unreproducible numbers.

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

Benchmark harnesses live in [`evals/`](./evals/README.md). No canonical result
snapshot is committed yet — see [`evals/README.md`](./evals/README.md) for what
runs offline, what needs live model calls, and what each suite actually measures.

## Recommended usage

Use it as the default communication layer when you want minimum user-facing words without reducing search, validation, or implementation effort.

## License

MIT — see [LICENSE](./LICENSE).
