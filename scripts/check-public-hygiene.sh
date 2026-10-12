#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'public hygiene: error: %s\n' "$*" >&2
  exit 1
}

is_approved_profile_endpoint_reference() {
  case "$1" in
    docs/configuration.md:56:*|\
    src/subctl/render.py:43:*|\
    tests/fixtures/alice_mihomo.yaml:210:*|\
    tests/test_render.py:272:*|\
    tests/test_render.py:295:*|\
    tests/test_repository_neutrality.py:51:*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
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
  '***REMOVED***' \
  '***REMOVED***' \
  'cXVlcmlvbg==' \
  'VEFTSy0=' \
  'cGxhbi0wMDk=' \
  'dmVyaWZ5LXRhc2swMjU='; do
  needle="$(printf '%s' "$encoded" | base64 --decode)"
  matches="$(git grep -n -I -F -- "$needle" -- . || true)"
  if [[ -z "$matches" ]]; then
    continue
  fi

  case "$encoded" in
    '***REMOVED***'|'cXVlcmlvbg==')
      while IFS= read -r match; do
        [[ -z "$match" ]] && continue
        if is_approved_profile_endpoint_reference "$match"; then
          continue
        fi
        printf '%s\n' "$match"
        fail "publicly unsafe reference found"
      done <<< "$matches"
      ;;
    *)
      printf '%s\n' "$matches"
      fail "publicly unsafe reference found"
      ;;
  esac
done

echo "public hygiene: passed"
