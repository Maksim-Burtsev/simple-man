# Session benchmark report

Generated from `trials.jsonl` by `evals/session/session_report.py` (schema `session-report-v1`).

- SkillsBench commit: `aafac12f5dc6aa18dd9675b714f52d0926188867`
- Registered: model `anthropic/claude-sonnet-5`, effort `low`, agent `claude-code@2.1.235`
- Observed: model `claude-sonnet-5`; CLI `2.1.235`
- Trials: 264 (N 87, B2 90, G 87); metered cost $129.05 (Claude Code's own estimate; billed to the subscription)

## Delivery (mechanical)

| arm | trials | payload reached the agent command |
|---|--:|--:|
| N | 87 | 0/87 |
| B2 | 90 | 90/90 |
| G | 87 | 87/87 |

## Trials by day

- 2026-08-21: B2 26, N 40
- 2026-08-22: B2 64, G 87, N 47

## B2 vs N — 81 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median N → B2 |
|---|--:|--:|--:|--:|--:|
| cost | +2.8% | [-7.2%, +10.7%] | 0.713 | -2.9% | 0.3691 → 0.4086 |
| total tokens | +2.1% | [-6.4%, +15.8%] | 0.710 | -6.0% | 5.721e+05 → 5.503e+05 |
| output tokens | -1.9% | [-10.1%, +9.0%] | 0.767 | -3.8% | 4898 → 4779 |
| cache reads | +1.5% | [-7.5%, +16.3%] | 0.763 | -6.2% | 5.541e+05 → 5.257e+05 |
| fresh input | +0.0% | [-5.9%, +14.3%] | 0.805 | -0.9% | 26 → 26 |
| turns | +0.0% | [-5.9%, +10.0%] | 0.993 | -1.9% | 16 → 16 |
| wall-clock | -0.9% | [-9.8%, +10.4%] | 0.767 | -11.8% | 7.957e+04 → 8.665e+04 |
| quality (reward) | 8↑ / 14↓ / 59 tie | — | sign 0.286 | — | mean 0.553 → 0.469; pass 41 → 35 |

Dropped symmetrically (failed on both arms): `bike-rebalance`, `earthquake-phase-association`, `multilingual-video-dubbing`, `python-scala-translation`, `radar-vital-signs`, `seismic-phase-picking`

## G vs N — 79 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median N → G |
|---|--:|--:|--:|--:|--:|
| cost | +0.4% | [-11.7%, +3.7%] | 0.164 | -12.1% | 0.3641 → 0.3268 |
| total tokens | -0.7% | [-11.9%, +4.7%] | 0.256 | -15.9% | 5.694e+05 → 4.37e+05 |
| output tokens | -1.0% | [-12.5%, +6.5%] | 0.313 | -15.5% | 4898 → 4418 |
| cache reads | -1.7% | [-12.2%, +4.7%] | 0.264 | -16.0% | 5.427e+05 → 4.174e+05 |
| fresh input | +0.0% | [-8.3%, +0.0%] | 0.370 | -11.6% | 26 → 24 |
| turns | +0.0% | [-9.5%, +2.0%] | 0.230 | -11.7% | 15 → 14 |
| wall-clock | -3.2% | [-13.9%, +1.5%] | 0.166 | -13.2% | 7.719e+04 → 7.165e+04 |
| quality (reward) | 11↑ / 11↓ / 57 tie | — | sign 1.000 | — | mean 0.555 → 0.536; pass 40 → 40 |

One-sided failures (pending retry, not counted): `energy-unit-commitment` (G: AgentTimeoutError), `latex-formula-extraction` (G: RuntimeError)

Dropped symmetrically (failed on both arms): `bike-rebalance`, `earthquake-phase-association`, `multilingual-video-dubbing`, `python-scala-translation`, `radar-vital-signs`, `seismic-phase-picking`

## B2 vs G — 79 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median G → B2 |
|---|--:|--:|--:|--:|--:|
| cost | +12.4% | [+0.7%, +22.3%] | 0.045 | +11.6% | 0.3268 → 0.4086 |
| total tokens | +10.4% | [+0.2%, +32.3%] | 0.062 | +12.7% | 4.37e+05 → 5.185e+05 |
| output tokens | +14.4% | [+0.5%, +22.7%] | 0.105 | +15.6% | 4418 → 4779 |
| cache reads | +10.7% | [+0.1%, +33.7%] | 0.067 | +12.7% | 4.174e+05 → 4.893e+05 |
| fresh input | +8.3% | [+0.0%, +28.6%] | 0.061 | +12.7% | 24 → 26 |
| turns | +10.5% | [+0.0%, +25.0%] | 0.057 | +11.8% | 14 → 16 |
| wall-clock | +3.1% | [-2.3%, +16.1%] | 0.337 | +2.2% | 7.165e+04 → 8.39e+04 |
| quality (reward) | 8↑ / 15↓ / 56 tie | — | sign 0.210 | — | mean 0.536 → 0.481; pass 40 → 35 |

One-sided failures (pending retry, not counted): `energy-unit-commitment` (G: AgentTimeoutError), `latex-formula-extraction` (G: RuntimeError)

Dropped symmetrically (failed on both arms): `bike-rebalance`, `earthquake-phase-association`, `multilingual-video-dubbing`, `python-scala-translation`, `radar-vital-signs`, `seismic-phase-picking`

Wilcoxon p-values use the normal approximation with tie and continuity correction; the sign test is exact and two-sided. Negative deltas mean the candidate used less.
