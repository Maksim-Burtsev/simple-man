#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LOCAL_ROOT = REPO / ".local-fixtures" / "skill-comparison"
SEEDS = REPO / "evals" / "fixtures" / "skill-comparison"
RUNS = LOCAL_ROOT / "runs"
OUTPUTS = LOCAL_ROOT / "outputs"
RAW = LOCAL_ROOT / "raw"
HOMES = LOCAL_ROOT / "codex-homes"
REPORT = REPO / "evals" / "reports" / "codex-skill-comparison.md"

MODEL = os.environ.get("MODEL", "gpt-5.5")
EFFORT = os.environ.get("EFFORT", "xhigh")
CODEX = os.environ.get("CODEX", "codex")
CAVEMAN_SKILL = Path(
    os.environ.get(
        "CAVEMAN_SKILL",
        str(
            Path.home()
            / ".codex"
            / ".tmp"
            / "marketplaces"
            / "caveman-repo"
            / "skills"
            / "caveman"
            / "SKILL.md"
        ),
    )
)
SIMPLE_MAN_SKILL = REPO / "skills" / "simple-man" / "SKILL.md"
AUTH = Path.home() / ".codex" / "auth.json"


@dataclass(frozen=True)
class Project:
    key: str
    title: str
    task: str
    check: list[str]


PROJECTS = [
    Project(
        key="node-auth-api",
        title="Node auth API",
        task=textwrap.dedent(
            """
            We have an auth bug: expired sessions are still accepted.

            Please inspect the project, fix the bug, run the relevant tests, and
            give me an engineering handoff with:
            - root cause
            - files changed
            - validation command and result
            - any remaining risk

            Answer in English. Do not mention benchmark internals, isolated
            CODEX_HOME directories, raw logs, or absolute run-copy paths.
            """
        ).strip(),
        check=["npm", "test"],
    ),
    Project(
        key="python-payment-ledger",
        title="Python payment ledger",
        task=textwrap.dedent(
            """
            We have a duplicate-charge retry bug. A gateway timeout can happen
            after the provider accepted the charge, and retrying with the same
            idempotency key currently creates another local charge.

            Please inspect the project, fix the idempotency bug, run the relevant
            tests, and give me an engineering handoff with:
            - root cause
            - files changed
            - validation command and result
            - any remaining risk

            Answer in English. Do not mention benchmark internals, isolated
            CODEX_HOME directories, raw logs, or absolute run-copy paths.
            """
        ).strip(),
        check=["python3", "-m", "unittest", "-v"],
    ),
    Project(
        key="sqlite-rollout-runner",
        title="SQLite rollout runner",
        task=textwrap.dedent(
            """
            We have an unsafe rollout order: the migration drops
            legacy_sessions.expires_at before the backup reads that column.

            Please inspect the project, fix the rollout order, run the relevant
            tests, and give me an engineering handoff with:
            - root cause
            - files changed
            - validation command and result
            - any remaining risk

            Answer in English. Do not mention benchmark internals, isolated
            CODEX_HOME directories, raw logs, or absolute run-copy paths.
            """
        ).strip(),
        check=["python3", "-m", "unittest", "-v"],
    ),
]

MODES = [
    ("baseline", "No brevity skill"),
    ("caveman-ultra", "Caveman ultra"),
    ("simple-man", "Simple Man"),
]


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    if len(cmd) >= 3 and cmd[1:3] == ["-m", "unittest"]:
        merged_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(cmd, cwd=cwd, env=merged_env, text=True, capture_output=True)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def remove_pycache(path: Path) -> None:
    for pycache in path.rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache)


def verify_seeds() -> dict[str, dict[str, str | int]]:
    results: dict[str, dict[str, str | int]] = {}
    for project in PROJECTS:
        proc = run(project.check, cwd=SEEDS / project.key)
        results[project.key] = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        print(f"seed check {project.key}: exit {proc.returncode}", flush=True)
    return results


def prepare_run_copy(project: Project, mode: str) -> Path:
    run_dir = RUNS / project.key / mode
    if run_dir.exists():
        shutil.rmtree(run_dir)
    shutil.copytree(SEEDS / project.key, run_dir)
    init = run(["git", "init"], cwd=run_dir)
    if init.returncode != 0:
        raise RuntimeError(init.stderr)
    add = run(["git", "add", "."], cwd=run_dir)
    if add.returncode != 0:
        raise RuntimeError(add.stderr)
    commit = run(
        [
            "git",
            "-c",
            "user.name=Codex Benchmark",
            "-c",
            "user.email=codex-benchmark@example.com",
            "commit",
            "-m",
            "Seed failing scenario",
        ],
        cwd=run_dir,
    )
    if commit.returncode != 0:
        raise RuntimeError(commit.stderr)
    return run_dir


def run_codex(project: Project, mode: str) -> dict[str, str | int]:
    run_dir = prepare_run_copy(project, mode)
    out_file = OUTPUTS / f"{project.key}-{mode}.txt"
    raw_file = RAW / f"{project.key}-{mode}.jsonl"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(HOMES / mode)

    cmd = [
        CODEX,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(run_dir),
        "-m",
        MODEL,
        "-c",
        f'model_reasoning_effort="{EFFORT}"',
        "-s",
        "workspace-write",
        "--json",
        "--output-last-message",
        str(out_file),
        project.task,
    ]
    print(f"codex {project.key} / {mode}: start", flush=True)
    with raw_file.open("w", encoding="utf-8") as raw:
        proc = subprocess.run(cmd, cwd=run_dir, env=env, text=True, stdout=raw, stderr=subprocess.PIPE)
    print(f"codex {project.key} / {mode}: exit {proc.returncode}", flush=True)

    final_text = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
    remove_pycache(run_dir)
    check = run(project.check, cwd=run_dir)
    remove_pycache(run_dir)
    print(f"post-check {project.key} / {mode}: exit {check.returncode}", flush=True)

    status = run(["git", "status", "--short"], cwd=run_dir)
    changed_files = run(["git", "diff", "--name-only"], cwd=run_dir)
    diff_stat = run(["git", "diff", "--stat"], cwd=run_dir)
    return {
        "codex_exit": proc.returncode,
        "codex_stderr": proc.stderr,
        "answer": final_text,
        "check_exit": check.returncode,
        "check_stdout": check.stdout,
        "check_stderr": check.stderr,
        "git_status": status.stdout,
        "changed_files": changed_files.stdout,
        "diff_stat": diff_stat.stdout,
        "run_dir": str(run_dir),
        "raw_file": str(raw_file),
    }


def fence(text: str) -> str:
    body = text.rstrip()
    delimiter = "```"
    while delimiter in body:
        delimiter += "`"
    return f"{delimiter}text\n{body}\n{delimiter}"


def table_cell(text: object) -> str:
    return (
        str(text)
        .strip()
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", "; ")
        or "(none)"
    )


def rendered_answer(text: str) -> str:
    answer = text.strip() or "(no final answer captured)"
    return answer


def compact_output(stdout: object, stderr: object, *, max_lines: int = 24) -> str:
    text = (str(stdout or "") + str(stderr or "")).strip()
    text = text.replace(str(SEEDS) + "/", "evals/fixtures/skill-comparison/")
    text = text.replace(str(LOCAL_ROOT) + "/", ".local-fixtures/skill-comparison/")
    text = text.replace(str(REPO) + "/", "")
    text = text.replace(str(Path.home()), "~")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"])


def result_badge(exit_code: object) -> str:
    return "PASS" if int(exit_code) == 0 else f"FAIL exit {exit_code}"


def seed_file_details(project: Project) -> list[str]:
    root = SEEDS / project.key
    lines = ["", "### Seed Project Files", ""]
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        lines.extend(
            [
                f"#### `{rel}`",
                "",
                fence(path.read_text(encoding="utf-8")),
                "",
            ]
        )
    return lines


def display_path(path: Path) -> str:
    text = str(path)
    text = text.replace(str(REPO) + "/", "")
    text = text.replace(str(Path.home()), "~")
    return text


def generate_markdown(seed_results: dict[str, dict[str, str | int]], results: dict[str, dict[str, dict[str, str | int]]]) -> None:
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    lines: list[str] = [
        "# Codex Skill Comparison: Caveman Ultra vs Simple Man",
        "",
        "This report compares real Codex coding-agent behavior on three small, reproducible bug-fix projects. Each scenario was run three times from the same failing seed state: no brevity skill, Caveman ultra, and Simple Man.",
        "",
        "The tables below show only benchmark-relevant facts and the full final Codex answers. Seed fixtures are tracked under `evals/fixtures/skill-comparison`; raw Codex JSONL logs, stderr warnings, and disposable run copies stay local.",
        "",
        "## Method",
        "",
        f"- Generated: `{generated}`",
        "- Base branch: `master`",
        f"- Model: `{MODEL}`",
        f"- Reasoning effort: `{EFFORT}`",
        f"- Caveman source: `{display_path(CAVEMAN_SKILL)}`",
        f"- Simple Man source: `{display_path(SIMPLE_MAN_SKILL)}`",
        "- Isolation: one disposable git repo per scenario per mode; seed files were committed before Codex ran, so changed-file lists show actual Codex edits.",
        "- Capture: final answers came from `codex exec --output-last-message`; validation was rerun outside Codex after each run.",
        "- Baseline column: no brevity/persona skill injected.",
        "",
        "## Summary Matrix",
        "",
        "| Scenario | Seed check | No brevity skill | Caveman ultra | Simple Man |",
        "| --- | --- | --- | --- | --- |",
    ]

    for project in PROJECTS:
        seed = seed_results[project.key]
        matrix_cells = []
        for mode, label in MODES:
            data = results[project.key][mode]
            changed = str(data["changed_files"]).strip().replace("\n", ", ") or "(none)"
            matrix_cells.append(
                f"{result_badge(data['check_exit'])}; `{' '.join(project.check)}`; {changed}; {len(str(data['answer']))} chars"
            )

        lines.append(
            f"| {project.title} | expected failing seed: exit `{seed['exit_code']}` | "
            f"{matrix_cells[0]} | {matrix_cells[1]} | {matrix_cells[2]} |"
        )

    for project in PROJECTS:
        seed = seed_results[project.key]
        lines.extend(
            [
                "",
                f"## Scenario: {project.title}",
                "",
                "### Task Prompt",
                "",
                fence(project.task),
                "",
                "### Seed Failure",
                "",
                f"- Command: `{' '.join(project.check)}`",
                f"- Exit: `{seed['exit_code']}`",
                "",
                "Seed output:",
                "",
                fence(compact_output(seed["stdout"], seed["stderr"])),
            ]
        )
        lines.extend(seed_file_details(project))
        lines.extend(
            [
                "### Mode Results Summary",
                "",
                "| Mode | Validation | Changed files | Diff stat | Answer length |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for mode, label in MODES:
            data = results[project.key][mode]
            changed = str(data["changed_files"]).strip() or "(none)"
            diff_stat = str(data["diff_stat"]).strip() or "(none)"
            validation = (
                f"{result_badge(data['check_exit'])}\n"
                f"{' '.join(project.check)}\n"
                f"Codex exit {data['codex_exit']}"
            )
            lines.append(
                "| "
                f"{table_cell(label)} | "
                f"{table_cell(validation)} | "
                f"{table_cell(changed)} | "
                f"{table_cell(diff_stat)} | "
                f"{len(str(data['answer']).strip())} chars |"
            )

        lines.extend(["", "### Full Final Answers", ""])
        for mode, label in MODES:
            data = results[project.key][mode]
            answer = rendered_answer(str(data["answer"]))
            lines.extend([f"#### {label}", "", answer, ""])

    write(REPORT, "\n".join(lines))
    print(f"report written: {REPORT}", flush=True)


def main() -> int:
    reset_dir(RUNS)
    reset_dir(OUTPUTS)
    reset_dir(RAW)
    prepare_homes()
    seed_results = verify_seeds()

    results: dict[str, dict[str, dict[str, str | int]]] = {}
    for project in PROJECTS:
        results[project.key] = {}
        for mode, _label in MODES:
            results[project.key][mode] = run_codex(project, mode)

    generate_markdown(seed_results, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
