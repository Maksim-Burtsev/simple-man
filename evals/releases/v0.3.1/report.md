# Simple Man benchmark

Model: `claude-sonnet-5`  Judge: `claude-haiku-4-5`  CLI: `2.1.233 (Claude Code)`
Output cases: 84  Activation cases: 40
Live calls: 836  Reported cost: $14.72

Every number below is recomputed from the raw records by
`evals/bench/report.py`; `make bench-v3-check` fails if it cannot be rebuilt.

## Output length

| Comparison | n | Median reduction | 95% CI | Median tokens |
| --- | ---: | ---: | --- | ---: |
| B2 vs N | 84 | +32.4% | [+23.2%, +43.8%] | 520 vs 833 |
| B2 vs A | 84 | -68.0% | [-110.1%, -52.0%] | 520 vs 234 |
| B2 vs G | 84 | -0.3% | [-8.1%, +8.7%] | 520 vs 529 |
| A vs N | 84 | +66.3% | [+53.5%, +71.8%] | 234 vs 833 |
| C vs N | 84 | +40.3% | [+27.7%, +51.1%] | 412 vs 833 |
| G vs N | 84 | +33.4% | [+23.9%, +37.4%] | 529 vs 833 |

## Retention

Split because the two failures mean different things. *Facts* is the share
of cases keeping every required fact and making no forbidden claim. *Format*
is the share obeying an explicitly requested shape, such as exactly four
numbered steps. *Both* is the strict combination, and is the gated metric.

| Arm | Facts | Format | Both |
| --- | ---: | ---: | ---: |
| A | 57.1% | 82.1% | 46.4% |
| B2 | 66.7% | 81.0% | 52.4% |
| C | 59.5% | 82.1% | 47.6% |
| G | 67.9% | 82.1% | 56.0% |
| N | 66.7% | 76.2% | 50.0% |

## Activation

| Description | Implicit recall | Precision | Explicit | Protected FP |
| --- | ---: | ---: | ---: | ---: |
| D0 | 100.0% | 90.0% | 66.7% | 2 |
| D1 | 100.0% | 100.0% | 100.0% | 0 |

## Blind pairwise preference

Each case judged in both orderings; a win requires winning both.

| Comparison | Wins | Ties | Cases |
| --- | --- | ---: | ---: |
| B2-vs-A | A 8, B2 48 | 28 | 84 |
| B2-vs-G | B2 19, G 20 | 44 | 83 |

Judge chose the left position in 35.4% of 333 decided judgments. Far from 50% would indicate position bias rather than a real preference.

## Coding tasks

Three fixtures with failing test suites. The arm edits production files,
then the patch is replayed against a pristine copy and against hidden
cases the model never saw. This is the only measurement here that does
not depend on anyone's opinion.

| Arm | Passed | Failed fixtures |
| --- | ---: | --- |
| A | 2/3 | python-payment-ledger |
| B2 | 2/3 | python-payment-ledger |
| G | 2/3 | python-payment-ledger |
| N | 2/3 | python-payment-ledger |

## Dev and holdout separately

The holdout wave was written after the first run, by authors who saw
neither its results nor any candidate policy. If a policy were tuned to
the dev corpus, the two slices would disagree.

| Wave | Cases | Comparison | Median reduction | Facts kept |
| --- | ---: | --- | ---: | ---: |
| dev | 60 | B2 vs N | +35.3% | 65.0% |
| dev | 60 | B2 vs A | -62.1% | 65.0% |
| dev | 60 | B2 vs G | -0.3% | 65.0% |
| dev | 60 | A vs N | +59.2% | 56.7% |
| dev | 60 | C vs N | +40.6% | 58.3% |
| dev | 60 | G vs N | +34.2% | 73.3% |
| holdout-v2 | 24 | B2 vs N | +27.4% | 70.8% |
| holdout-v2 | 24 | B2 vs A | -102.1% | 70.8% |
| holdout-v2 | 24 | B2 vs G | +0.3% | 70.8% |
| holdout-v2 | 24 | A vs N | +69.4% | 58.3% |
| holdout-v2 | 24 | C vs N | +33.1% | 62.5% |
| holdout-v2 | 24 | G vs N | +30.1% | 54.2% |

## By category

Candidate against no policy, per case category.

| Category | n | Median reduction |
| --- | ---: | ---: |
| creative_override | 7 | +4.5% |
| destructive_risk | 7 | +43.4% |
| detailed_override | 7 | +21.5% |
| diagnosis | 7 | +33.5% |
| failed_validation | 7 | +37.0% |
| final | 7 | +32.2% |
| plan | 7 | +17.5% |
| review | 7 | +50.9% |
| security | 7 | +65.4% |
| setup | 7 | +50.5% |
| status | 7 | +15.2% |
| teaching_override | 7 | -28.6% |

## Limits

- One model (`claude-sonnet-5`), one CLI version, one run per case. No repeats.
- The CLI exposes no temperature or seed control, so runs are not reproducible
  bit-for-bit. Confidence intervals are clustered on case id.
- Activation is measured as a routing decision from the skill description,
  not as a live end-to-end dispatch inside an agent session.
- Results are scoped to this corpus, model and CLI. They are not a universal
  claim about token cost.
