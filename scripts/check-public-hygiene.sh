#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'public hygiene: error: %s\n' "$*" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || fail "git is required"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "run from a Git worktree"
cd "$repo_root"

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

python3 -m pytest -q tests/test_repository_neutrality.py
printf 'public hygiene: passed\n'
