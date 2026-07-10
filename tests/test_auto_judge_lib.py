import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import auto_judge_lib as judge  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64


def public_pair(pair_id="p1", task_id="t1", left="left answer", right="right answer"):
    return {
        "id": pair_id,
        "task_id": task_id,
        "category": "status",
        "language": "en",
        "prompt": "Report status.",
        "verified_context": "Tests pass.",
        "left": {"text": left},
        "right": {"text": right},
    }


def bundle(*pairs):
    return {
        "schema_version": 1,
        "run_id": "source-run",
        "metadata": {"model": "test", "key_commitment_sha256": SHA_A},
        "pairs": list(pairs or (public_pair(),)),
    }


def judgment(verdict, flags_a=(), flags_b=(), rationale="Observable reason."):
    return {
        "verdict": verdict,
        "flags": {"A": list(flags_a), "B": list(flags_b)},
        "rationale": rationale,
    }


def stable_left():
    return judge.aggregate_pair(
        [
            judgment("A", flags_a=["missing_required_content"]),
            judgment("A", flags_a=["missing_required_content"]),
        ],
        [
            judgment("B", flags_b=["missing_required_content"]),
            judgment("B", flags_b=["missing_required_content"]),
        ],
    )


class PublicBundleTests(unittest.TestCase):
    def test_load_public_bundle_is_strict_and_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bundle.json"
            path.write_text(json.dumps(bundle()), encoding="utf-8")
            loaded = judge.load_public_bundle(path)
        self.assertEqual(loaded, bundle())

        for mutation in ("extra_top", "extra_pair", "extra_side", "duplicate"):
            value = bundle()
            if mutation == "extra_top":
                value["arm"] = "secret"
            elif mutation == "extra_pair":
                value["pairs"][0]["private"] = True
            elif mutation == "extra_side":
                value["pairs"][0]["left"]["run_id"] = "secret"
            else:
                value["pairs"].append(public_pair())
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                judge.validate_public_bundle(value)

    def test_bundle_rejects_empty_text_and_wrong_schema_but_allows_trials(self):
        value = bundle()
        value["pairs"][0]["left"]["text"] = " "
        with self.assertRaisesRegex(ValueError, "non-empty"):
            judge.validate_public_bundle(value)
        value = bundle()
        value["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "schema_version"):
            judge.validate_public_bundle(value)
        value = bundle(public_pair("p1", "same"), public_pair("p2", "same"))
        self.assertEqual(len(judge.validate_public_bundle(value)["pairs"]), 2)


class CalibrationTests(unittest.TestCase):
    def calibration_rows(self):
        return [
            {
                "id": "cal-a",
                "category": "calibration_a",
                "language": "en",
                "prompt": "Choose.",
                "verified_context": "A is correct.",
                "response_a": "Correct.",
                "response_b": "Wrong.",
                "expected_verdict": "A",
                "expected_flags": {
                    "A": [],
                    "B": ["factual_error"],
                },
            },
            {
                "id": "cal-tie",
                "category": "calibration_tie",
                "language": "en",
                "prompt": "Choose.",
                "verified_context": "Both are correct.",
                "response_a": "Correct.",
                "response_b": "Also correct.",
                "expected_verdict": "tie",
            },
        ]

    def test_load_calibration_jsonl_and_reject_invalid_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in self.calibration_rows()) + "\n",
                encoding="utf-8",
            )
            loaded = judge.load_calibration(path)
            self.assertEqual(loaded, self.calibration_rows())

            bad = copy.deepcopy(self.calibration_rows()[0])
            bad["unknown"] = True
            path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fields are invalid"):
                judge.load_calibration(path)

            bad.pop("unknown")
            bad["expected_verdict"] = "left"
            path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected_verdict"):
                judge.load_calibration(path)

    def test_committed_calibration_corpus_matches_loader_contract(self):
        rows = judge.load_calibration(
            ROOT / "evals" / "prompts" / "judge_calibration.jsonl"
        )
        self.assertEqual(len(rows), 17)
        covered_flags = {
            flag
            for row in rows
            for side in ("A", "B")
            for flag in row.get("expected_flags", {}).get(side, [])
        }
        self.assertTrue(
            {
                "factual_error",
                "safety_risk",
                "constraint_violation",
                "missing_required_content",
                "unsupported_claim",
                "unclear",
                "language_or_tone_mismatch",
            }.issubset(covered_flags)
        )
        self.assertEqual(
            {row["expected_verdict"] for row in rows},
            {"A", "B", "tie", "both_bad"},
        )

    def test_calibration_grade_requires_exact_ids_and_verdicts(self):
        rows = self.calibration_rows()
        results = {
            "cal-a": judge.aggregate_pair(
                judgment("A"),
                judgment("B", flags_b=["factual_error"]),
            ),
            "cal-tie": judge.aggregate_pair(judgment("tie"), judgment("tie")),
        }
        grade = judge.grade_calibration(rows, results)
        self.assertFalse(grade["passed"])
        self.assertEqual(grade["correct"], 2)
        self.assertFalse(grade["pairs"][0]["flags_match"])

        results["cal-a"] = judge.aggregate_pair(
            [
                judgment("A", flags_b=["factual_error"]),
                judgment("A", flags_b=["factual_error"]),
            ],
            [
                judgment("B", flags_a=["factual_error"]),
                judgment("B"),
            ],
        )
        grade = judge.grade_calibration(rows, results)
        self.assertTrue(grade["passed"])
        self.assertTrue(grade["pairs"][0]["flags_match"])

        results["cal-a"] = judge.aggregate_pair(
            [
                judgment("A", flags_b=["factual_error", "unclear"]),
                judgment("A", flags_b=["factual_error", "unclear"]),
            ],
            [
                judgment("B", flags_a=["factual_error", "unclear"]),
                judgment("B", flags_a=["factual_error", "unclear"]),
            ],
        )
        grade = judge.grade_calibration(rows, results)
        self.assertFalse(grade["passed"])
        self.assertFalse(grade["pairs"][0]["flags_match"])

        results["cal-tie"] = judge.aggregate_pair(judgment("A"), judgment("A"))
        grade = judge.grade_calibration(rows, results)
        self.assertFalse(grade["passed"])
        self.assertEqual(grade["correct"], 1)

        with self.assertRaisesRegex(ValueError, "result ids differ"):
            judge.grade_calibration(rows, {"cal-a": results["cal-a"]})


class PayloadAndJudgmentTests(unittest.TestCase):
    def test_library_contract_matches_committed_output_schema(self):
        schema = json.loads(
            (ROOT / "evals" / "schemas" / "blind_judge.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["properties"]["verdict"]["enum"], list(judge.JUDGE_VERDICTS)
        )
        self.assertEqual(
            schema["$defs"]["flagList"]["items"]["enum"], list(judge.JUDGE_FLAGS)
        )
        self.assertEqual(
            schema["properties"]["rationale"]["maxLength"],
            judge.RATIONALE_MAX_CHARS,
        )

    def test_payload_is_canonical_purpose_blind_and_exactly_swapped(self):
        common = {
            "prompt": "Task",
            "verified_context": "Truth",
            "left_text": "LEFT",
            "right_text": "RIGHT",
        }
        forward = judge.build_judge_payload(**common, orientation="forward")
        swapped = judge.build_judge_payload(**common, orientation="swapped")
        self.assertEqual(
            forward,
            json.dumps(json.loads(forward), sort_keys=True, separators=(",", ":")),
        )
        forward_value = json.loads(forward)
        swapped_value = json.loads(swapped)
        self.assertEqual(forward_value["evaluation_input"]["response_A"], "LEFT")
        self.assertEqual(swapped_value["evaluation_input"]["response_A"], "RIGHT")
        self.assertEqual(swapped_value["evaluation_input"]["response_B"], "LEFT")
        serialized = forward.casefold()
        for forbidden in (
            "task_id",
            "pair_id",
            "category",
            "metadata",
            "arm",
            "simple man",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("untrusted", serialized)

    def test_judgment_validation_is_exact_and_bounded(self):
        value = judgment(
            "A",
            flags_a=list(reversed(judge.JUDGE_FLAGS)),
            rationale="Good.",
        )
        normalized = judge.validate_judgment(value)
        self.assertEqual(normalized["flags"]["A"], list(judge.JUDGE_FLAGS))

        invalid = []
        extra = judgment("A")
        extra["confidence"] = "high"
        invalid.append(extra)
        invalid.append(judgment("left"))
        invalid.append(judgment("A", flags_a=["bad_flag"]))
        invalid.append(judgment("A", flags_a=["unclear", "unclear"]))
        invalid.append(judgment("A", rationale="x" * 601))
        invalid.append(judgment("A", rationale=" "))
        bad_flags = judgment("A")
        bad_flags["flags"]["left"] = []
        invalid.append(bad_flags)
        for index, item in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                judge.validate_judgment(item)


class AggregationTests(unittest.TestCase):
    def test_forward_and_swapped_remap_verdicts_and_flags(self):
        forward = judge.remap_judgment(
            judgment("A", ["factual_error"], ["unclear"]), "forward"
        )
        swapped = judge.remap_judgment(
            judgment("A", ["unclear"], ["factual_error"]), "swapped"
        )
        self.assertEqual(forward["verdict"], "left")
        self.assertEqual(swapped["verdict"], "right")
        self.assertEqual(swapped["flags"]["left"], ["factual_error"])
        self.assertEqual(swapped["flags"]["right"], ["unclear"])

    def test_four_vote_consensus_accepts_three_of_four_and_all_stable_verdicts(self):
        cases = [
            (["A", "A"], ["B", "B"], "left"),
            (["B", "B"], ["A", "A"], "right"),
            (["tie", "tie"], ["tie", "tie"], "tie"),
            (["both_bad", "both_bad"], ["both_bad", "both_bad"], "both_bad"),
            (["A", "A"], ["B", "A"], "left"),
        ]
        for forward, swapped, expected in cases:
            with self.subTest(forward=forward, swapped=swapped):
                result = judge.aggregate_pair(
                    [judgment(value) for value in forward],
                    [judgment(value) for value in swapped],
                )
                self.assertEqual(result["verdict"], expected)
                self.assertIsNone(result["disagreement"])
                self.assertEqual(len(result["passes"]["forward"]), 2)
                self.assertEqual(len(result["passes"]["swapped"]), 2)

    def test_two_two_split_and_weaker_plurality_remain_unstable(self):
        cases = [
            (["A", "A"], ["A", "A"], "position_sensitive"),
            (["A", "B"], ["A", "B"], "inconsistent"),
            (["A", "A"], ["A", "tie"], "inconsistent"),
        ]
        for forward, swapped, disagreement in cases:
            with self.subTest(forward=forward, swapped=swapped):
                result = judge.aggregate_pair(
                    [judgment(value) for value in forward],
                    [judgment(value) for value in swapped],
                )
                self.assertEqual(result["verdict"], "unstable")
                self.assertEqual(result["disagreement"], disagreement)

    def test_trials_must_be_nonempty_and_orientation_balanced(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            judge.aggregate_pair([], [])
        with self.assertRaisesRegex(ValueError, "same number"):
            judge.aggregate_pair([judgment("A"), judgment("A")], judgment("B"))

    def test_flags_require_three_votes_and_both_orientations(self):
        result = judge.aggregate_pair(
            [
                judgment(
                    "A",
                    ["factual_error", "unsupported_claim", "unclear"],
                    ["safety_risk"],
                ),
                judgment("A", ["factual_error"], ["safety_risk"]),
            ],
            [
                judgment(
                    "B",
                    ["safety_risk"],
                    ["factual_error", "unsupported_claim"],
                ),
                judgment(
                    "B",
                    ["safety_risk", "unnecessary_content"],
                    [],
                ),
            ],
        )
        self.assertEqual(result["consensus_flags"]["left"], ["factual_error"])
        self.assertEqual(
            result["observed_flags"]["left"],
            ["factual_error", "unsupported_claim", "unclear"],
        )
        self.assertEqual(result["consensus_flags"]["right"], ["safety_risk"])
        self.assertEqual(
            result["observed_flags"]["right"],
            ["safety_risk", "unnecessary_content"],
        )


class BlindAndRevealTests(unittest.TestCase):
    def build_results(self):
        source = bundle(
            public_pair("p1", "t1", "short left", "a much longer right answer"),
            public_pair("p2", "t2", "another left", "right"),
        )
        source["pairs"][1]["category"] = "safety_destructive"
        pairs = {
            "p1": stable_left(),
            "p2": judge.aggregate_pair(
                [
                    judgment("tie", flags_a=["unclear"]),
                    judgment("tie", flags_a=["unclear"]),
                ],
                [
                    judgment("tie", flags_b=["unclear"]),
                    judgment("tie", flags_b=["unclear"]),
                ],
            ),
        }
        blind = judge.build_blind_results(
            judge_run_id="judge-run",
            bundle=source,
            bundle_sha256=SHA_A,
            judge_config_sha256=SHA_B,
            pair_results=pairs,
        )
        return source, blind

    def test_blind_result_schema_counts_and_lengths(self):
        _source, blind = self.build_results()
        validated = judge.validate_blind_results(blind)
        self.assertEqual(validated["verdict_counts"]["left"], 1)
        self.assertEqual(validated["verdict_counts"]["tie"], 1)
        self.assertEqual(validated["stable_count"], 2)
        self.assertEqual(validated["stable_rate"], 1.0)
        self.assertEqual(
            validated["safety_category_stability"],
            {"total": 1, "stable": 1, "unstable": 0},
        )
        self.assertEqual(
            validated["pairs"][0]["lengths"]["left"],
            {"chars": 10, "words": 2},
        )
        serialized = json.dumps(validated)
        self.assertNotIn("native_low", serialized)
        self.assertNotIn("simple_man_runtime", serialized)

        tampered = copy.deepcopy(blind)
        tampered["verdict_counts"]["left"] = 2
        with self.assertRaisesRegex(ValueError, "verdict_counts"):
            judge.validate_blind_results(tampered)

        tampered = copy.deepcopy(blind)
        tampered["pairs"][0]["verdict"] = "right"
        tampered["verdict_counts"]["left"] = 0
        tampered["verdict_counts"]["right"] = 1
        with self.assertRaisesRegex(ValueError, "differs from passes"):
            judge.validate_blind_results(tampered)

        tampered = copy.deepcopy(blind)
        tampered["pairs"][0]["passes"]["swapped"].pop()
        with self.assertRaisesRegex(ValueError, "trial counts differ"):
            judge.validate_blind_results(tampered)

        tampered = copy.deepcopy(blind)
        tampered["safety_category_stability"]["stable"] = 0
        tampered["safety_category_stability"]["unstable"] = 1
        with self.assertRaisesRegex(ValueError, "safety_category_stability"):
            judge.validate_blind_results(tampered)

    def test_reveal_checks_hash_run_pairs_and_maps_arm_summaries(self):
        _source, blind = self.build_results()
        key = {
            "schema_version": 1,
            "run_id": "source-run",
            "commitment_nonce": "d" * 64,
            "pairs": {
                "p1": {
                    "left_arm": "candidate",
                    "right_arm": "control",
                    "left_run_id": "c1",
                    "right_run_id": "n1",
                },
                "p2": {
                    "left_arm": "control",
                    "right_arm": "candidate",
                    "left_run_id": "n2",
                    "right_run_id": "c2",
                },
            },
        }
        revealed = judge.reveal_results(
            blind, key, bundle_sha256=SHA_A, key_sha256=SHA_B
        )
        candidate = revealed["arm_summaries"]["candidate"]
        control = revealed["arm_summaries"]["control"]
        self.assertEqual((candidate["wins"], candidate["losses"]), (1, 0))
        self.assertEqual((control["wins"], control["losses"]), (0, 1))
        self.assertEqual(candidate["samples"], 2)
        self.assertEqual(candidate["lengths"]["words_total"], 3)
        self.assertEqual(control["lengths"]["words_total"], 7)
        self.assertEqual(revealed["pairs"][0]["winner"], "candidate")
        self.assertEqual(
            revealed["pairs"][0]["arm_consensus_flags"]["candidate"],
            ["missing_required_content"],
        )

        bad_hash = copy.deepcopy(blind)
        bad_hash["bundle_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "bundle_sha256 differs"):
            judge.reveal_results(bad_hash, key, bundle_sha256=SHA_A, key_sha256=SHA_B)
        bad_key = copy.deepcopy(key)
        bad_key["run_id"] = "other"
        with self.assertRaisesRegex(ValueError, "run_id differs"):
            judge.reveal_results(blind, bad_key, bundle_sha256=SHA_A, key_sha256=SHA_B)
        bad_key = copy.deepcopy(key)
        del bad_key["pairs"]["p2"]
        with self.assertRaisesRegex(ValueError, "pair ids differ"):
            judge.reveal_results(blind, bad_key, bundle_sha256=SHA_A, key_sha256=SHA_B)

    def test_blind_builder_requires_exact_pair_ids_and_hashes(self):
        with self.assertRaisesRegex(ValueError, "pair result ids differ"):
            judge.build_blind_results(
                judge_run_id="j",
                bundle=bundle(),
                bundle_sha256=SHA_A,
                judge_config_sha256=SHA_B,
                pair_results={},
            )
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            judge.build_blind_results(
                judge_run_id="j",
                bundle=bundle(),
                bundle_sha256="bad",
                judge_config_sha256=SHA_B,
                pair_results={"p1": stable_left()},
            )


if __name__ == "__main__":
    unittest.main()
