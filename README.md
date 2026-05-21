<p align="center">
  <img src="assets/icon.png" alt="Simple Man icon" width="160">
</p>

# Simple Man

High-compression professional communication mode for coding agents.

Simple Man is not a persona. It is a communication policy:

> Minimum words, maximum signal.

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

## Benchmark

This repo includes a Codex-based token benchmark for checking whether
Simple Man reduces communication cost without hiding important engineering
facts.

```bash
make bench-dry-run
make bench-refresh
make bench
make bench-check
make bench-compare-sample
```

The benchmark compares `control`, generic `terse`, `simple_man`, and optional
`caveman` arms over 40 coding-agent tasks. It reports output compression,
first-turn net cost, and amortized net cost separately.

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
