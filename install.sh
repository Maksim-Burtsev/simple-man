#!/usr/bin/env bash
set -euo pipefail

RAW_BASE="${SIMPLE_MAN_RAW_BASE:-https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/master}"
CODEX_HOME="${CODEX_HOME:-"$HOME/.codex"}"
SKILL_DIR="$CODEX_HOME/skills/simple-man"
AGENTS_FILE="$CODEX_HOME/AGENTS.md"
BEGIN_MARKER="<!-- simple-man-always-on-begin -->"
END_MARKER="<!-- simple-man-always-on-end -->"

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)" || script_dir=""

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
rm -rf "$SKILL_DIR"
mkdir -p "$SKILL_DIR"
cp -R "$skill_tmp/." "$SKILL_DIR/"

block="$tmpdir/simple-man-block.md"
{
  printf '%s\n' "$BEGIN_MARKER"
  printf '## Global Communication Default\n\n'
  printf 'Simple Man is installed globally at `~/.codex/skills/simple-man`.\n\n'
  cat "$tmpdir/AGENTS.md.snippet"
  printf '\n%s\n' "$END_MARKER"
} > "$block"

existing="$tmpdir/AGENTS.existing.md"
without_block="$tmpdir/AGENTS.without-simple-man.md"
if [ -f "$AGENTS_FILE" ]; then
  cp "$AGENTS_FILE" "$existing"
else
  : > "$existing"
fi

awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == begin {skip = 1; next}
  $0 == end {skip = 0; next}
  !skip {print}
' "$existing" > "$without_block"

{
  sed -e '${/^$/d;}' "$without_block"
  if [ -s "$without_block" ]; then
    printf '\n\n'
  fi
  cat "$block"
} > "$AGENTS_FILE"

echo "Simple Man installed."
echo "Skill: $SKILL_DIR"
echo "Global instructions: $AGENTS_FILE"
echo "Important: Simple Man is always-on after install. This is expected."
