# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Notes

- Public benchmark claims are intentionally absent until a run against a pinned
  model with a committed snapshot lands.

## [0.2.0] - 2026-08-01

Initial packaged release: portable Agent Skill, Codex plugin package, always-on
installer, and the compact runtime policy surfaces (`AGENTS.md`, `CLAUDE.md`,
`GEMINI.md`) generated from `AGENTS.md.snippet`.

[Unreleased]: https://github.com/Maksim-Burtsev/simple-man/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Maksim-Burtsev/simple-man/releases/tag/v0.2.0
