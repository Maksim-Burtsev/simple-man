# Session benchmark report

Generated from `trials.jsonl` by `evals/session/session_report.py` (schema `session-report-v1`).

- SkillsBench commit: `aafac12f5dc6aa18dd9675b714f52d0926188867`
- Registered: model `anthropic/claude-sonnet-5`, effort `low`, agent `claude-code@2.1.235`
- Observed: model `claude-sonnet-5`; CLI `2.1.235`
- Trials: 6 (N 3, B2 3, G 0); metered cost $3.72 (Claude Code's own estimate; billed to the subscription)

## Delivery (mechanical)

| arm | trials | payload reached the agent command |
|---|--:|--:|
| N | 3 | 0/3 |
| B2 | 3 | 3/3 |
| G | 0 | 0/0 |

## Trials by day

- 2026-08-21: B2 3, N 3

## B2 vs N — 3 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median N → B2 |
|---|--:|--:|--:|--:|--:|
| cost | -13.3% | [-26.1%, +24.3%] | — | -7.1% | 0.6107 → 0.6359 |
| total tokens | -5.7% | [-26.4%, -0.7%] | — | -16.4% | 8.939e+05 → 8.43e+05 |
| output tokens | -23.3% | [-27.9%, +38.2%] | — | -10.9% | 5839 → 8069 |
| cache reads | -9.0% | [-26.4%, -3.3%] | — | -18.0% | 8.43e+05 → 7.669e+05 |
| fresh input | +0.0% | [-5.3%, +0.0%] | — | -2.6% | 32 → 32 |
| turns | +0.0% | [-5.0%, +30.4%] | — | +11.8% | 20 → 19 |
| wall-clock | -20.0% | [-27.4%, +92.5%] | — | +25.5% | 1.65e+05 → 1.545e+05 |
| quality (reward) | 1↑ / 1↓ / 1 tie | — | sign 1.000 | — | mean 0.610 → 0.833; pass 1 → 2 |

## G vs N — 0 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median N → G |
|---|--:|--:|--:|--:|--:|
| quality (reward) | — | — | — | — | — |

## B2 vs G — 0 clean pairs

| metric | median paired delta | 95% CI (bootstrap) | Wilcoxon p | totals delta | median G → B2 |
|---|--:|--:|--:|--:|--:|
| quality (reward) | — | — | — | — | — |

Wilcoxon p-values use the normal approximation with tie and continuity correction; the sign test is exact and two-sided. Negative deltas mean the candidate used less.
