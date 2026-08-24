#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_PATH="${SUBCTL_CONFIG:-/etc/subctl/config.yaml}"
USERS_PATH="${SUBCTL_USERS:-/var/lib/subctl/registry/users.yaml}"
STATE_DIR="${SUBCTL_STATE_DIR:-/var/lib/subctl}"
OUTPUT_DIR="${SUBCTL_OUTPUT_DIR:-/var/lib/subctl/public}"
SUBCTL_BIN="${SUBCTL_BIN:-subctl}"
PYTHON_BIN="${PYTHON:-python3}"
NOTE_DIR="${SUBCTL_NOTE_DIR:-${STATE_DIR}/deployment-notes}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() { printf 'subscription smoke: error: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
file_mode() { stat -c '%a' "$1"; }

require_command "$PYTHON_BIN"
require_command stat
require_command find
require_command "$SUBCTL_BIN"

[[ -f "$CONFIG_PATH" ]] || fail "config file is missing"
[[ -f "$USERS_PATH" ]] || fail "users file is missing"
for path in "$CONFIG_PATH" "$USERS_PATH"; do
  mode="$(file_mode "$path")"
  (( 8#$mode & 7 )) && fail "protected file is readable by other users"
done

mkdir -p "$NOTE_DIR"
chmod 700 "$NOTE_DIR"
NOTE_PATH="$NOTE_DIR/subscription-smoke-$(date -u +%Y%m%dT%H%M%SZ).md"
umask 077
{
  printf '# Subscription smoke check\n\n'
  printf -- '- started_utc: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf -- '- config_mode: %s\n- users_mode: %s\n' "$(file_mode "$CONFIG_PATH")" "$(file_mode "$USERS_PATH")"
  printf -- '- secret_values_recorded: false\n\n'
} >"$NOTE_PATH"

"$SUBCTL_BIN" --config "$CONFIG_PATH" --users "$USERS_PATH" --state-dir "$STATE_DIR" --output-dir "$OUTPUT_DIR" refresh-provider >/dev/null
"$SUBCTL_BIN" --config "$CONFIG_PATH" --users "$USERS_PATH" --state-dir "$STATE_DIR" --output-dir "$OUTPUT_DIR" render --all >/dev/null

mapfile -t yaml_files < <(find "$OUTPUT_DIR/s" -maxdepth 1 -type f -name '*.yaml' | sort)
mapfile -t raw_files < <(find "$OUTPUT_DIR/s" -maxdepth 1 -type f -name '*.raw' | sort)
(( ${#yaml_files[@]} > 0 )) || fail "no YAML files were generated"
(( ${#raw_files[@]} > 0 )) || fail "no raw files were generated"

printf -- '- generated_yaml_count: %s\n- generated_raw_count: %s\n' "${#yaml_files[@]}" "${#raw_files[@]}" >>"$NOTE_PATH"
for yaml_file in "${yaml_files[@]}"; do
  "$PYTHON_BIN" - "$yaml_file" <<'PY'
import sys
import yaml
yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
PY
done
for raw_file in "${raw_files[@]}"; do
  "$PYTHON_BIN" - "$raw_file" <<'PY'
import base64
import sys
base64.b64decode(open(sys.argv[1], "rb").read(), validate=True)
PY
done

printf -- '- finished_utc: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$NOTE_PATH"
chmod 600 "$NOTE_PATH"
printf 'subscription smoke: passed (%s YAML, %s raw)\n' "${#yaml_files[@]}" "${#raw_files[@]}"
