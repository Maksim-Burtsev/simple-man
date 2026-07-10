from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import auto_judge_lib as judge  # noqa: E402
import check_auto_judge as gate  # noqa: E402
import reveal_auto_judge as reveal_runner  # noqa: E402
import run_auto_judge as judge_runner  # noqa: E402
import run_blind_review as answer_runner  # noqa: E402
from review_lib import build_arm_specs  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_answer_raw(path: Path, text: str) -> dict[str, int]:
    usage = {"input_tokens": 10, "output_tokens": 4}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": text},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": usage}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return usage


def write_judge_raw(path: Path, judgment: dict) -> dict[str, int]:
    usage = {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": json.dumps(judgment),
                        },
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": usage}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return usage


class ArtifactChainTests(unittest.TestCase):
    def build_chain(self, root: Path) -> dict[str, object]:
        prompts_path = root / "prompts.jsonl"
        prompt = {
            "id": "task-1",
            "category": "neutral_status",
            "language": "en",
            "prompt": "Report the verified status.",
            "verified_context": "Tests passed and nothing was deployed.",
        }
        prompts_path.write_text(json.dumps(prompt) + "\n", encoding="utf-8")
        prompts = [prompt]
        candidate_policy_path = (
            ROOT / "evals" / "policies" / "simple_man_candidate_runtime.md"
        )
        policy = candidate_policy_path.read_text(encoding="utf-8")
        arm_map = build_arm_specs(policy)
        arms = [arm_map["native_low"], arm_map["simple_man_runtime"]]
        answer_private = root / "answer" / "private"
        bundle_path = root / "answer" / "public" / "bundle.json"
        key_path = answer_private / "key.json"
        answer_runner_hash = "a" * 64
        answer_config = answer_runner.config_payload(
            prompts=prompts,
            arms=arms,
            trials=1,
            model="answer-model",
            effort="high",
            cli_version="codex-test",
            runner_sha256=answer_runner_hash,
            source_git_commit="1" * 40,
            source_git_dirty=False,
        )
        answer_config_sha = gate.sha256_text(gate.canonical_json(answer_config))
        answer_run_id = "answer-run"
        secret = "benchmark-secret"
        answer_results = []
        run_keys = []
        for spec in arms:
            key = answer_runner.RunKey(prompt["id"], spec.name, 1)
            run_keys.append(key)
            private_id = gate.private_run_id(answer_config_sha, key)
            raw_path = answer_private / "raw" / f"{private_id}.jsonl"
            text = (
                "Tests passed; nothing was deployed."
                if spec.name == "simple_man_runtime"
                else "The tests passed successfully, and nothing was deployed."
            )
            usage = write_answer_raw(raw_path, text)
            identity = answer_runner.result_identity(
                run_id=private_id,
                key=key,
                prompt=prompt["prompt"],
                spec=spec,
                model="answer-model",
                effort="high",
                cli_version="codex-test",
                runner_sha256=answer_runner_hash,
            )
            result = {
                **identity,
                "text": text,
                "usage": usage,
                "duration_ms": 1,
                "raw_sha256": gate.sha256_file(raw_path),
            }
            write_json(answer_private / "runs" / f"{private_id}.json", result)
            answer_results.append(result)

        schedule = answer_runner.secret_seeded_execution_schedule(secret, run_keys)
        schedule_payload = answer_runner.execution_schedule_payload(
            run_id=answer_run_id,
            config_sha256=answer_config_sha,
            run_keys=schedule,
        )
        schedule_sha = gate.sha256_text(gate.canonical_json(schedule_payload))
        answer_manifest = {
            "schema_version": 1,
            "run_id": answer_run_id,
            "created_at": "2026-07-10T00:00:00+00:00",
            "blinding_secret": secret,
            "execution_schedule_sha256": schedule_sha,
            "config_sha256": answer_config_sha,
            "config": answer_config,
        }
        answer_manifest_path = answer_private / "manifest.json"
        write_json(answer_manifest_path, answer_manifest)
        write_json(answer_private / "execution-schedule.json", schedule_payload)
        metadata = {
            "generated_at": answer_manifest["created_at"],
            "model": "answer-model",
            "effort": "high",
            "trials": 1,
            "task_count": 1,
            "pair_count": 1,
            "prompt_corpus_sha256": gate.prompt_corpus_sha256(prompts),
            "codex_cli_version": "codex-test",
        }
        bundle, private_key = gate.build_blind_bundle(
            public_run_id=answer_run_id,
            metadata=metadata,
            prompts=prompts,
            arms=[spec.name for spec in arms],
            trials=1,
            blinding_secret=secret,
            results=answer_results,
        )
        write_json(bundle_path, bundle)
        write_json(key_path, private_key)

        calibration_path = root / "calibration.jsonl"
        calibration_row = {
            "id": "cal-tie",
            "category": "calibration_tie",
            "language": "en",
            "prompt": "Choose between equivalent responses.",
            "verified_context": "Both responses are complete and correct.",
            "response_a": "Complete result.",
            "response_b": "Complete result.",
            "expected_verdict": "tie",
            "expected_flags": {"A": [], "B": []},
        }
        calibration_path.write_text(
            json.dumps(calibration_row) + "\n", encoding="utf-8"
        )
        calibration = [calibration_row]
        calibration_calls, benchmark_calls = judge_runner.build_calls(
            bundle=bundle,
            calibration=calibration,
            judge_trials=1,
        )
        calls = calibration_calls + benchmark_calls
        judge_root = answer_private / "auto-judge"
        judge_policy_path = root / "judge-policy.md"
        judge_policy_path.write_text("Judge anonymous responses.\n", encoding="utf-8")
        schema_path = root / "schema.json"
        schema_path.write_text("{}\n", encoding="utf-8")
        judge_config = {
            "bundle_sha256": gate.sha256_file(bundle_path),
            "calibration_sha256": gate.sha256_file(calibration_path),
            "policy_sha256": gate.sha256_file(judge_policy_path),
            "output_schema_sha256": gate.sha256_file(schema_path),
            "model": "judge-model",
            "effort": "medium",
            "judge_trials_per_orientation": 1,
            "call_count": len(calls),
            "codex_cli_version": "codex-test",
            "runner_sha256": "b" * 64,
        }
        judge_config_sha = gate.sha256_text(gate.canonical_json(judge_config))
        judge_manifest = {
            "schema_version": 2,
            "run_id": "judge-run",
            "created_at": "2026-07-10T00:01:00+00:00",
            "config_sha256": judge_config_sha,
            "config": judge_config,
        }
        judge_manifest_path = judge_root / "manifest.json"
        write_json(judge_manifest_path, judge_manifest)
        frozen = {
            "bundle.json": bundle_path,
            "calibration.jsonl": calibration_path,
            "judge-policy.md": judge_policy_path,
            "output-schema.json": schema_path,
        }
        for filename, source in frozen.items():
            destination = judge_root / "inputs" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

        raw_judgment = {
            "verdict": "tie",
            "flags": {"A": [], "B": []},
            "rationale": "Both responses are equivalent.",
        }
        judge_results = {}
        for call in calls:
            identity = judge_runner.call_identity(
                run_id="judge-run",
                config_sha256=judge_config_sha,
                call=call,
                model="judge-model",
                effort="medium",
                cli_version="codex-test",
                runner_sha256="b" * 64,
            )
            call_id = identity["call_id"]
            raw_path = judge_root / "raw" / f"{call_id}.jsonl"
            usage = write_judge_raw(raw_path, raw_judgment)
            result = {
                **identity,
                "judgment": raw_judgment,
                "usage": usage,
                "duration_ms": 1,
                "raw_sha256": gate.sha256_file(raw_path),
            }
            write_json(judge_root / "runs" / f"{call_id}.json", result)
            judge_results[
                (call.kind, call.subject_id, call.orientation, call.trial)
            ] = result

        calibration_aggregated = judge_runner.aggregate_results(
            calibration_calls, judge_results
        )
        calibration_report = judge.grade_calibration(
            calibration, calibration_aggregated
        )
        benchmark_aggregated = judge_runner.aggregate_results(
            benchmark_calls, judge_results
        )
        blind = judge.build_blind_results(
            judge_run_id="judge-run",
            bundle=bundle,
            bundle_sha256=gate.sha256_file(bundle_path),
            judge_config_sha256=judge_config_sha,
            pair_results=benchmark_aggregated,
            calibration=calibration_report,
        )
        blind_results_path = judge_root / "blind-results.json"
        write_json(blind_results_path, blind)
        revealed = judge.reveal_results(
            blind,
            private_key,
            bundle_sha256=gate.sha256_file(bundle_path),
            key_sha256=gate.sha256_file(key_path),
        )
        revealed["blind_results_sha256"] = gate.sha256_file(blind_results_path)
        revealed["blind_reliability"] = reveal_runner.reliability_report(
            blind, min_stable_rate=0.9
        )
        return {
            "revealed": revealed,
            "answer_manifest_path": answer_manifest_path,
            "judge_manifest_path": judge_manifest_path,
            "bundle_path": bundle_path,
            "key_path": key_path,
            "blind_results_path": blind_results_path,
            "prompts_path": prompts_path,
            "candidate_policy_path": candidate_policy_path,
            "calibration_path": calibration_path,
            "answer_result_path": next((answer_private / "runs").glob("*.json")),
        }

    def test_chain_rebuilds_from_raw_and_rejects_revealed_or_answer_tamper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            chain = self.build_chain(Path(temporary))
            report = gate.verify_artifact_chain(
                chain["revealed"],
                answer_manifest_path=chain["answer_manifest_path"],
                judge_manifest_path=chain["judge_manifest_path"],
                bundle_path=chain["bundle_path"],
                key_path=chain["key_path"],
                blind_results_path=chain["blind_results_path"],
                prompts_path=chain["prompts_path"],
                candidate_policy_path=chain["candidate_policy_path"],
                calibration_path=chain["calibration_path"],
                min_stable_rate=0.9,
            )
            self.assertTrue(report["passed"])
            self.assertTrue(report["revealed_results_rebuilt"])

            tampered = copy.deepcopy(chain["revealed"])
            tampered["pairs"][0]["task_id"] = "forged-task"
            with self.assertRaisesRegex(ValueError, "revealed results differ"):
                gate.verify_artifact_chain(
                    tampered,
                    answer_manifest_path=chain["answer_manifest_path"],
                    judge_manifest_path=chain["judge_manifest_path"],
                    bundle_path=chain["bundle_path"],
                    key_path=chain["key_path"],
                    blind_results_path=chain["blind_results_path"],
                    prompts_path=chain["prompts_path"],
                    candidate_policy_path=chain["candidate_policy_path"],
                    calibration_path=chain["calibration_path"],
                    min_stable_rate=0.9,
                )

            result_path = chain["answer_result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["text"] = "forged answer"
            write_json(result_path, result)
            with self.assertRaisesRegex(RuntimeError, "differs from raw JSONL"):
                gate.reconstruct_answer_artifacts(
                    manifest_path=chain["answer_manifest_path"],
                    bundle_path=chain["bundle_path"],
                    key_path=chain["key_path"],
                    prompts_path=chain["prompts_path"],
                    candidate_policy_path=chain["candidate_policy_path"],
                )


if __name__ == "__main__":
    unittest.main()
