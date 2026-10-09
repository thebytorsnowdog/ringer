# The thebytorsnowdog/ringer fork

This fork carries a few additions on top of
[NateBJones-Projects/ringer](https://github.com/NateBJones-Projects/ringer)
and stays level with it.

## Branches

| Branch | What it is | Edit it? |
| --- | --- | --- |
| `main` | Mirror of upstream `main`, fast-forward only | Never |
| `feat/*` | One addition each, started from `main` (or from the branch it needs) | Yes: this is where changes go |
| `ian` | Upstream `main` merged with every `feat/*` branch; the default branch and what you run | No hand edits; it is only merged into |

`ian` is never rewritten, so every clone of it fast-forwards.

| Topic branch | Adds | Upstream |
| --- | --- | --- |
| `feat/fork-setup` | This guide, the README notes, `scripts/fork/update-ian.sh`, the weekly workflow | Fork only |
| `feat/opencode-writable-root` | Repeatable `--writable-root PATH` for the sandboxed OpenCode wrapper; `repo-feature` uses it | Candidate PR |
| `feat/glm-5.3-flash` | GLM 5.3 Flash capability card and model identity | Candidate PR |
| `feat/openrouter-models` | Model identities for DeepSeek V4.1 Flash and MiMo V2.5 (on probation; see docs/MODEL-ROUTING.md) | Candidate PR |
| `feat/codex-integration` | `install-agent --codex`, a Codex-host skill, Codex tool names in the nudge hook | Candidate PR |
| `feat/cost-engine` | Cost accounting, optional billing admission, reliability evidence, model-fit assessment (`ringer.py assess`, opt-in gate), `ringer.py outcomes`, benchmarks | Ask upstream first |
| `feat/discovery` | `ringer-discover.py` automation discovery companion (builds on `feat/cost-engine`) | Fork only |
| `feat/mission-fit` | `AGENTS.md` mission contract and `scripts/check_mission_fit_gate.py` | Fork only |
| `feat/pstack-ringer-defaults` | `pstack-ringer-defaults` skill, the single source that harness skill dirs symlink to | Fork only |

## Staying level with upstream

The `fork sync` workflow runs every Monday (and on demand from the Actions
tab). It runs `scripts/fork/update-ian.sh`, which:

1. fast-forwards `main` to upstream;
2. merges upstream `main` and every `feat/*` branch into `ian`;
3. runs the test suite, and pushes `main` and `ian` only if everything passed.

If a merge conflicts, the suite fails, or upstream changed a workflow file
(the Actions token cannot push those), the workflow opens a "Fork sync failed"
issue. Finish the update locally:

```sh
git switch ian && git pull --ff-only
git merge upstream/main          # resolve any conflict here, once
scripts/fork/update-ian.sh       # merges the feat/* branches, tests, pushes
```

`git config rerere.enabled true` lets Git replay a resolution it has seen.

## Making a change

Put it on the `feat/*` branch it belongs to, or start a new one from `main`.
Keep new behaviour in new modules and edits to `ringer.py` to thin hook points;
upstream changes `ringer.py` often. The next update merges it into `ian`; run
`scripts/fork/update-ian.sh` to do it now.

To offer a branch upstream, rebase it onto current `main` and open a PR from
it. Once upstream merges it, delete the branch: the change then arrives
through `main`.

## Settings kept out of the repository

Personal choices live in `~/.config/ringer/config.toml`, which Ringer reads
by default. For example:

```toml
# Require a model-fit assessment on every model-backed manifest.
require_model_assessment = true
```

Ringer's self-update only fast-forwards a checkout on `main`, so on `ian` it
reports itself blocked and changes nothing; update with `git pull --ff-only`.
