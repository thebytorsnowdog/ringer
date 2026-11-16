#!/usr/bin/env bash
# Publish a tested fork update as a pull request into ian.
#
# Fast-forwards the fork's main mirror to UPSTREAM_SHA, pushes IAN_SHA to a new
# fork-sync/* branch and opens a PR into ian. Nothing here runs repository
# code, so the weekly workflow can give only this step write access. ian itself
# is never pushed: it changes only when that PR is merged (with a merge commit,
# so upstream history and contributor credit are kept).
#
# Usage: scripts/fork/publish-sync.sh IAN_SHA UPSTREAM_SHA
# Env: FORK_REMOTE (default origin), GH_REPO (default: the fork remote's repo),
#      FORK_SYNC_DISPATCH_TESTS=1 to start the tests workflow on the branch
#      (needed when the PR is opened with the Actions token, which does not
#      trigger pull_request workflows itself).
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 IAN_SHA UPSTREAM_SHA" >&2
  exit 2
fi
ian_sha=$(git rev-parse --verify "$1^{commit}")
upstream_sha=$(git rev-parse --verify "$2^{commit}")
origin=${FORK_REMOTE:-origin}

git fetch --quiet "$origin" main ian

if [ "$(git rev-parse "$origin/main")" != "$upstream_sha" ]; then
  if ! git merge-base --is-ancestor "$origin/main" "$upstream_sha"; then
    echo "publish-sync: $origin/main has commits that upstream lacks; main must stay a mirror" >&2
    exit 1
  fi
  git push --quiet "$origin" "$upstream_sha:refs/heads/main"
fi

if git merge-base --is-ancestor "$ian_sha" "$origin/ian"; then
  echo "publish-sync: ian already contains $(git rev-parse --short "$ian_sha"); nothing to propose"
  exit 0
fi

short=$(git rev-parse --short "$ian_sha")
branch="fork-sync/$(date -u +%Y-%m-%d)-$short"
git push --quiet "$origin" "$ian_sha:refs/heads/$branch"

if [ -z "${GH_REPO:-}" ]; then
  GH_REPO=$(git remote get-url "$origin" | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')
  export GH_REPO
fi

body="Weekly fork update: upstream main at $(git rev-parse --short "$upstream_sha") plus every feat/* branch, merged onto ian. The suite passed before this branch was pushed.

Merge with **Create a merge commit** (not squash), so upstream history and README contributor credit survive."
url=$(gh pr create --base ian --head "$branch" --title "Fork sync $(date -u +%Y-%m-%d) ($short)" --body "$body")
echo "publish-sync: opened $url"

number=${url##*/}
for older in $(gh pr list --base ian --state open --json number,headRefName \
    --jq ".[] | select(.headRefName | startswith(\"fork-sync/\")) | select(.number != $number) | .number"); do
  gh pr close "$older" --comment "Superseded by #$number."
done

if [ "${FORK_SYNC_DISPATCH_TESTS:-0}" = 1 ]; then
  gh workflow run tests.yml --ref "$branch"
fi
