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


def tree_entries(root: Path) -> dict[str, tuple[object, ...]]:
    if not root.is_dir():
        return {".": ("missing",)}
    entries: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            entries[rel] = ("directory",)
        else:
            entries[rel] = (
                "file",
                stat.S_IMODE(path.stat().st_mode),
                path.read_bytes(),
            )
    return entries


def drifted_paths() -> list[Path]:
    expected = SNIPPET.read_bytes()
    drift = [path for path in SURFACES if not path.is_file() or path.read_bytes() != expected]
    if tree_entries(PLUGIN_SKILL) != tree_entries(CANONICAL_SKILL):
        drift.append(PLUGIN_SKILL)
    return drift


def replace_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
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
