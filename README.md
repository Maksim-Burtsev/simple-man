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

## Install

Copy the skill directory to your agent skills directory:

```bash
cp -R skills/simple-man ~/.agents/skills/simple-man
```

Then add `AGENTS.md.snippet` to the global or repo-level `AGENTS.md` so the behavior is used by default.

## Recommended usage

Test it without other brevity/persona skills enabled first.

If using it with other style rules, give Simple Man priority for final user-facing responses.
