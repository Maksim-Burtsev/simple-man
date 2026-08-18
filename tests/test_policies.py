"""Guards for benchmark policy arms.

The v0.2 freeze is a historical record: benchmark arm A must keep meaning "the
policy v0.2 shipped", even though the shipped skill has since moved on. What
ships today must be byte-identical to the promoted candidate, and the promotion
must be recorded in a release DECISION.md — a candidate equal to the shipped
policy without that record means the release gate was bypassed.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "evals" / "policies"
V02 = POLICIES / "v0.2"
V03 = POLICIES / "v0.3"


def _description(skill_markdown: str) -> str:
    match = re.search(r"^description:\s*(.+)$", skill_markdown, re.M)
    assert match is not None, "skill file has no description field"
    return match.group(1).strip()


PROMOTION_RECORD = ROOT / "evals" / "releases" / "v0.3.1" / "DECISION.md"


class PolicyArmTests(unittest.TestCase):
    def test_shipped_policy_is_byte_identical_to_the_promoted_candidate(self):
        pairs = (
            (V03 / "B2-runtime.md", ROOT / "AGENTS.md.snippet"),
            (V03 / "B2-skill.md", ROOT / "skills" / "simple-man" / "SKILL.md"),
        )
        for promoted, shipped in pairs:
            with self.subTest(promoted=promoted.name):
                self.assertEqual(
                    promoted.read_bytes(),
                    shipped.read_bytes(),
                    f"{shipped} drifted from the promoted candidate {promoted}",
                )

    def test_promotion_is_recorded_not_silent(self):
        self.assertTrue(
            PROMOTION_RECORD.is_file(),
            "shipped policy equals a candidate but no DECISION.md records why",
        )
        text = PROMOTION_RECORD.read_text()
        self.assertIn("B2", text)
        self.assertIn("KEEP_SHIPPED_POLICY", text)

    def test_v02_freeze_is_a_stable_historical_record(self):
        """Benchmark arm A and both preregistrations depend on these bytes."""
        import hashlib

        expected = {
            "simple_man_runtime.md": "4deec7f8ae7e45a9b33ec01ddf2d1a5ca50d0e5b997b89aad37e5a8ab7148734",
            "simple_man_skill.md": "ac5bf862d48fafe2a5834984d1b2fcaeb1f9a8c88103cd803a9ea2e79d00305a",
            "description.txt": "0bf0e2fe2ced1fcef911d20c26fdc5418786938510a7095ac8101c0260473729",
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(
                    hashlib.sha256((V02 / name).read_bytes()).hexdigest(), digest
                )

    def test_shipped_description_is_d1(self):
        shipped = _description((ROOT / "skills" / "simple-man" / "SKILL.md").read_text())
        self.assertEqual(shipped, (V03 / "D1-description.txt").read_text().strip())

    def test_v03_candidates_exist(self):
        for name in (
            "B-runtime.md",
            "B-skill.md",
            "generic-terse.md",
            "D1-description.txt",
        ):
            with self.subTest(name=name):
                self.assertTrue((V03 / name).is_file())
                self.assertTrue((V03 / name).read_text().strip())

    def test_the_failed_first_candidate_is_never_shipped(self):
        """B failed its gates and was never promoted; it must stay a candidate."""
        self.assertNotEqual(
            (V03 / "B-runtime.md").read_bytes(),
            (ROOT / "AGENTS.md.snippet").read_bytes(),
        )
        self.assertNotEqual(
            (V03 / "B-skill.md").read_bytes(),
            (ROOT / "skills" / "simple-man" / "SKILL.md").read_bytes(),
        )

    def test_d1_description_matches_candidate_skill_frontmatter(self):
        candidate = _description((V03 / "B-skill.md").read_text())
        self.assertEqual(candidate, (V03 / "D1-description.txt").read_text().strip())

    def test_d1_adds_a_negative_trigger_that_d0_lacks(self):
        d0 = (V02 / "description.txt").read_text().lower()
        d1 = (V03 / "D1-description.txt").read_text().lower()
        self.assertNotIn("do not use", d0)
        self.assertIn("do not use", d1)

    def test_candidate_runtime_carries_the_rules_v02_kept_skill_only(self):
        runtime = (V03 / "B-runtime.md").read_text().lower()
        self.assertIn("match the user's language", runtime)
        self.assertIn("answer first", runtime)

    def test_generic_terse_control_is_a_single_short_instruction(self):
        text = (V03 / "generic-terse.md").read_text().strip()
        self.assertLessEqual(len(text.split()), 25, "control arm must stay one sentence")


if __name__ == "__main__":
    unittest.main()


class SecondCandidateTests(unittest.TestCase):
    """B2 answers specific defects found in the first live run.

    Each assertion below traces to a pattern in
    evals/releases/v0.3.0/analysis.md, so a future edit that quietly drops one
    of them fails here rather than in a paid run.
    """

    RUNTIME = V03 / "B2-runtime.md"
    SKILL = V03 / "B2-skill.md"

    def setUp(self):
        self.runtime = self.RUNTIME.read_text().lower()
        self.skill = self.SKILL.read_text().lower()

    def test_both_surfaces_exist(self):
        for path in (self.RUNTIME, self.SKILL):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())

    def test_findings_must_carry_a_fix(self):
        """B lost review and security cases for stating defects without remedies."""
        for text in (self.runtime, self.skill):
            self.assertIn("one-line fix", text)
        self.assertNotIn("no fix snippets", self.runtime)
        self.assertNotIn("no fix snippets", self.skill)

    def test_refusal_must_carry_the_safe_procedure(self):
        for text in (self.runtime, self.skill):
            self.assertIn("missing precondition", text)
            self.assertIn("safe procedure", text)

    def test_requested_shape_is_treated_as_a_contract(self):
        for text in (self.runtime, self.skill):
            self.assertIn("contract", text)
            self.assertIn("order given", text)

    def test_qualifiers_are_protected(self):
        for text in (self.runtime, self.skill):
            self.assertIn("no known remaining risks", text)

    def test_failed_validation_points_at_the_next_step(self):
        for text in (self.runtime, self.skill):
            self.assertIn("where to look next", text)

    def test_mode_is_decided_before_compressing(self):
        head = self.runtime.split("\n\n")[1]
        self.assertIn("mode first", head)

    def test_runtime_stays_within_a_sane_length_budget(self):
        """The runtime policy is paid on every turn under the always-on surface."""
        self.assertLessEqual(len(self.RUNTIME.read_text().split()), 360)

    def test_candidate_is_registered_as_a_benchmark_arm(self):
        sys.path.insert(0, str(ROOT / "evals" / "bench"))
        import runner as bench

        self.assertIn("B2", bench.ARMS)
        self.assertEqual(bench.ARMS["B2"], self.RUNTIME)
