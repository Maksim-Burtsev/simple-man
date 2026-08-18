# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Lean Claude Code benchmark under `evals/bench/`: a runner, a blind pairwise
  judge, and a report that recomputes every number from the raw records. Runs on
  a Claude subscription with no API key, and refuses to start if any Anthropic
  credential in the environment could move the run onto API billing.
- Benchmark corpus: 60 output cases across 12 categories and 30 activation
  cases, in `evals/cases/bench-*.jsonl`, with guard tests for size, category
  replication, language mix and cluster independence.
- Make targets `bench-v3-dry-run`, `bench-v3`, `bench-v3-report`,
  `bench-v3-check`.
- Benchmark policy arms under `evals/policies/`: `v0.2/` freezes the shipped
  policy as the baseline arm, `v0.3/` holds candidate successors plus a
  one-sentence generic-terse control. Candidates are **not** shipped; they are
  promoted only if they clear the release gates on a live run.
- `tests/test_policies.py` guards both directions: the frozen baseline must stay
  byte-identical to the shipped policy, and a candidate must never silently
  become the shipped policy.
- CI status badge and this changelog.
- Vendored copy of the exact external Caveman `SKILL.md` used as a comparison
  arm, under `evals/policies/external/`, so that comparison is auditable and
  reproducible from the repository alone.

### Changed

- README rewritten as a product page: Claude Code is now a first-class install
  path with verified commands, and internal release-process narration was
  removed.
- `evals/README.md` now states plainly which targets run offline, which require
  live model calls and credentials, and that no canonical snapshot is committed.

### Fixed

- `test_model_answer_profile_real_macos_probe_is_ready_and_reaps_exact_tag` no
  longer fails when the host process list is busy. The probe returning
  `INCONCLUSIVE` is the harness's own "cannot determine" state, not a defect in
  the code under test, so it now skips instead of failing. The test is skipped
  on Linux CI, so this failure was only ever visible locally on macOS.

### Results

- Second benchmark run: 848 live calls, evidence in `evals/releases/v0.3.1/`.
  Adds a coding phase, a 24-case holdout wave written blind, and harder
  activation cases. The second candidate cleared 9 of 12 gates and **did not
  ship**: it beats the shipped policy decisively (blind preference 48–8, fact
  retention 66.7% vs 57.1%) but finishes level with a one-sentence control.
- The harder activation corpus finally separates the two skill descriptions: the
  shipped one scores 90% precision and lets two protected-category requests
  through, while the trigger-focused candidate scores 100% across the board.

- First published benchmark: 600 live calls on `claude-sonnet-5`, evidence in
  `evals/releases/v0.3.0/`. The shipped policy produces 53.9% fewer output
  tokens than no policy (95% CI 43.8–66.8%), and the report is rebuilt from the
  raw records by `make bench-v3-check`.
- Reported honestly: compression costs facts. No policy retains the most
  required facts (71.7%); every compression arm retains fewer. The policies
  recover that on obeying an explicitly requested output shape.
- The v0.3 candidate **did not ship**. It cleared 10 of 11 preregistered gates
  but lost blind preference to a one-sentence "be concise" control, 9 wins to
  23, concentrated in categories where the user asked for length. Published
  rather than patched.

## [0.2.0] - 2026-08-01

Initial packaged release: portable Agent Skill, Codex plugin package, always-on
installer, and the compact runtime policy surfaces (`AGENTS.md`, `CLAUDE.md`,
`GEMINI.md`) generated from `AGENTS.md.snippet`.

[Unreleased]: https://github.com/Maksim-Burtsev/simple-man/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Maksim-Burtsev/simple-man/releases/tag/v0.2.0
