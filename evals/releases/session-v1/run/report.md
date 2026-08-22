# Session benchmark report

Generated from `trials.jsonl` by `evals/session/session_report.py` (schema `session-report-v1`).

- SkillsBench commit: `aafac12f5dc6aa18dd9675b714f52d0926188867`
- Registered: model `anthropic/claude-sonnet-5`, effort `low`, agent `claude-code@2.1.235`
- Observed: model `claude-sonnet-5`; CLI `2.1.235`
- Trials: 266 (N 87, B2 90, G 89); metered cost $129.96 (Claude Code's own estimate; billed to the subscription)

## Delivery (mechanical)

| arm | trials | payload reached the agent command |
|---|--:|--:|
| N | 87 | 0/87 |
| B2 | 90 | 90/90 |
| G | 89 | 89/89 |

## Trials by day

- 2026-08-21: B2 26, N 40
- 2026-08-22: B2 64, G 89, N 47

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

## G vs N — 80 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median N → G |
|---|--:|--:|--:|--:|--:|
| cost | -0.7% | [-13.2%, +3.7%] | 0.123 | -13.0% | 0.3666 → 0.3319 |
| total tokens | -1.0% | [-11.9%, +2.7%] | 0.194 | -16.8% | 5.707e+05 → 4.469e+05 |
| output tokens | -2.0% | [-12.5%, +5.5%] | 0.237 | -16.4% | 4916 → 4518 |
| cache reads | -1.7% | [-12.2%, +2.8%] | 0.201 | -17.0% | 5.484e+05 → 4.265e+05 |
| fresh input | +0.0% | [-8.3%, +0.0%] | 0.287 | -12.2% | 26 → 24 |
| turns | +0.0% | [-9.5%, +1.0%] | 0.169 | -12.3% | 15.5 → 14.5 |
| wall-clock | -3.5% | [-13.9%, +0.6%] | 0.125 | -14.0% | 7.838e+04 → 7.24e+04 |
| quality (reward) | 11↑ / 12↓ / 57 tie | — | sign 1.000 | — | mean 0.560 → 0.529; pass 41 → 40 |

One-sided failures (pending retry, not counted): `latex-formula-extraction` (G: ApiRateLimitError)

Dropped symmetrically (failed on both arms): `bike-rebalance`, `earthquake-phase-association`, `multilingual-video-dubbing`, `python-scala-translation`, `radar-vital-signs`, `seismic-phase-picking`

## B2 vs G — 80 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median G → B2 |
|---|--:|--:|--:|--:|--:|
| cost | +13.1% | [+1.8%, +19.4%] | 0.036 | +11.7% | 0.3319 → 0.4096 |
| total tokens | +13.2% | [+0.9%, +32.6%] | 0.048 | +13.0% | 4.469e+05 → 5.344e+05 |
| output tokens | +14.2% | [+0.9%, +22.2%] | 0.092 | +15.2% | 4518 → 4929 |
| cache reads | +13.5% | [+0.9%, +34.5%] | 0.053 | +13.0% | 4.265e+05 → 5.075e+05 |
| fresh input | +10.4% | [+0.0%, +28.6%] | 0.048 | +13.0% | 24 → 26 |
| turns | +10.8% | [+0.0%, +24.4%] | 0.045 | +12.0% | 14.5 → 16 |
| wall-clock | +3.6% | [-1.4%, +17.5%] | 0.267 | +2.9% | 7.24e+04 → 8.528e+04 |
| quality (reward) | 8↑ / 15↓ / 57 tie | — | sign 0.210 | — | mean 0.529 → 0.475; pass 40 → 35 |

One-sided failures (pending retry, not counted): `latex-formula-extraction` (G: ApiRateLimitError)

Dropped symmetrically (failed on both arms): `bike-rebalance`, `earthquake-phase-association`, `multilingual-video-dubbing`, `python-scala-translation`, `radar-vital-signs`, `seismic-phase-picking`

Wilcoxon p-values use the normal approximation with tie and continuity correction; the sign test is exact and two-sided. Negative deltas mean the candidate used less.
