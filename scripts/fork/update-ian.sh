#!/usr/bin/env bash
# Update the fork's working branch.
#
# Brings the fork's main level with upstream (fast-forward only), then merges
# upstream main and every feat/* branch into ian and runs the test suite.
# ian is only ever merged into, never rewritten, so every clone of it can
# fast-forward. Nothing is pushed unless every merge and the suite succeed.
#
# Usage: scripts/fork/update-ian.sh [--no-test] [--no-push]
# Remotes: FORK_REMOTE (default origin), UPSTREAM_REMOTE (default upstream).
set -euo pipefail

origin=${FORK_REMOTE:-origin}
upstream=${UPSTREAM_REMOTE:-upstream}
upstream_url=https://github.com/NateBJones-Projects/ringer.git
run_tests=1
push=1
for arg in "$@"; do
  case "$arg" in
    --no-test) run_tests=0 ;;
    --no-push) push=0 ;;
    *) echo "usage: $0 [--no-test] [--no-push]" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
git remote get-url "$upstream" >/dev/null 2>&1 || git remote add "$upstream" "$upstream_url"
git fetch --quiet "$upstream" main
git fetch --quiet --prune "$origin"

# main is a mirror: refuse to continue if it has commits upstream lacks.
if git rev-parse --verify --quiet "refs/remotes/$origin/main" >/dev/null &&
   ! git merge-base --is-ancestor "$origin/main" "$upstream/main"; then
  echo "update-ian: $origin/main has commits that upstream lacks; main must stay a mirror" >&2
  exit 1
fi

work=$(mktemp -d)
trap 'git worktree remove --force "$work" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
if git rev-parse --verify --quiet "refs/remotes/$origin/ian" >/dev/null; then
  git worktree add --quiet --detach "$work" "$origin/ian"
else
  git worktree add --quiet --detach "$work" "$upstream/main"
fi
cd "$work"

git merge --quiet --no-edit "$upstream/main"
for branch in $(git for-each-ref --format='%(refname:short)' "refs/remotes/$origin/feat/"); do
  if ! git merge --quiet --no-edit "$branch"; then
    git merge --abort
    echo "update-ian: merging $branch conflicts; merge it into a local ian checkout by hand" >&2
    exit 1
  fi
done

if [ "$run_tests" = 1 ]; then
  RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests
fi

if [ "$push" = 1 ]; then
  git push --quiet "$origin" "refs/remotes/$upstream/main:refs/heads/main"
  git push --quiet "$origin" "HEAD:refs/heads/ian"
fi
echo "update-ian: ian is at $(git rev-parse --short HEAD)"
