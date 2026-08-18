#!/usr/bin/env bash
set -euo pipefail

RAW_BASE="${SIMPLE_MAN_RAW_BASE:-https://raw.githubusercontent.com/Maksim-Burtsev/simple-man/v0.3.1}"
BEGIN_MARKER="<!-- simple-man-always-on-begin -->"
END_MARKER="<!-- simple-man-always-on-end -->"

fail() {
  echo "simple-man: $*" >&2
  exit 1
}

require_absolute_path() {
  local name="$1"
  local value="$2"
  [ -n "$value" ] || fail "$name must not be empty"
  case "$value" in
    /*) ;;
    *) fail "$name must be an absolute path" ;;
  esac
  case "$value" in
    *$'\n'*|*$'\r'*) fail "$name must not contain newlines" ;;
  esac
}

[ -n "${HOME:-}" ] || fail "HOME must not be empty"
require_absolute_path HOME "$HOME"

codex_home_is_explicit=0
if [ "${CODEX_HOME+x}" = x ]; then
  codex_home_is_explicit=1
  require_absolute_path CODEX_HOME "$CODEX_HOME"
  CODEX_HOME_PATH="${CODEX_HOME%/}"
else
  CODEX_HOME_PATH="${HOME%/}/.codex"
fi

legacy_skill_dir="${HOME%/}/.codex/skills/simple-man"
if [ "${SIMPLE_MAN_SKILL_ROOT+x}" = x ]; then
  require_absolute_path SIMPLE_MAN_SKILL_ROOT "$SIMPLE_MAN_SKILL_ROOT"
  SKILL_ROOT="${SIMPLE_MAN_SKILL_ROOT%/}"
elif [ "$codex_home_is_explicit" -eq 1 ]; then
  SKILL_ROOT="$CODEX_HOME_PATH/skills"
elif [ -e "$legacy_skill_dir" ] || [ -L "$legacy_skill_dir" ]; then
  SKILL_ROOT="${HOME%/}/.codex/skills"
else
  SKILL_ROOT="${HOME%/}/.agents/skills"
fi

SKILL_DIR="$SKILL_ROOT/simple-man"
LEGACY_BACKUP_DIR="$SKILL_DIR.backup"
AGENTS_FILE="$CODEX_HOME_PATH/AGENTS.md"

if [ -L "$SKILL_DIR" ]; then
  fail "$SKILL_DIR is a symlink; symlinked skill targets require explicit manual update"
fi

if [ -e "$LEGACY_BACKUP_DIR/SKILL.md" ] || [ -L "$LEGACY_BACKUP_DIR/SKILL.md" ]; then
  fail "$LEGACY_BACKUP_DIR contains SKILL.md; move or rename that discoverable backup before installing"
fi

tmpdir="$(mktemp -d)"
skill_stage=""
agents_stage=""
agents_backup=""
old_skill_holder=""
skill_root_anchor=""
agents_parent_anchor=""
rollback_needed=0
new_skill_installed=0
old_skill_moved=0
agents_write_started=0
agents_existed=0

nearest_existing_dir() {
  local path="$1"
  while [ ! -d "$path" ]; do
    local parent
    parent="$(dirname "$path")"
    [ "$parent" != "$path" ] || break
    path="$parent"
  done
  printf '%s\n' "$path"
}

normalize_absolute_path() {
  local path="$1"
  local part
  local result=""
  local -a parts=()
  local -a stack=()
  local IFS='/'
  read -r -a parts <<< "$path"
  for part in "${parts[@]}"; do
    case "$part" in
      ""|.) ;;
      ..)
        if [ "${#stack[@]}" -gt 0 ]; then
          unset 'stack[${#stack[@]}-1]'
        fi
        ;;
      *) stack+=("$part") ;;
    esac
  done
  for part in "${stack[@]}"; do
    result="$result/$part"
  done
  printf '%s\n' "${result:-/}"
}

physical_path() {
  local path
  local probe
  local suffix=""
  local base
  path="$(normalize_absolute_path "$1")"
  probe="$path"
  while [ ! -e "$probe" ] && [ ! -L "$probe" ] && [ "$probe" != "/" ]; do
    suffix="/$(basename "$probe")$suffix"
    probe="$(dirname "$probe")"
  done
  if [ -d "$probe" ]; then
    base="$(cd "$probe" && pwd -P)"
  else
    base="$(cd "$(dirname "$probe")" && pwd -P)/$(basename "$probe")"
  fi
  normalize_absolute_path "$base$suffix"
}

paths_overlap() {
  local left="$1"
  local right="$2"
  [ "$left" = "/" ] || [ "$right" = "/" ] ||
    [ "$left" = "$right" ] ||
    [ "${left#"$right"/}" != "$left" ] ||
    [ "${right#"$left"/}" != "$right" ]
}

file_mode() {
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

prune_to_anchor() {
  local path="$1"
  local anchor="$2"
  while [ -n "$path" ] && [ "$path" != "$anchor" ]; do
    rmdir "$path" 2>/dev/null || break
    path="$(dirname "$path")"
  done
}

rollback() {
  local rc=0
  set +e

  if [ "$agents_write_started" -eq 1 ]; then
    if [ "$agents_existed" -eq 1 ]; then
      if [ -n "$agents_backup" ] && [ -e "$agents_backup" ]; then
        mv -f "$agents_backup" "$AGENTS_WRITE_TARGET" || rc=1
      else
        rc=1
      fi
    else
      rm -f "$AGENTS_WRITE_TARGET" || rc=1
    fi
  fi

  if [ "$new_skill_installed" -eq 1 ] && { [ -e "$SKILL_DIR" ] || [ -L "$SKILL_DIR" ]; }; then
    rm -rf "$SKILL_DIR" || rc=1
  fi
  if [ "$old_skill_moved" -eq 1 ] && { [ -e "$old_skill_holder" ] || [ -L "$old_skill_holder" ]; }; then
    mv "$old_skill_holder" "$SKILL_DIR" || rc=1
  fi
  set -e
  return "$rc"
}

cleanup() {
  local status=$?
  local rollback_ok=1
  trap - EXIT

  if [ "$status" -ne 0 ] && [ "$rollback_needed" -eq 1 ]; then
    if ! rollback; then
      rollback_ok=0
      status=1
      echo "simple-man: rollback failed; transaction files were retained" >&2
    fi
  fi

  rm -rf "$tmpdir"
  if [ "$rollback_ok" -eq 1 ]; then
    [ -z "$skill_stage" ] || rm -rf "$skill_stage"
    [ -z "$agents_stage" ] || rm -f "$agents_stage"
    [ -z "$agents_backup" ] || rm -f "$agents_backup"
    [ -z "$old_skill_holder" ] || rm -rf "$old_skill_holder"
  fi

  if [ "$status" -ne 0 ]; then
    [ -z "$skill_root_anchor" ] || prune_to_anchor "$SKILL_ROOT" "$skill_root_anchor"
    [ -z "$agents_parent_anchor" ] || prune_to_anchor "$AGENTS_PARENT" "$agents_parent_anchor"
  fi
  exit "$status"
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
  command -v curl >/dev/null 2>&1 || fail "curl is required for remote install"
  curl -fsSL "$RAW_BASE/$path" -o "$target"
}

skill_tmp="$tmpdir/simple-man"
mkdir -p "$skill_tmp/agents"
fetch "skills/simple-man/SKILL.md" "$skill_tmp/SKILL.md"
fetch "skills/simple-man/agents/openai.yaml" "$skill_tmp/agents/openai.yaml"
fetch "AGENTS.md.snippet" "$tmpdir/AGENTS.md.snippet"

[ -s "$skill_tmp/SKILL.md" ] || fail "downloaded SKILL.md is empty"
[ -s "$skill_tmp/agents/openai.yaml" ] || fail "downloaded openai.yaml is empty"
[ -s "$tmpdir/AGENTS.md.snippet" ] || fail "downloaded AGENTS.md.snippet is empty"
[ "$(sed -n '1p' "$skill_tmp/SKILL.md")" = "---" ] || fail "downloaded SKILL.md has invalid frontmatter"
grep -qx 'name: simple-man' "$skill_tmp/SKILL.md" || fail "downloaded SKILL.md has an unexpected name"
grep -q '^description: .' "$skill_tmp/SKILL.md" || fail "downloaded SKILL.md has no description"
grep -qx 'interface:' "$skill_tmp/agents/openai.yaml" || fail "downloaded openai.yaml has no interface"
grep -q '^  display_name: .' "$skill_tmp/agents/openai.yaml" || fail "downloaded openai.yaml has no display name"
grep -qx 'policy:' "$skill_tmp/agents/openai.yaml" || fail "downloaded openai.yaml has no policy"
if grep -Fqx "$BEGIN_MARKER" "$tmpdir/AGENTS.md.snippet" || grep -Fqx "$END_MARKER" "$tmpdir/AGENTS.md.snippet"; then
  fail "downloaded AGENTS.md.snippet contains a managed marker"
fi

AGENTS_WRITE_TARGET="$AGENTS_FILE"
if [ -L "$AGENTS_FILE" ]; then
  [ -e "$AGENTS_FILE" ] || fail "AGENTS.md symlink is broken; no changes made"
  link_target="$(readlink "$AGENTS_FILE")"
  case "$link_target" in
    /*) AGENTS_WRITE_TARGET="$link_target" ;;
    *) AGENTS_WRITE_TARGET="$(dirname "$AGENTS_FILE")/$link_target" ;;
  esac
  [ ! -L "$AGENTS_WRITE_TARGET" ] || fail "nested AGENTS.md symlinks are unsupported; no changes made"
  [ -f "$AGENTS_WRITE_TARGET" ] || fail "AGENTS.md symlink target is not a regular file; no changes made"
  AGENTS_WRITE_TARGET="$(cd "$(dirname "$AGENTS_WRITE_TARGET")" && pwd -P)/$(basename "$AGENTS_WRITE_TARGET")"
elif [ -e "$AGENTS_FILE" ]; then
  [ -f "$AGENTS_FILE" ] || fail "AGENTS.md is not a regular file; no changes made"
fi

skill_lexical="$(normalize_absolute_path "$SKILL_DIR")"
agents_lexical="$(normalize_absolute_path "$AGENTS_WRITE_TARGET")"
skill_physical="$(physical_path "$SKILL_DIR")"
agents_physical="$(physical_path "$AGENTS_WRITE_TARGET")"
if paths_overlap "$skill_lexical" "$agents_lexical" || paths_overlap "$skill_physical" "$agents_physical"; then
  fail "skill destination $SKILL_DIR and AGENTS destination $AGENTS_WRITE_TARGET overlap; choose disjoint roots"
fi

existing="$tmpdir/AGENTS.existing.md"
without_block="$tmpdir/AGENTS.without-simple-man.md"
if [ -f "$AGENTS_FILE" ]; then
  cp "$AGENTS_FILE" "$existing"
  agents_existed=1
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
  fail "malformed managed block in $AGENTS_FILE; no changes made"
fi

block="$tmpdir/simple-man-block.md"
{
  printf '%s\n' "$BEGIN_MARKER"
  printf '## Global Communication Default\n\n'
  printf 'Simple Man skill: `%s`.\n\n' "$SKILL_DIR"
  cat "$tmpdir/AGENTS.md.snippet"
  printf '\n%s\n' "$END_MARKER"
} > "$block"

awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == begin {skip = 1; next}
  $0 == end {skip = 0; next}
  !skip {print}
' "$existing" > "$without_block"

skill_root_anchor="$(nearest_existing_dir "$SKILL_ROOT")"
AGENTS_PARENT="$(dirname "$AGENTS_WRITE_TARGET")"
agents_parent_anchor="$(nearest_existing_dir "$AGENTS_PARENT")"
mkdir -p "$SKILL_ROOT" "$AGENTS_PARENT"

agents_stage="$(mktemp "$AGENTS_PARENT/.AGENTS.md.simple-man.XXXXXX")"
{
  sed -e '${/^$/d;}' "$without_block"
  if [ -s "$without_block" ]; then
    printf '\n\n'
  fi
  cat "$block"
} > "$agents_stage"

if ! awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == begin {begins += 1; inside += 1}
  $0 == end {ends += 1; inside -= 1}
  inside < 0 {exit 1}
  END {
    if (begins != 1 || ends != 1 || inside != 0) exit 1
  }
' "$agents_stage"; then
  fail "rendered AGENTS.md managed block is invalid"
fi

if [ "$agents_existed" -eq 1 ]; then
  agents_mode="$(file_mode "$AGENTS_WRITE_TARGET")"
  chmod "$agents_mode" "$agents_stage"
  agents_backup="$(mktemp "$AGENTS_PARENT/.AGENTS.md.simple-man.previous.XXXXXX")"
  cp -p "$AGENTS_WRITE_TARGET" "$agents_backup"
else
  chmod 0644 "$agents_stage"
fi

skill_stage="$(mktemp -d "$SKILL_ROOT/.simple-man.install.XXXXXX")"
cp -R "$skill_tmp/." "$skill_stage/"
cmp "$skill_tmp/SKILL.md" "$skill_stage/SKILL.md" >/dev/null
cmp "$skill_tmp/agents/openai.yaml" "$skill_stage/agents/openai.yaml" >/dev/null

if [ -e "$SKILL_DIR" ] || [ -L "$SKILL_DIR" ]; then
  old_skill_holder="$(mktemp -d "$SKILL_ROOT/.simple-man.previous.XXXXXX")"
  rmdir "$old_skill_holder"
fi
rollback_needed=1
if [ -n "$old_skill_holder" ]; then
  mv "$SKILL_DIR" "$old_skill_holder"
  old_skill_moved=1
fi
mv "$skill_stage" "$SKILL_DIR"
skill_stage=""
new_skill_installed=1

agents_write_started=1
mv "$agents_stage" "$AGENTS_WRITE_TARGET"
agents_stage=""

rollback_needed=0
rm -rf "$old_skill_holder"
old_skill_holder=""
rm -f "$agents_backup"
agents_backup=""

echo "Simple Man installed."
echo "Skill: $SKILL_DIR"
echo "Global instructions: $AGENTS_FILE"
echo "Important: Simple Man is always-on after install. This is expected."
