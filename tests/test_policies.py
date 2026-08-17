"""Guards for benchmark policy arms.

Arm A must stay byte-identical to the shipped policy, and v0.3 candidates must
stay candidates until a release gate promotes them.
"""

import re
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


class PolicyArmTests(unittest.TestCase):
    def test_v02_arm_is_byte_identical_to_shipped_policy(self):
        pairs = (
            (V02 / "simple_man_runtime.md", ROOT / "AGENTS.md.snippet"),
            (V02 / "simple_man_skill.md", ROOT / "skills" / "simple-man" / "SKILL.md"),
        )
        for frozen, shipped in pairs:
            with self.subTest(frozen=frozen.name):
                self.assertEqual(
                    frozen.read_bytes(),
                    shipped.read_bytes(),
                    f"{frozen} drifted from {shipped}; re-freeze or revert",
                )

    def test_v02_description_matches_shipped_skill_frontmatter(self):
        shipped = _description((ROOT / "skills" / "simple-man" / "SKILL.md").read_text())
        self.assertEqual(shipped, (V02 / "description.txt").read_text().strip())

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

    def test_v03_candidates_are_not_silently_promoted(self):
        """A candidate that already equals the shipped policy would mean the
        release gate was bypassed, so the benchmark would compare A against A."""
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
