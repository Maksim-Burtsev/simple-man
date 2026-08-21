# Session benchmark gates

| gate | metric | rule | value | result |
|---|---|---|--:|---|
| delivery_treated | `delivery.B2.fraction` | eq 1.0 | 1.0000 | PASS |
| delivery_control | `delivery.N.fraction` | eq 0.0 | 0.0000 | PASS |
| enough_pairs | `quality.N:B2.n` | gte 60 | 3 | FAIL |
| reward_not_worse | `quality.N:B2.worse_significant` | eq False | False | PASS |

Decision: **REVIEW_POLICY** — failed: enough_pairs

`KEEP_SHIPPED_POLICY` means the shipped policy survives real sessions; `REVIEW_POLICY` means a gate failed and the policy needs a candidate revision before the next release.
