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
MODEL_ARG := $(if $(MODEL),--model $(MODEL),)
LIMIT_ARG := $(if $(filter-out 0,$(LIMIT)),--limit $(LIMIT),)

.PHONY: test bench bench-check bench-dry-run bench-refresh bench-smoke bench-compare-sample bench-reference bench-reference-check bench-reference-dry-run bench-reference-refresh bench-reference-smoke review-smoke-dry-run review-smoke review-serve

test:
	$(PYTHON) -m unittest discover -s tests
	$(PYTHON) -m py_compile evals/benchmark_lib.py evals/measure.py evals/review_lib.py evals/review_server.py evals/run_blind_review.py evals/run_codex.py evals/run_skill_comparison.py
	node --check evals/review_app/app.js
	$(PYTHON) evals/run_codex.py --dry-run --limit 1
	$(PYTHON) evals/run_codex.py --suite reference_compression --dry-run --limit 1

review-smoke-dry-run:
	$(PYTHON) evals/run_blind_review.py --dry-run --prompts $(REVIEW_PROMPTS) --output-dir $(REVIEW_OUTPUT) --model $(REVIEW_MODEL) --effort $(REVIEW_EFFORT)

review-smoke:
	$(PYTHON) evals/run_blind_review.py --prompts $(REVIEW_PROMPTS) --output-dir $(REVIEW_OUTPUT) --model $(REVIEW_MODEL) --effort $(REVIEW_EFFORT)

review-serve:
	$(PYTHON) evals/review_server.py --bundle $(REVIEW_OUTPUT)/public/bundle.json --key $(REVIEW_OUTPUT)/private/key.json --port $(REVIEW_PORT)

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
