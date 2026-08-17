# Benchmark policies

Policy texts used as benchmark arms. Nothing here is installed by the skill,
the plugin, or the installer — these are evaluation inputs only.

| Directory | What it is |
| --- | --- |
| `v0.2/` | Frozen copy of the currently shipped policy. Arm `A`. |
| `v0.3/` | Candidate policy under evaluation. Arm `B`, plus controls. Not shipped. |
| `external/` | Vendored third-party policies, hash-pinned. |

## `v0.2/` — arm A, the shipped baseline

Byte-identical copies of what the repository ships today, so a benchmark run can
compare against a fixed reference even after the shipped files change.
`tests/test_policies.py` fails if they drift apart.

| File | Copy of |
| --- | --- |
| `simple_man_runtime.md` | `AGENTS.md.snippet` |
| `simple_man_skill.md` | `skills/simple-man/SKILL.md` |
| `description.txt` | the `description:` field of `skills/simple-man/SKILL.md` |

## `v0.3/` — candidates, not shipped

`B-runtime.md` and `B-skill.md` are the candidate successors to the v0.2 pair.
They are promoted into the shipped surfaces **only** if they clear the release
gates on a live run. Until then the shipped policy is unchanged, and
`tests/test_policies.py` fails if a candidate is silently promoted.

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
