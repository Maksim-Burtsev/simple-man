# Scout 1 — B2 against the one-sentence control

Cheap check before spending a full run, on exactly the seven categories where the
first candidate lost: review, security, teaching_override, creative_override,
destructive_risk, failed_validation, plan. Dev wave only, 35 cases.

## Result

B2 wins 16, control wins 7, 12 ties, across 35 cases judged in both orderings.

| Category | B2 | control | tie | first candidate was |
| --- | ---: | ---: | ---: | --- |
| security | 4 | 0 | 1 | 0–2 |
| plan | 3 | 0 | 2 | 0–2 |
| failed_validation | 3 | 2 | 0 | 1–3 |
| review | 2 | 1 | 2 | 1–3 |
| creative_override | 2 | 1 | 2 | 0–3 |
| teaching_override | 2 | 2 | 1 | 0–4 |
| destructive_risk | 0 | 1 | 4 | 0–2 |

Every category improved or held. The scout rule — B2 at least matches the control
— is met, so the full run proceeds.

`destructive_risk` is now mostly ties rather than losses: adding the safe
procedure closed the gap the first candidate lost on, without the answers
diverging much.

105 live calls, $2.03. All 70 judgments parsed, so the retry added in the
previous change is doing its job.

## Reused answers

Control answers were not re-run. They come from `../../v0.3.0/run/output.jsonl`,
same model, same corpus, same prelude, and only the candidate arm changed, so
paying for them again would buy nothing. Only the B2 answers produced here are
stored in `output.jsonl`; the judge records name the arm on each side, so any
pair can be reconstructed from the two directories.

There is no seed or temperature control in the CLI, so no two calls are
identical anyway — reuse is no less valid than a fresh call would have been.
