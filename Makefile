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
BENCH_V3_DIR ?= evals/releases/v0.3.1/run
BENCH_V3_REPORT ?= evals/releases/v0.3.1/report.md
BENCH_V3_MAX_CALLS ?= 700
SESSION_RELEASE ?= evals/releases/session-v1
SESSION_PREREG ?= $(SESSION_RELEASE)/preregistration.json
SESSION_RUN ?= run
SESSION_JOBS ?= $(HOME)/.cache/simple-man/session-jobs/$(SESSION_RUN)
SKILLSBENCH ?= $(HOME)/.cache/simple-man/skillsbench
ARM ?= N
BATCH ?= 0
MODEL_ARG := $(if $(MODEL),--model $(MODEL),)
LIMIT_ARG := $(if $(filter-out 0,$(LIMIT)),--limit $(LIMIT),)

.PHONY: session-dry-run session-pilot session-run session-retry session-collect session-report session-check bench-v3 bench-v3-dry-run bench-v3-report bench-v3-check test package-check eval-foundation-check eval-gates-check eval-release-dry-run bench bench-check bench-dry-run bench-refresh bench-smoke bench-compare-sample bench-reference bench-reference-check bench-reference-dry-run bench-reference-refresh bench-reference-smoke

test: package-check
	$(PYTHON) -m unittest discover -s tests
	PYTHONPYCACHEPREFIX=/tmp/simple-man-pycache $(PYTHON) -m py_compile evals/benchmark_lib.py evals/measure.py evals/run_codex.py evals/run_skill_comparison.py evals/eval_v2_lib.py evals/run_eval_v2.py evals/check_eval_v2.py evals/coding_gate.py evals/bench/runner.py evals/bench/report.py evals/session/run_ab.py evals/session/collect.py evals/session/session_report.py evals/session/session_gates.py evals/fixtures/skill-comparison/python-payment-ledger/app.py evals/fixtures/skill-comparison/python-payment-ledger/runtime.py evals/fixtures/skill-comparison/sqlite-rollout-runner/app.py evals/fixtures/skill-comparison/sqlite-rollout-runner/runtime.py
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

# Session benchmark: real Claude Code sessions on SkillsBench through Harbor.
# Needs `uv tool install harbor`, Docker, a SkillsBench checkout at the
# registered commit, and CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`.
session-dry-run:
	$(PYTHON) evals/session/run_ab.py --prereg $(SESSION_PREREG) --arm $(ARM) --batch $(BATCH) --jobs-dir $(SESSION_JOBS) --skillsbench $(SKILLSBENCH) --dry-run

session-pilot:
	$(PYTHON) evals/session/run_ab.py --prereg $(SESSION_PREREG) --arm $(ARM) --pilot 3 --jobs-dir $(SESSION_JOBS) --skillsbench $(SKILLSBENCH)

session-run:
	$(PYTHON) evals/session/run_ab.py --prereg $(SESSION_PREREG) --arm $(ARM) --batch $(BATCH) --jobs-dir $(SESSION_JOBS) --skillsbench $(SKILLSBENCH)

session-retry:
	$(PYTHON) evals/session/run_ab.py --prereg $(SESSION_PREREG) --arm $(ARM) --retry $(TASK) --jobs-dir $(SESSION_JOBS) --skillsbench $(SKILLSBENCH)

session-collect:
	$(PYTHON) evals/session/collect.py --jobs-dir $(SESSION_JOBS) --prereg $(SESSION_PREREG) --write $(SESSION_RELEASE)/$(SESSION_RUN)/trials.jsonl

session-report:
	$(PYTHON) evals/session/session_report.py --trials $(SESSION_RELEASE)/$(SESSION_RUN)/trials.jsonl --prereg $(SESSION_PREREG) --write $(SESSION_RELEASE)/$(SESSION_RUN)/report.md
	$(PYTHON) evals/session/session_gates.py --trials $(SESSION_RELEASE)/$(SESSION_RUN)/trials.jsonl --prereg $(SESSION_PREREG) --write $(SESSION_RELEASE)/$(SESSION_RUN)/gates.md

session-check:
	$(PYTHON) evals/session/session_report.py --trials $(SESSION_RELEASE)/$(SESSION_RUN)/trials.jsonl --prereg $(SESSION_PREREG) --check $(SESSION_RELEASE)/$(SESSION_RUN)/report.md
	$(PYTHON) evals/session/session_gates.py --trials $(SESSION_RELEASE)/$(SESSION_RUN)/trials.jsonl --prereg $(SESSION_PREREG) --check $(SESSION_RELEASE)/$(SESSION_RUN)/gates.md

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
