# Benchmark policies

Policy texts used as benchmark arms. Nothing here is installed by the skill,
the plugin, or the installer — these are evaluation inputs only.

| Directory | What it is |
| --- | --- |
| `v0.2/` | Frozen copy of the policy shipped before v0.3.1. Arm `A`. |
| `v0.3/` | Candidates and controls. `B2` was promoted in v0.3.1; `B` failed its gates. |
| `external/` | Vendored third-party policies, hash-pinned. |

## `v0.2/` — arm A, the previous shipped policy

Byte-identical copies of the policy the repository shipped before the v0.3.1
promotion, so a benchmark run can compare against a fixed reference even after
the shipped files change. `tests/test_policies.py` guards that reference.

| File | Copy of |
| --- | --- |
| `simple_man_runtime.md` | `AGENTS.md.snippet` |
| `simple_man_skill.md` | `skills/simple-man/SKILL.md` |
| `description.txt` | the `description:` field of `skills/simple-man/SKILL.md` |

## `v0.3/` — candidates and controls

`B-runtime.md` and `B-skill.md` are the first candidate successors to the v0.2
pair. They **failed their gates and were never shipped**; they are kept as
evidence, not as a staging area.

`B2-runtime.md` and `B2-skill.md` are the second candidate, and this one **is
the policy the repository ships since v0.3.1** — `B2-runtime.md` is byte-identical
to `AGENTS.md.snippet`. The promotion was an explicit owner decision over the
automated gate result, recorded in
[`../releases/v0.3.1/DECISION.md`](../releases/v0.3.1/DECISION.md).

A candidate reaches the shipped surfaces only through that recorded route, and
`tests/test_policies.py` fails if one is promoted silently.

The changes below are the ones `B` introduced against v0.2; `B2` refines the
same line of work.

Changes from v0.2, each tracing to a specific defect in the shipped text:

1. **Scope limit.** v0.2 has no negative trigger anywhere, while
   `skills/simple-man/agents/openai.yaml` sets `allow_implicit_invocation: true`.
   The skill could therefore activate on a request for a tutorial or a detailed
   report and damage exactly the output the user asked for. The candidate states
   when not to apply, and that requested format and detail outweigh brevity.
2. **Priority between conflicting rules.** v0.2 says "Prefer one line" near the
   top and "expand until clear" at the bottom, with nothing saying which wins.
   The candidate merges them into one rule so the exception cannot be missed by
   a reader working top-to-bottom.
3. **"Adjacent" disambiguated.** v0.2 uses the word in three different senses —
   banned tips, protected factual findings, and adjacent correctness issues — so
   the ban could be read as suppressing a real finding. The candidate states
   plainly that the rule limits what you *offer*, not what you *find*.
4. **Runtime/skill parity.** Two rules existed only in `SKILL.md`: answer-first
   for explanations and plans, and the whole Language block. Users activating via
   `CLAUDE.md` got a narrower policy than users activating via the skill. Both are
   now in the runtime policy.
5. **"Same work quality" made checkable.** v0.2 asserts it without saying how it
   could be falsified. The candidate defines it as the list of activities that
   must not shrink, which is the same list the benchmark measures.
6. **Tone floor.** "Brevity is not curtness" — the project positions itself as a
   policy, not a persona, and nothing in v0.2 said so inside the policy itself.

`generic-terse.md` is the credibility control: a single sentence, reused verbatim
from `TERSE_INSTRUCTIONS` in `evals/run_codex.py`. It answers the question a
skeptic asks first — does a several-hundred-word policy beat one sentence of
"be concise"? If the candidate cannot beat this arm, that result gets published.

`D1-description.txt` is the candidate skill description, generated from the
frontmatter of `B-skill.md`. It is compared against `v0.2/description.txt` (D0)
on activation cases only.
