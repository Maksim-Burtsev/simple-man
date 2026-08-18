| Gate | Required | Measured | Result |
| --- | --- | --- | --- |
| explicit_activation | eq 1.0 | 1.000 | pass |
| implicit_recall | gte 0.9 | 1.000 | pass |
| precision | gte 0.95 | 1.000 | pass |
| protected_near_miss_false_positives | eq 0 | 0 | pass |
| false_validation_or_lost_blocker | eq 0 | 0 | pass |
| retention_equal_or_better_than_A | eq True | True | pass |
| winner_coding_failures | eq 0 | 1 | FAIL |
| blind_wins_minus_losses_vs_A | gte 0 | 40 | pass |
| blind_wins_minus_losses_vs_G | gte 0 | -1 | FAIL |
| median_output_reduction_vs_N | gte 0.15 | 0.324 | pass |
| bootstrap_95_lower_bound_vs_N | gt 0.05 | 0.232 | pass |
| median_output_reduction_vs_G | gte 0.05 | -0.003 | FAIL |

Decision: **KEEP_SHIPPED_POLICY**

Failed: winner_coding_failures, blind_wins_minus_losses_vs_G, median_output_reduction_vs_G

## What the failures mean

### `winner_coding_failures` — a gate I specified wrong

All four arms, including the one with no policy at all, fail
`python-payment-ledger` and pass the other two fixtures. The fixture is beyond
this model regardless of communication policy, so this number measures fixture
difficulty, not the candidate.

The gate should have been relative — candidate failures no worse than the
control's — rather than an absolute zero. It was registered as an absolute, and
a preregistration is worth nothing if it is edited once the results are in, so
it is scored as failed and the mistake is recorded here instead.

### `blind_wins_minus_losses_vs_G` and `median_output_reduction_vs_G` — a real dead heat

Against the one-sentence control the candidate finishes level: 19 wins to 20
across 83 judged cases with 44 ties, and 0.3% longer at the median.

This is the honest headline of the whole exercise. The first candidate was 27.9%
shorter than that control and lost badly on quality. The second one spends
exactly that headroom on remediation, safe procedures and next steps, and buys a
tie. For this model and this corpus, a 337-word policy and one sentence of "be
concise" land in the same place.

## What the candidate does win

Against the policy actually shipped today it is not close:

- blind preference 48 wins to 8, with 28 ties, across 84 cases
- fact retention 66.7% against 57.1%
- every activation metric perfect, while the shipped description scores 90%
  precision and lets two protected-category requests through

The gate table asks whether the candidate beats a generic control, and it does
not. It does not ask whether the candidate beats what is shipped, which it does
by a wide margin. That is a limitation of the gates as written, not a finding
about the candidate, and changing them now would be tuning the bar to the
result. The next preregistration should ask both questions separately.

## Holdout agrees with dev

The holdout wave, written by authors who saw neither the first run's results nor
any candidate, tracks the dev wave closely: candidate against no policy +35.3%
on dev and +27.4% on holdout, candidate against the control -0.3% on dev and
+0.3% on holdout. There is no sign of a policy tuned to the corpus it was
developed against.
