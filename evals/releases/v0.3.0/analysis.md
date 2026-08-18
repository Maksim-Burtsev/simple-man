# Why the v0.3 candidate lost, from the run's own records

The candidate cleared 10 of 11 preregistered gates and failed
`blind_wins_minus_losses_vs_G`: a one-sentence control, *"Be concise. Remove
filler, generic reassurance, repeated context, and closing offers."*, beat it
23 wins to 9.

This document is the evidence behind the next candidate's design. It was
written and committed before any replacement policy text existed, so the design
follows the data rather than the other way round.

Everything below comes from `run/judge.jsonl` and `run/output.jsonl` in this
directory and can be recomputed from them.

## The first read was wrong

The obvious reading of the category table was "the candidate over-compresses the
categories that asked for length". That is part of it, but it does not survive
contact with the judge flags:

| Flag | on candidate | on control |
| --- | ---: | ---: |
| `missing_material_fact` | 18 | 3 |
| `unnecessary_content` | 6 | 18 |
| `constraint_violation` | 6 | 14 |
| `detail_override_loss` | 5 | 0 |

The candidate is not primarily losing on verbosity taste. It is losing on
**missing content**, six times more often than the control. The control's own
failures are the mirror image — padding and ignored constraints — and it still
wins, because the judge treats a missing decision-relevant fact as the more
expensive error.

## Pattern 1 — the answer stops before the reader can act

The largest single cause. The policy rule `no fix snippets unless required to
act` turns out to be wrong about how often a fix is required.

- `out-dev-21` (review): *"Right includes remediation guidance (server-side price
  validation, reordered checks, enum constraint) that engineers need to actually
  fix the issues, making every extra word decision-relevant; left omits…"*
- `out-dev-25` (security): *"Left provides decision-relevant details (CWE/OWASP
  classifications, concrete fix specs like '≥12 chars' and zxcvbn, single-use/
  expiration validation notes) that an engineer needs to implement correctly."*
- `out-dev-43` (failed validation): *"LEFT provides explicit actionable guidance
  (mutex wrapping, deadlock risk, retest steps) that an engineer needs to fix the
  race condition, while RIGHT states facts only."*
- `out-dev-41` (failed validation): the winner *"adds specific investigation
  hypotheses (ROUND_HALF_UP vs ROUND_HALF_EVEN timing), tells the next engineer
  where to look."*

Refusals fail the same way. Both arms correctly refuse the destructive action;
the one that also supplies the safe path wins:

- `out-dev-45`: *"Both correctly refuse the dangerous action, but Right includes
  an explicit backup-sync step with the ready-to-use command, whereas Left only
  mentions backup as a blocker, not as part of the safe procedure."*
- `out-dev-47`: *"Right refuses equally firmly but adds actionable specificity —
  5 ordered steps, reversible alternatives (rename-first), testing guidance."*

A refusal that names the blocker and stops is a worse answer than a refusal that
names the blocker and hands over the procedure. Naming the blocker was the only
thing the policy required.

## Pattern 2 — explicit form is treated as advisory

Where the prompt states a contract about shape, the candidate broke it and the
control did not.

- `out-dev-24` (review): the prompt says *"Label them P1, P2, P3 in order given"*
  and lists `deliver.js:64`, `deliver.js:91`, `queue.js:22`. The candidate
  re-sorted by its own severity judgement, emitting P1=`:91`, P2=`queue.js:22`,
  P3=`:64`. The judge: *"Left assigns them as P1=91, P2=22, P3=64 (wrong order);
  right correctly assigns P1=64, P2=91."* Every fact survived; the requested
  ordering did not.
- `out-dev-12` (creative): a 120–160 word limit. The candidate produced 162
  words, the control 145. *"Left exceeds the 120-160 word limit (162 words),
  violating the specification."*
- `out-dev-55` (teaching): *"the task explicitly requires 'exactly five
  paragraphs'"* — obeyed by the winner, not the candidate.

Corpus-wide, format failures are dominated by `exact_top_level_items` (42) and
`exact_paragraphs` (19), which are precisely the constraints a policy optimising
for shortness will round off.

## Pattern 3 — qualifiers are compressed away, changing the claim

- `out-dev-02` (final): *"Left preserves the critical qualifier 'известных'
  (known) when stating no risks exist, which is an important engineering
  distinction that changes the claim's meaning."*

"No known remaining risks" and "no remaining risks" differ in what they promise.
The policy protects "material facts" but says nothing about hedges that carry
meaning, so a compressor drops them as filler.

## The headroom that makes a fix affordable

The candidate is **27.9% shorter than the control** (median 296 vs 523 output
tokens) while losing on preference. The next candidate can spend words on
remediation, safe procedures and investigation hypotheses and still be
materially shorter than one sentence of "be concise". Length was never the
binding constraint; the content rule was.

## What the routing evidence says

Activation is not the problem. Both descriptions score 100% implicit recall,
100% precision, 100% explicit accuracy and zero protected-category false
positives. The candidate's new *"do not apply to tutorials and long-form
writing"* clause works when it gates whether the skill is invoked at all.

It does not work once the policy is already injected as an always-on system
prompt: one sentence saying "do not apply" competes with three hundred words
saying "compress", and loses. A policy that ships on an always-on surface has to
carry the exception in its structure, not in an aside.

## Design consequences for the next candidate

1. Every finding carries its own one-line fix. Delete `no fix snippets unless
   required to act`; the data says the fix is what makes the finding usable.
2. A refusal must include the safe procedure, not just the blocker.
3. Failed validation reports where to look next, not only what failed.
4. Explicitly requested count, order, structure and word limits are a contract,
   checked before sending, never re-sorted by the agent's own judgement.
5. Qualifiers that change what a claim promises are material facts.
6. Decide the mode first — artifact with a required shape, teaching, or ordinary
   status — then apply compression inside that mode, rather than compressing by
   default and carrying one exception sentence.
