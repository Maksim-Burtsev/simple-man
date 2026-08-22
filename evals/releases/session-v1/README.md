# Session benchmark v1 — real Claude Code sessions on SkillsBench

**Question.** Does the shipped policy (B2), delivered always-on as a
system-prompt append, change what a real Claude Code session costs, and does
the agent still solve the task?

**Answer.** No measurable change to cost, tokens, turns or wall-clock. Task
reward leaned lower with the policy (8 better / 14 worse / 59 tie, sign
p = 0.29) — not significant, but the direction is not in the policy's favour
and is reported as such. All four preregistered gates pass;
decision `KEEP_SHIPPED_POLICY`.

| | |
|---|---|
| Protocol | SkillsBench at `aafac12f` (87 tasks, Harbor format), Claude Code `2.1.235` in Docker, `claude-sonnet-5`, effort `low`, k = 1; each task's own skills injected for every arm; Wilcoxon signed-rank on paired deltas, exact sign test on reward; delivery checked mechanically (B2 90/90, G 87/87, N 0/87) |
| Arms | `N` no policy · `B2` shipped policy · `G` one-sentence "be concise" control |
| Trials | 264 live sessions, $129.05 metered, 2026-08-21/22, claude.ai Max subscription via `claude setup-token` |
| Pairs | B2 vs N 81 · G vs N 79 · B2 vs G 79 |
| Dropped symmetrically | `bike-rebalance`, `earthquake-phase-association`, `multilingual-video-dubbing`, `python-scala-translation`, `radar-vital-signs`, `seismic-phase-picking` — image build or agent install failed on every arm |
| Retried once (one-sided infra failure) | B2: `exam-block-sequencing`, `glm-lake-mendota`, `paratransit-routing`; G: `energy-unit-commitment`, `latex-formula-extraction` |

Full tables: `run/report.md`; gates: `run/gates.md`; every number rebuilds
from `run/trials.jsonl` with `make session-check`. Pilot (3 tasks, not part
of the run): `pilot/`.

## Headline, B2 vs N (81 pairs)

| metric | median paired delta | 95 % CI | Wilcoxon p | arm totals |
|---|--:|--:|--:|--:|
| cost | +2.8 % | [−7.2, +10.7] | 0.71 | −2.9 % |
| total tokens | +2.1 % | [−6.4, +15.8] | 0.71 | −6.0 % |
| output tokens | −1.9 % | [−10.1, +9.0] | 0.77 | −3.8 % |
| turns | 0.0 % | [−5.9, +10.0] | 0.99 | −1.9 % |
| wall-clock | −0.9 % | [−9.8, +10.4] | 0.77 | −11.8 % |
| reward | 8↑ / 14↓ / 59 tie | — | sign 0.29 | mean 0.553 → 0.469; pass 41 → 35 |

Every CI straddles zero. The −32 % output-token figure on the front page is
what the policy does to a single answer with tools off; in a tool-using
session the visible answer is about one percent of the tokens, and the
policy does not touch the other ninety-nine. This run measures that directly
so the README no longer has to borrow JetBrains' caveman number to say it.

## The control arm is the uncomfortable part

`G` — the single sentence "be concise" — against `N`: reward 11↑ / 11↓ / 57,
cost median +0.4 % but totals −12.1 %, turns −11.7 % in totals. Against `B2`
it is cheaper on every resource metric, and `cost` clears p < 0.05:

| B2 vs G (79 pairs) | median paired delta | 95 % CI | Wilcoxon p | totals |
|---|--:|--:|--:|--:|
| cost | +12.4 % | [+0.7, +22.3] | **0.045** | +11.6 % |
| total tokens | +10.4 % | [+0.2, +32.3] | 0.062 | +12.7 % |
| turns | +10.5 % | [+0.0, +25.0] | 0.057 | +11.8 % |
| reward | 8↑ / 15↓ / 56 tie | — | sign 0.21 | mean 0.536 → 0.481 |

One sentence does in a session what two kilobytes of policy do not, and
costs less doing it. On this corpus the policy's session-level value over
"be concise" is negative on cost and indistinguishable on quality.

## Where the reward went

Of the 22 pairs that did not tie, B2 lost 14. In those 14 the treated session
made fewer turns (median 0.94× the control), spent less (0.93×) and wrote
less (0.93×); in the 8 it won it made the same number of turns and wrote
more (1.23×). In the 59 ties everything is 1.0×. The losses look like
stopping earlier, not working longer and failing: `hvac-control` 25 → 11
turns, `energy-unit-commitment` 36 → 26, `edit-pdf` 15 → 10, each from a full
to a zero reward. The control sentence's 11 losses show no such pattern
(turn ratio 1.0×).

This is an observation on 14 trajectories, not a finding. The hypothesis it
suggests — that "minimum words, no unrequested alternatives" leaks from the
answer into the agent's own deliberation and shortens the work, not just the
report — is the first thing a v0.4 candidate should test, with a
preregistered run, before changing a line of the policy.

## Drift

Arms ran interleaved in batches of 20 (N then B2 per batch, G afterwards),
so any day-to-day drift in the model lands on both arms of each pair:
2026-08-21 N 40 / B2 26; 2026-08-22 N 47 / B2 64 / G 87. G ran entirely on
the second day; its comparison with N is therefore cross-day for most
pairs, which is a reason to read the G-vs-N totals (−12 %) as an upper bound.

## What changes

The policy text: nothing in this release — no gate failed. The README:
session-level savings are stated as zero, the "be concise" comparison moves
from "tie" to "costs more in sessions", and the reward lean is quoted with
its p-value rather than rounded to "quality unchanged".
