#!/usr/bin/env python3
"""Check or regenerate Simple Man's generated distribution surfaces."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNIPPET = ROOT / "AGENTS.md.snippet"
SURFACES = tuple(ROOT / name for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"))
CANONICAL_SKILL = ROOT / "skills" / "simple-man"
PLUGIN_SKILL = ROOT / "plugins" / "simple-man" / "skills" / "simple-man"


def object_entry(path: Path) -> tuple[object, ...]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return ("missing",)
    if stat.S_ISLNK(mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISDIR(mode):
        return ("directory", stat.S_IMODE(mode))
    if stat.S_ISREG(mode):
        return ("file", stat.S_IMODE(mode), path.read_bytes())
    return ("other", stat.S_IFMT(mode), stat.S_IMODE(mode))


def tree_entries(root: Path) -> dict[str, tuple[object, ...]]:
    entries = {".": object_entry(root)}
    if entries["."][0] != "directory":
        return entries
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        entries[rel] = object_entry(path)
    return entries


def drifted_paths() -> list[Path]:
    expected = SNIPPET.read_bytes()
    drift = [
        path
        for path in SURFACES
        if object_entry(path) != ("file", 0o644, expected)
    ]
    if tree_entries(PLUGIN_SKILL) != tree_entries(CANONICAL_SKILL):
        drift.append(PLUGIN_SKILL)
    return drift


def replace_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_surfaces() -> None:
    content = SNIPPET.read_bytes()
    for surface in SURFACES:
        replace_file(surface, content)

    PLUGIN_SKILL.parent.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix=".simple-man-sync.", dir=PLUGIN_SKILL.parent))
    stage = stage_parent / "simple-man"
    try:
        shutil.copytree(CANONICAL_SKILL, stage, symlinks=True)
        if PLUGIN_SKILL.exists() or PLUGIN_SKILL.is_symlink():
            if PLUGIN_SKILL.is_dir() and not PLUGIN_SKILL.is_symlink():
                shutil.rmtree(PLUGIN_SKILL)
            else:
                PLUGIN_SKILL.unlink()
        os.replace(stage, PLUGIN_SKILL)
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="fail if generated surfaces drift")
    action.add_argument("--write", action="store_true", help="regenerate all generated surfaces")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.write:
        write_surfaces()

    drift = drifted_paths()
    if drift:
        for path in drift:
            print(f"out of sync: {path.relative_to(ROOT)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
