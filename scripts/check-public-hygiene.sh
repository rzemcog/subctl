#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'public hygiene: error: %s\n' "$*" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || fail "git is required"
git rev-parse --show-toplevel >/dev/null 2>&1 || fail "run from a Git worktree"

while IFS= read -r -d '' path; do
  case "$path" in
    plan.md|new_plan.md|new_plan.md.orig|plan-*/**|docs/migration/*|*verify-task*|*cutover-rollback*|deploy/create-cutover-artifact.py)
      fail "task-specific or legacy artifact is tracked: $path"
      ;;
    *.env|*.pem|*.key|*.p12)
      fail "secret-like file is tracked: $path"
      ;;
  esac
done < <(git ls-files -z)

# Keep these values encoded so this checker does not match its own source.
for encoded in \
  'cm9vdEBydS12cHM=' \
  'c3ViLnF1ZXJpb24ub3Jn' \
  'cXVlcmlvbg==' \
  'VEFTSy0=' \
  'cGxhbi0wMDk=' \
  'dmVyaWZ5LXRhc2swMjU='; do
  needle="$(printf '%s' "$encoded" | base64 --decode)"
  if git grep -n -I -F -- "$needle" -- .; then
    fail "publicly unsafe reference found"
  fi
done

echo "public hygiene: passed"
