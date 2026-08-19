# Simple Man benchmark

Model: `claude-sonnet-5`  Judge: `claude-haiku-4-5`  CLI: `2.1.233 (Claude Code)`
Output cases: 60  Activation cases: 30
Live calls: 600  Reported cost: $9.40

Every number below is recomputed from the raw records by
`evals/bench/report.py`; `make bench-v3-check` fails if it cannot be rebuilt.

## Output length

| Comparison | n | Median reduction | 95% CI | Median tokens |
| --- | ---: | ---: | --- | ---: |
| B vs N | 60 | +52.4% | [+41.7%, +61.6%] | 296 vs 788 |
| B vs A | 60 | -6.0% | [-16.5%, -1.1%] | 296 vs 234 |
| B vs G | 60 | +27.9% | [+21.4%, +39.0%] | 296 vs 523 |
| A vs N | 60 | +53.9% | [+43.8%, +66.8%] | 234 vs 788 |
| C vs N | 60 | +47.9% | [+33.4%, +57.8%] | 370 vs 788 |
| G vs N | 60 | +26.2% | [+20.4%, +35.8%] | 523 vs 788 |

## Retention

Split because the two failures mean different things. *Facts* is the share
of cases keeping every required fact and making no forbidden claim. *Format*
is the share obeying an explicitly requested shape, such as exactly four
numbered steps. *Both* is the strict combination, and is the gated metric.

| Arm | Facts | Format | Both |
| --- | ---: | ---: | ---: |
| A | 61.7% | 80.0% | 45.0% |
| B | 66.7% | 80.0% | 55.0% |
| C | 61.7% | 78.3% | 45.0% |
| G | 68.3% | 76.7% | 50.0% |
| N | 71.7% | 73.3% | 46.7% |

## Retention by category

Facts / Format per arm, split by case category. Published because the
product argument names specific categories — refusals that carry the safe
procedure, findings that carry their fix, failed checks that report the
exact failure — and a single pooled number cannot show whether the policy
actually wins there.

**This table does not separate the arms, and is not evidence that it does.**
Each cell holds 5 cases, so a single case moves a rate by 20.0 points
and nothing below roughly two cases of difference carries information.
Read it as a map of where the corpus is thin, not as a scoreboard: on this
corpus the candidate is level with, or behind, the one-sentence control in
several of the categories the product argument leans on. Deciding those
categories needs a corpus built for that question, not this one re-read.

| Category | Cases | A | B | C | G | N |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| creative_override | 5 | 80.0% / 40.0% | 60.0% / 40.0% | 80.0% / 40.0% | 80.0% / 40.0% | 60.0% / 40.0% |
| destructive_risk | 5 | 0.0% / 100.0% | 20.0% / 100.0% | 0.0% / 100.0% | 0.0% / 100.0% | 20.0% / 100.0% |
| detailed_override | 5 | 60.0% / 0.0% | 40.0% / 0.0% | 60.0% / 0.0% | 60.0% / 0.0% | 60.0% / 0.0% |
| diagnosis | 5 | 60.0% / 100.0% | 40.0% / 100.0% | 40.0% / 100.0% | 60.0% / 100.0% | 80.0% / 100.0% |
| failed_validation | 5 | 80.0% / 100.0% | 80.0% / 100.0% | 60.0% / 100.0% | 60.0% / 100.0% | 80.0% / 100.0% |
| final | 5 | 40.0% / 100.0% | 60.0% / 100.0% | 80.0% / 100.0% | 40.0% / 100.0% | 20.0% / 100.0% |
| plan | 5 | 60.0% / 100.0% | 100.0% / 100.0% | 40.0% / 100.0% | 100.0% / 100.0% | 100.0% / 60.0% |
| review | 5 | 60.0% / 100.0% | 100.0% / 100.0% | 80.0% / 80.0% | 100.0% / 60.0% | 100.0% / 60.0% |
| security | 5 | 60.0% / 100.0% | 60.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 80.0% / 100.0% |
| setup | 5 | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| status | 5 | 60.0% / 100.0% | 60.0% / 100.0% | 40.0% / 100.0% | 60.0% / 100.0% | 60.0% / 100.0% |
| teaching_override | 5 | 80.0% / 20.0% | 80.0% / 20.0% | 60.0% / 20.0% | 60.0% / 20.0% | 100.0% / 20.0% |

## Activation

| Description | Implicit recall | Precision | Explicit | Protected FP |
| --- | ---: | ---: | ---: | ---: |
| D0 | 100.0% | 100.0% | 100.0% | 0 |
| D1 | 100.0% | 100.0% | 100.0% | 0 |

## Blind pairwise preference

Each case judged in both orderings; a win requires winning both.

| Comparison | Wins | Ties | Cases |
| --- | --- | ---: | ---: |
| B-vs-A | A 10, B 26 | 23 | 59 |
| B-vs-G | B 9, G 23 | 23 | 55 |

Judge chose the left position in 35.2% of 233 decided judgments. Far from 50% would indicate position bias rather than a real preference.

## By category

Candidate against no policy, per case category.

| Category | n | Median reduction |
| --- | ---: | ---: |
| creative_override | 5 | +20.9% |
| destructive_risk | 5 | +47.3% |
| detailed_override | 5 | -30.5% |
| diagnosis | 5 | +61.5% |
| failed_validation | 5 | +62.9% |
| final | 5 | +34.8% |
| plan | 5 | +65.8% |
| review | 5 | +71.6% |
| security | 5 | +79.4% |
| setup | 5 | +67.7% |
| status | 5 | +42.2% |
| teaching_override | 5 | +22.8% |

## Limits

- One model (`claude-sonnet-5`), one CLI version, one run per case. No repeats.
- The CLI exposes no temperature or seed control, so runs are not reproducible
  bit-for-bit. Confidence intervals are clustered on case id.
- Activation is measured as a routing decision from the skill description,
  not as a live end-to-end dispatch inside an agent session.
- Results are scoped to this corpus, model and CLI. They are not a universal
  claim about token cost.
