PYTHON ?= python3
BENCH_PYTHON ?= uv run --with tiktoken python
MODEL ?=
TRIALS ?= 3
LIMIT ?= 0
BENCH_SNAPSHOT ?= evals/snapshots/codex-results.json
REFERENCE_SNAPSHOT ?= evals/snapshots/reference-results.json
SMOKE_SNAPSHOT ?= /tmp/simple-man-codex-smoke-results.json
REFERENCE_SMOKE_SNAPSHOT ?= /tmp/simple-man-reference-smoke-results.json
SAMPLE_SNAPSHOT ?= /tmp/simple-man-caveman-sample-results.json
REVIEW_MODEL ?= gpt-5.5
REVIEW_EFFORT ?= high
REVIEW_OUTPUT ?= .local-fixtures/blind-review
REVIEW_PROMPTS ?= evals/prompts/review_smoke.jsonl
REVIEW_PORT ?= 8765
AUTO_REVIEW_MODEL ?= gpt-5.5
AUTO_REVIEW_EFFORT ?= high
AUTO_REVIEW_OUTPUT ?= .local-fixtures/review-auto-heldout-v1
AUTO_REVIEW_PROMPTS ?= evals/prompts/review_auto_holdout_v1.jsonl
AUTO_REVIEW_POLICY ?= evals/policies/simple_man_candidate_runtime.md
AUTO_REVIEW_TRIALS ?= 1
AUTO_REVIEW_MAX_CALLS ?= 48
AUTO_REVIEW_MAX_REPORTED_TOKENS ?= 1500000
JUDGE_MODEL ?= gpt-5.4
JUDGE_EFFORT ?= medium
JUDGE_TRIALS ?= 2
JUDGE_OUTPUT ?= $(AUTO_REVIEW_OUTPUT)/private/auto-judge
JUDGE_MAX_PAIRS ?= 24
JUDGE_MAX_CALLS ?= 180
JUDGE_MAX_TOTAL_INPUT_CHARS ?= 1000000
JUDGE_MAX_REPORTED_TOKENS ?= 1500000
QUALITY_MODEL ?= gpt-5.5
QUALITY_EFFORT ?= high
QUALITY_OUTPUT ?= .local-fixtures/review-quality-v1
QUALITY_POLICY ?= evals/policies/simple_man_candidate_runtime.md
MODEL_ARG := $(if $(MODEL),--model $(MODEL),)
LIMIT_ARG := $(if $(filter-out 0,$(LIMIT)),--limit $(LIMIT),)

.PHONY: test bench bench-check bench-dry-run bench-refresh bench-smoke bench-compare-sample bench-reference bench-reference-check bench-reference-dry-run bench-reference-refresh bench-reference-smoke review-smoke-dry-run review-smoke review-serve review-auto-dry-run review-auto-generate review-auto-judge-dry-run review-auto-judge review-auto-reveal review-auto-gate review-auto review-quality-dry-run review-quality review-automatic

test:
	$(PYTHON) -m unittest discover -s tests
	$(PYTHON) -m py_compile evals/auto_judge_lib.py evals/benchmark_lib.py evals/check_auto_judge.py evals/measure.py evals/reveal_auto_judge.py evals/review_lib.py evals/review_server.py evals/run_auto_judge.py evals/run_blind_review.py evals/run_codex.py evals/run_skill_comparison.py
	node --check evals/review_app/app.js
	$(PYTHON) evals/run_codex.py --dry-run --limit 1
	$(PYTHON) evals/run_codex.py --suite reference_compression --dry-run --limit 1

review-smoke-dry-run:
	$(PYTHON) evals/run_blind_review.py --dry-run --prompts $(REVIEW_PROMPTS) --output-dir $(REVIEW_OUTPUT) --model $(REVIEW_MODEL) --effort $(REVIEW_EFFORT)

review-smoke:
	$(PYTHON) evals/run_blind_review.py --prompts $(REVIEW_PROMPTS) --output-dir $(REVIEW_OUTPUT) --model $(REVIEW_MODEL) --effort $(REVIEW_EFFORT)

review-serve:
	$(PYTHON) evals/review_server.py --bundle $(REVIEW_OUTPUT)/public/bundle.json --key $(REVIEW_OUTPUT)/private/key.json --port $(REVIEW_PORT)

review-auto-dry-run:
	$(PYTHON) evals/run_blind_review.py --dry-run --require-clean-source --prompts $(AUTO_REVIEW_PROMPTS) --runtime-policy $(AUTO_REVIEW_POLICY) --output-dir $(AUTO_REVIEW_OUTPUT) --model $(AUTO_REVIEW_MODEL) --effort $(AUTO_REVIEW_EFFORT) --trials $(AUTO_REVIEW_TRIALS) --max-calls $(AUTO_REVIEW_MAX_CALLS) --max-total-reported-tokens $(AUTO_REVIEW_MAX_REPORTED_TOKENS)

review-auto-generate:
	$(PYTHON) evals/run_blind_review.py --require-clean-source --prompts $(AUTO_REVIEW_PROMPTS) --runtime-policy $(AUTO_REVIEW_POLICY) --output-dir $(AUTO_REVIEW_OUTPUT) --model $(AUTO_REVIEW_MODEL) --effort $(AUTO_REVIEW_EFFORT) --trials $(AUTO_REVIEW_TRIALS) --max-calls $(AUTO_REVIEW_MAX_CALLS) --max-total-reported-tokens $(AUTO_REVIEW_MAX_REPORTED_TOKENS)

review-auto-judge-dry-run:
	$(PYTHON) evals/run_auto_judge.py --dry-run --require-clean-source --bundle $(AUTO_REVIEW_OUTPUT)/public/bundle.json --output-dir $(JUDGE_OUTPUT) --model $(JUDGE_MODEL) --effort $(JUDGE_EFFORT) --judge-trials $(JUDGE_TRIALS) --max-pairs $(JUDGE_MAX_PAIRS) --max-calls $(JUDGE_MAX_CALLS) --max-total-input-chars $(JUDGE_MAX_TOTAL_INPUT_CHARS) --max-total-reported-tokens $(JUDGE_MAX_REPORTED_TOKENS)

review-auto-judge: review-auto-generate
	$(PYTHON) evals/run_auto_judge.py --require-clean-source --bundle $(AUTO_REVIEW_OUTPUT)/public/bundle.json --output-dir $(JUDGE_OUTPUT) --model $(JUDGE_MODEL) --effort $(JUDGE_EFFORT) --judge-trials $(JUDGE_TRIALS) --max-pairs $(JUDGE_MAX_PAIRS) --max-calls $(JUDGE_MAX_CALLS) --max-total-input-chars $(JUDGE_MAX_TOTAL_INPUT_CHARS) --max-total-reported-tokens $(JUDGE_MAX_REPORTED_TOKENS)

review-auto-reveal: review-auto-judge
	$(PYTHON) evals/reveal_auto_judge.py --bundle $(AUTO_REVIEW_OUTPUT)/public/bundle.json --blind-results $(JUDGE_OUTPUT)/blind-results.json --key $(AUTO_REVIEW_OUTPUT)/private/key.json --output $(JUDGE_OUTPUT)/revealed-results.json

review-auto-gate: review-auto-reveal
	$(PYTHON) evals/check_auto_judge.py --revealed $(JUDGE_OUTPUT)/revealed-results.json --answer-manifest $(AUTO_REVIEW_OUTPUT)/private/manifest.json --judge-manifest $(JUDGE_OUTPUT)/manifest.json --bundle $(AUTO_REVIEW_OUTPUT)/public/bundle.json --key $(AUTO_REVIEW_OUTPUT)/private/key.json --blind-results $(JUDGE_OUTPUT)/blind-results.json --prompts $(AUTO_REVIEW_PROMPTS) --candidate-policy $(AUTO_REVIEW_POLICY) --judge-policy evals/policies/blind_judge.md --calibration evals/prompts/judge_calibration.jsonl --output-schema evals/schemas/blind_judge.schema.json --answer-model $(AUTO_REVIEW_MODEL) --answer-effort $(AUTO_REVIEW_EFFORT) --answer-trials $(AUTO_REVIEW_TRIALS) --judge-model $(JUDGE_MODEL) --judge-effort $(JUDGE_EFFORT) --judge-trials $(JUDGE_TRIALS) --output $(JUDGE_OUTPUT)/auto-gate.json

review-auto: review-auto-gate

review-quality-dry-run:
	$(PYTHON) evals/run_skill_comparison.py --dry-run --candidate-policy $(QUALITY_POLICY) --output-dir $(QUALITY_OUTPUT) --model $(QUALITY_MODEL) --effort $(QUALITY_EFFORT)

review-quality:
	$(PYTHON) evals/run_skill_comparison.py --candidate-policy $(QUALITY_POLICY) --output-dir $(QUALITY_OUTPUT) --model $(QUALITY_MODEL) --effort $(QUALITY_EFFORT)

review-automatic: review-quality
	$(MAKE) review-auto

bench:
	$(PYTHON) evals/measure.py --snapshot $(BENCH_SNAPSHOT)

bench-check:
	$(PYTHON) evals/measure.py --snapshot $(BENCH_SNAPSHOT) --check

bench-dry-run:
	$(PYTHON) evals/run_codex.py --dry-run $(MODEL_ARG) --trials $(TRIALS) $(LIMIT_ARG)

bench-refresh:
	$(BENCH_PYTHON) evals/run_codex.py --snapshot $(BENCH_SNAPSHOT) --overwrite $(MODEL_ARG) --trials $(TRIALS) $(LIMIT_ARG)

bench-smoke:
	$(BENCH_PYTHON) evals/run_codex.py --snapshot $(SMOKE_SNAPSHOT) --overwrite $(MODEL_ARG) --trials 1 --limit 1
	$(PYTHON) evals/measure.py --snapshot $(SMOKE_SNAPSHOT) --check
	$(PYTHON) evals/measure.py --snapshot $(SMOKE_SNAPSHOT)

bench-compare-sample:
	$(BENCH_PYTHON) evals/run_codex.py --snapshot $(SAMPLE_SNAPSHOT) --overwrite $(MODEL_ARG) --trials 1 --limit 10
	$(PYTHON) evals/measure.py --snapshot $(SAMPLE_SNAPSHOT) --check
	$(PYTHON) evals/measure.py --snapshot $(SAMPLE_SNAPSHOT)

bench-reference:
	$(PYTHON) evals/measure.py --snapshot $(REFERENCE_SNAPSHOT)

bench-reference-check:
	$(PYTHON) evals/measure.py --snapshot $(REFERENCE_SNAPSHOT) --check

bench-reference-dry-run:
	$(PYTHON) evals/run_codex.py --suite reference_compression --dry-run $(MODEL_ARG) --trials $(TRIALS) $(LIMIT_ARG)

bench-reference-refresh:
	$(BENCH_PYTHON) evals/run_codex.py --suite reference_compression --snapshot $(REFERENCE_SNAPSHOT) --overwrite $(MODEL_ARG) --trials $(TRIALS) $(LIMIT_ARG)

bench-reference-smoke:
	$(BENCH_PYTHON) evals/run_codex.py --suite reference_compression --snapshot $(REFERENCE_SMOKE_SNAPSHOT) --overwrite $(MODEL_ARG) --trials 1 --limit 1
	$(PYTHON) evals/measure.py --snapshot $(REFERENCE_SMOKE_SNAPSHOT) --check
	$(PYTHON) evals/measure.py --snapshot $(REFERENCE_SMOKE_SNAPSHOT)
