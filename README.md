<p align="center">
  <img src="assets/icon.png" alt="Simple Man icon" width="160">
</p>

# Simple Man — High-Signal Agent Communication

High-compression professional communication mode for coding agents.

Simple Man is not a persona. It is a communication policy:

> Minimum user-facing words; same work quality.

It is designed for users who work with agents for many hours and want lower cognitive load without making the agent passive, less careful, or less proactive.

## Portable Agent Skill

Install the portable skill:

```bash
npx skills add Maksim-Burtsev/simple-man -g -a codex -s simple-man -y
```

This makes Simple Man available without changing global instructions or enabling an always-on policy. Invoke it explicitly with `$simple-man`, or let the agent activate it from the request.

## Codex Plugin

Install the Codex plugin package:

```bash
codex plugin marketplace add Maksim-Burtsev/simple-man --ref v0.2.0
codex plugin add simple-man@simple-man
```

The pinned v0.2.0 plugin makes the skill available; it does not enable the always-on policy. Its manifest still uses an always-on label; the source manifest for upcoming v0.3 removes that misleading wording. PR5 will update the release pin.

## Always-on Codex policy

The currently released v0.2.0 installer enables the compact runtime policy:

```bash
curl -fsSL https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/v0.2.0/install.sh | bash
```

That pinned installer writes `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, installs the skill under `${CODEX_HOME:-$HOME/.codex}/skills/simple-man`, and keeps `simple-man.backup` on rerun.

The installer in this source tree is the upcoming v0.3 contract: the AGENTS destination stays the same, while skill placement uses explicit overrides, an existing legacy install, then `$HOME/.agents/skills/simple-man`. It creates no backup skill and preserves pre-existing non-skill backup data. PR5 will change the pin to v0.3.0; see [INSTALL.md](./INSTALL.md) for the full source contract and project-level setup.

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

## Historical examples

The old captured comparison report is historical evidence, not current release evidence. Re-run the offline foundation checks and a reviewed live evaluation before publishing new benchmark claims.

## Agent support

This repo ships two activation surfaces. `AGENTS.md.snippet` is the canonical runtime policy; `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` are generated from it:

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

## Recommended usage

Use it as the default communication layer for Codex when you want minimum user-facing words without reducing search, validation, or implementation effort.
