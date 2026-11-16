#!/usr/bin/env bash
# Update the fork's working branch.
#
# Brings the fork's main level with upstream (fast-forward only), then merges
# upstream main and every feat/* branch into ian and runs the test suite.
# ian is only ever merged into, never rewritten, so every clone of it can
# fast-forward. Nothing is pushed unless every merge and the suite succeed, and
# ian is never pushed directly: publish-sync.sh opens a PR into it.
#
# Usage: scripts/fork/update-ian.sh [--no-test] [--no-push] [--bundle FILE]
#   --bundle FILE  write the result to a git bundle instead of pushing (the
#                  weekly workflow tests without write access, then publishes
#                  the bundle from a separate job).
# Remotes: FORK_REMOTE (default origin), UPSTREAM_REMOTE (default upstream).
set -euo pipefail

origin=${FORK_REMOTE:-origin}
upstream=${UPSTREAM_REMOTE:-upstream}
upstream_url=https://github.com/NateBJones-Projects/ringer.git
run_tests=1
push=1
bundle=
while [ $# -gt 0 ]; do
  case "$1" in
    --no-test) run_tests=0 ;;
    --no-push) push=0 ;;
    --bundle) bundle=${2:?--bundle needs a file}; push=0; shift ;;
    *) echo "usage: $0 [--no-test] [--no-push] [--bundle FILE]" >&2; exit 2 ;;
  esac
  shift
done

cd "$(git rev-parse --show-toplevel)"
script_dir=$(pwd)/scripts/fork
case "$bundle" in "" | /*) ;; *) bundle=$(pwd)/$bundle ;; esac
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

upstream_sha=$(git rev-parse "refs/remotes/$upstream/main")
if [ -n "$bundle" ]; then
  git update-ref refs/fork-sync/ian HEAD
  git update-ref refs/fork-sync/main "$upstream_sha"
  git bundle create --quiet "$bundle" refs/fork-sync/ian refs/fork-sync/main
  git update-ref -d refs/fork-sync/ian
  git update-ref -d refs/fork-sync/main
fi
if [ "$push" = 1 ]; then
  FORK_REMOTE=$origin "$script_dir/publish-sync.sh" HEAD "$upstream_sha"
fi
echo "update-ian: merged result is $(git rev-parse --short HEAD)"
