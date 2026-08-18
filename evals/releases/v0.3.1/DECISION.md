# Release decision — promote B2 and D1 to the shipped skill

Date: 2026-08-18. Decided by the project owner, recorded before the change was
made.

## What the automated decision said

The preregistered gate table for this run returned **KEEP_SHIPPED_POLICY**:
three gates failed, and the decision rule said the candidate ships only if every
gate holds. Nothing in that table is re-scored here; `gates.md` stands as
written.

## Why the owner overrides it

The two failed comparison gates measure whether the candidate beats a
one-sentence generic control on brevity and blind preference. After two full
runs the owner redefined the product's goal: **cut agent chattiness without
material quality loss** — quality first, compression second. Real session-level
savings from terseness skills are single-digit percent regardless of policy
(JetBrains measured caveman at −8.5% output tokens across 86 real tasks against
its advertised 65%), so competing on compression percentages was the wrong
target. Under the actual goal, the vs-control gates answer an orthogonal
question.

Against the goal as now defined, the measured record is one-sided:

| Question | Shipped v0.2 | Candidate B2 |
| --- | --- | --- |
| Blind preference (84 cases, both orderings) | 8 wins | 48 wins, 28 ties |
| Required facts kept | 57.1% | 66.7% — level with no policy at all |
| False success claims | — | 0 |
| Routing precision (description) | 90%, 2 protected misses | 100%, 0 |
| Output vs no policy | −66.3% | −32.4% |

The shipped policy achieves its extra compression by dropping facts; the
candidate restores fact retention to the no-policy level while still removing a
third of the output. The third failed gate (`winner_coding_failures`) was
specified wrong — all arms including no-policy fail the same fixture — and is
documented in `gates.md`.

## What this is not

This is not a silent bypass of the preregistration. The measurement stands, the
gate failures are published, and this document dates and explains the
deviation. The preregistration mechanism binds the *measurement*; the product
decision belongs to the owner, made here with the trade-offs in the open.

## What ships

- `AGENTS.md.snippet` and derived surfaces := `evals/policies/v0.3/B2-runtime.md`
- `skills/simple-man/SKILL.md` and plugin copy := `evals/policies/v0.3/B2-skill.md`
  (its frontmatter is the D1 description with the negative trigger)

The frozen `evals/policies/v0.2/` record is unchanged, so both preregistrations
keep verifying and benchmark arm A keeps meaning "the v0.2 policy".
