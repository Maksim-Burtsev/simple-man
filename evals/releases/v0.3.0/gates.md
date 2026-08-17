| Gate | Required | Measured | Result |
| --- | --- | --- | --- |
| explicit_activation | eq 1.0 | 1.000 | pass |
| implicit_recall | gte 0.9 | 1.000 | pass |
| precision | gte 0.95 | 1.000 | pass |
| protected_near_miss_false_positives | eq 0 | 0 | pass |
| false_validation_or_lost_blocker | eq 0 | 0 | pass |
| retention_equal_or_better_than_A | eq True | True | pass |
| blind_wins_minus_losses_vs_A | gte 0 | 16 | pass |
| blind_wins_minus_losses_vs_G | gte 0 | -14 | FAIL |
| median_output_reduction_vs_N | gte 0.15 | 0.524 | pass |
| bootstrap_95_lower_bound_vs_N | gt 0.05 | 0.417 | pass |
| median_output_reduction_vs_G | gte 0.05 | 0.279 | pass |

Decision: **KEEP_SHIPPED_POLICY**

Failed: blind_wins_minus_losses_vs_G

## What the failure means

`blind_wins_minus_losses_vs_G` compares the candidate against a one-sentence
control: *"Be concise. Remove filler, generic reassurance, repeated context, and
closing offers."* The judge preferred that control on 23 cases and the candidate
on 9.

By category, the control wins where the user asked for length and the candidate
compressed anyway — teaching 4-0, creative 3-0, review 3-1, failed validation
3-1. The candidate wins or ties on status, setup, diagnosis and final.

The candidate's new "do not apply to tutorials and long-form writing" clause did
not prevent this. That clause works when it gates *routing*: activation
precision is 100% with zero protected-category false positives. It does not work
once the policy is already injected as an always-on system prompt, where one
sentence saying "do not apply" competes with three hundred words saying
"compress".

So the failure is specific and actionable, not a verdict on the whole idea: the
candidate beats the shipped policy on every axis measured here, and beats the
one-line control on length by 27.9% while keeping facts at a comparable rate. It
loses on preference because it over-compresses the categories that asked for
detail.
