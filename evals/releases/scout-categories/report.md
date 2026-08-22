# Simple Man benchmark

Model: `claude-sonnet-5`  Judge: `claude-haiku-4-5`  CLI: `2.1.235 (Claude Code)`
Output cases: 60  Activation cases: 0
Live calls: 420  Reported cost: $6.15

Every number below is recomputed from the raw records by
`evals/bench/report.py`; `make bench-v3-check` fails if it cannot be rebuilt.

## Output length

| Comparison | n | Median reduction | 95% CI | Median tokens |
| --- | ---: | ---: | --- | ---: |
| B2 vs N | 60 | +51.9% | [+43.1%, +56.1%] | 326 vs 702 |
| B2 vs G | 60 | +33.7% | [+13.7%, +37.8%] | 326 vs 498 |
| G vs N | 60 | +25.7% | [+19.2%, +29.9%] | 498 vs 702 |

## Retention

Split because the two failures mean different things. *Facts* is the share
of cases keeping every required fact and making no forbidden claim. *Format*
is the share obeying an explicitly requested shape, such as exactly four
numbered steps. *Both* is the strict combination, and is the gated metric.

| Arm | Facts | Format | Both |
| --- | ---: | ---: | ---: |
| B2 | 71.7% | 98.3% | 70.0% |
| G | 73.3% | 100.0% | 73.3% |
| N | 71.7% | 100.0% | 71.7% |

## Retention by category

Facts / Format per arm, split by case category. Published because the
product argument names specific categories — refusals that carry the safe
procedure, findings that carry their fix, failed checks that report the
exact failure — and a single pooled number cannot show whether the policy
actually wins there.

**This table does not separate the arms, and is not evidence that it does.**
Each cell holds 20 cases, so a single case moves a rate by 5.0 points
and nothing below roughly two cases of difference carries information.
Read it as a map of where the corpus is thin, not as a scoreboard: on this
corpus the candidate is level with, or behind, the one-sentence control in
several of the categories the product argument leans on. Deciding those
categories needs a corpus built for that question, not this one re-read.

| Category | Cases | B2 | G | N |
| --- | ---: | ---: | ---: | ---: |
| destructive_risk | 20 | 30.0% / 100.0% | 35.0% / 100.0% | 40.0% / 100.0% |
| security | 20 | 85.0% / 100.0% | 90.0% / 100.0% | 90.0% / 100.0% |
| status | 20 | 100.0% / 95.0% | 95.0% / 100.0% | 85.0% / 100.0% |

## Blind pairwise preference

Each case judged in both orderings; a win requires winning both.

| Comparison | Wins | Ties | Cases |
| --- | --- | ---: | ---: |
| B2-vs-G | B2 15, G 23 | 22 | 60 |
| B2-vs-N | B2 11, N 23 | 26 | 60 |

Judge chose the left position in 41.0% of 227 decided judgments. Far from 50% would indicate position bias rather than a real preference.

## By category

Candidate against no policy, per case category.

| Category | n | Median reduction |
| --- | ---: | ---: |
| destructive_risk | 20 | +52.4% |
| security | 20 | +61.0% |
| status | 20 | +4.0% |

## Limits

- One model (`claude-sonnet-5`), one CLI version, one run per case. No repeats.
- The CLI exposes no temperature or seed control, so runs are not reproducible
  bit-for-bit. Confidence intervals are clustered on case id.
- Activation is measured as a routing decision from the skill description,
  not as a live end-to-end dispatch inside an agent session.
- Results are scoped to this corpus, model and CLI. They are not a universal
  claim about token cost.
