#!/usr/bin/env bash
set -euo pipefail

RAW_BASE="${SIMPLE_MAN_RAW_BASE:-https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/v0.2.0}"
CODEX_HOME="${CODEX_HOME:-"$HOME/.codex"}"
SKILL_DIR="$CODEX_HOME/skills/simple-man"
SKILL_BACKUP_DIR="$SKILL_DIR.backup"
AGENTS_FILE="$CODEX_HOME/AGENTS.md"
BEGIN_MARKER="<!-- simple-man-always-on-begin -->"
END_MARKER="<!-- simple-man-always-on-end -->"

tmpdir="$(mktemp -d)"
skill_stage=""
agents_stage=""
cleanup() {
  rm -rf "$tmpdir"
  if [ -n "$skill_stage" ] && [ -e "$skill_stage" ]; then
    rm -rf "$skill_stage"
  fi
  if [ -n "$agents_stage" ] && [ -e "$agents_stage" ]; then
    rm -f "$agents_stage"
  fi
}
trap cleanup EXIT

script_dir=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

fetch() {
  local path="$1"
  local target="$2"
  if [ -n "$script_dir" ] && [ -f "$script_dir/$path" ]; then
    cp "$script_dir/$path" "$target"
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "simple-man: curl is required for remote install" >&2
    exit 1
  fi
  curl -fsSL "$RAW_BASE/$path" -o "$target"
}

skill_tmp="$tmpdir/simple-man"
mkdir -p "$skill_tmp/agents"
fetch "skills/simple-man/SKILL.md" "$skill_tmp/SKILL.md"
fetch "skills/simple-man/agents/openai.yaml" "$skill_tmp/agents/openai.yaml"
fetch "AGENTS.md.snippet" "$tmpdir/AGENTS.md.snippet"

mkdir -p "$CODEX_HOME/skills" "$(dirname "$AGENTS_FILE")"

existing="$tmpdir/AGENTS.existing.md"
without_block="$tmpdir/AGENTS.without-simple-man.md"
if [ -f "$AGENTS_FILE" ]; then
  cp "$AGENTS_FILE" "$existing"
else
  : > "$existing"
fi

if ! awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == begin {
    if (inside || seen) exit 1
    inside = 1
    seen = 1
    next
  }
  $0 == end {
    if (!inside) exit 1
    inside = 0
    next
  }
  END {
    if (inside) exit 1
  }
' "$existing"; then
  echo "simple-man: malformed managed block in $AGENTS_FILE; no changes made" >&2
  exit 1
fi

block="$tmpdir/simple-man-block.md"
{
  printf '%s\n' "$BEGIN_MARKER"
  printf '## Global Communication Default\n\n'
  printf 'Simple Man is installed globally at `~/.codex/skills/simple-man`.\n\n'
  cat "$tmpdir/AGENTS.md.snippet"
  printf '\n%s\n' "$END_MARKER"
} > "$block"

awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == begin {skip = 1; next}
  $0 == end {skip = 0; next}
  !skip {print}
' "$existing" > "$without_block"

agents_stage="$(mktemp "$CODEX_HOME/.AGENTS.md.simple-man.XXXXXX")"
{
  sed -e '${/^$/d;}' "$without_block"
  if [ -s "$without_block" ]; then
    printf '\n\n'
  fi
  cat "$block"
} > "$agents_stage"

skill_stage="$(mktemp -d "$CODEX_HOME/skills/.simple-man.install.XXXXXX")"
cp -R "$skill_tmp/." "$skill_stage/"

if [ -e "$SKILL_DIR" ]; then
  rm -rf "$SKILL_BACKUP_DIR"
  mv "$SKILL_DIR" "$SKILL_BACKUP_DIR"
fi

if ! mv "$skill_stage" "$SKILL_DIR"; then
  if [ -e "$SKILL_BACKUP_DIR" ]; then
    mv "$SKILL_BACKUP_DIR" "$SKILL_DIR"
  fi
  exit 1
fi
skill_stage=""
if [ -L "$AGENTS_FILE" ]; then
  cat "$agents_stage" > "$AGENTS_FILE"
  rm -f "$agents_stage"
else
  mv "$agents_stage" "$AGENTS_FILE"
fi
agents_stage=""

echo "Simple Man installed."
echo "Skill: $SKILL_DIR"
if [ -e "$SKILL_BACKUP_DIR" ]; then
  echo "Previous skill backup: $SKILL_BACKUP_DIR"
fi
echo "Global instructions: $AGENTS_FILE"
echo "Important: Simple Man is always-on after install. This is expected."
