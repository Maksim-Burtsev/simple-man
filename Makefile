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
MODEL_ARG := $(if $(MODEL),--model $(MODEL),)
LIMIT_ARG := $(if $(filter-out 0,$(LIMIT)),--limit $(LIMIT),)

.PHONY: test bench bench-check bench-dry-run bench-refresh bench-smoke bench-compare-sample bench-reference bench-reference-check bench-reference-dry-run bench-reference-refresh bench-reference-smoke

test:
	$(PYTHON) -m unittest discover -s tests
	$(PYTHON) -m py_compile evals/benchmark_lib.py evals/measure.py evals/run_codex.py evals/run_skill_comparison.py
	$(PYTHON) evals/run_codex.py --dry-run --limit 1
	$(PYTHON) evals/run_codex.py --suite reference_compression --dry-run --limit 1

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
