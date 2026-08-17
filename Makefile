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
BENCH_V3_DIR ?= evals/releases/v0.3.0/run
BENCH_V3_REPORT ?= evals/releases/v0.3.0/report.md
BENCH_V3_MAX_CALLS ?= 700
MODEL_ARG := $(if $(MODEL),--model $(MODEL),)
LIMIT_ARG := $(if $(filter-out 0,$(LIMIT)),--limit $(LIMIT),)

.PHONY: bench-v3 bench-v3-dry-run bench-v3-report bench-v3-check test package-check eval-foundation-check eval-gates-check eval-release-dry-run bench bench-check bench-dry-run bench-refresh bench-smoke bench-compare-sample bench-reference bench-reference-check bench-reference-dry-run bench-reference-refresh bench-reference-smoke

test: package-check
	$(PYTHON) -m unittest discover -s tests
	PYTHONPYCACHEPREFIX=/tmp/simple-man-pycache $(PYTHON) -m py_compile evals/benchmark_lib.py evals/measure.py evals/run_codex.py evals/run_skill_comparison.py evals/eval_v2_lib.py evals/run_eval_v2.py evals/check_eval_v2.py evals/coding_gate.py evals/bench/runner.py evals/bench/report.py evals/fixtures/skill-comparison/python-payment-ledger/app.py evals/fixtures/skill-comparison/python-payment-ledger/runtime.py evals/fixtures/skill-comparison/sqlite-rollout-runner/app.py evals/fixtures/skill-comparison/sqlite-rollout-runner/runtime.py
	$(PYTHON) evals/run_codex.py --dry-run --limit 1
	$(PYTHON) evals/run_codex.py --suite reference_compression --dry-run --limit 1

bench-v3-dry-run:
	$(PYTHON) evals/bench/runner.py all --output-dir $(BENCH_V3_DIR) --max-calls $(BENCH_V3_MAX_CALLS) --dry-run

bench-v3:
	$(PYTHON) evals/bench/runner.py all --output-dir $(BENCH_V3_DIR) --max-calls $(BENCH_V3_MAX_CALLS) $(MODEL_ARG)

bench-v3-report:
	$(PYTHON) evals/bench/report.py --run-dir $(BENCH_V3_DIR) --write $(BENCH_V3_REPORT)

bench-v3-check:
	$(PYTHON) evals/bench/report.py --run-dir $(BENCH_V3_DIR) --check $(BENCH_V3_REPORT)

package-check:
	bash -n install.sh
	$(PYTHON) scripts/sync_surfaces.py --check
	$(PYTHON) -m json.tool .agents/plugins/marketplace.json >/dev/null
	$(PYTHON) -m json.tool plugins/simple-man/.codex-plugin/plugin.json >/dev/null

eval-foundation-check:
	$(PYTHON) -m unittest tests/test_eval_foundation.py -v

eval-gates-check:
	$(PYTHON) -m unittest tests/test_eval_v2.py tests/test_coding_gate.py tests/test_eval_v2_gates.py -v
	$(PYTHON) evals/check_eval_v2.py gates

eval-release-dry-run:
	$(PYTHON) evals/check_eval_v2.py release-dry-run

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
