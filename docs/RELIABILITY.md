# Reliability and outcome evidence

Ringer records task lifecycle evidence before dispatch and classifies failures before deciding whether to retry. A passing executable check remains a product-check result. It does not establish delivery, human acceptance or permission to promote an artifact.

## Manifest fields

Existing manifests retain a 60-second checker timeout and two maximum attempts. The default retry policy now retries product or missing-deliverable failures only. The worker engine and model defaults are unchanged.

| Field | Default | Meaning |
| --- | --- | --- |
| Run `job_id` | `run_name` | Stable identity across rounds. Each invocation also gets a unique `run_id`. |
| Task `check_timeout_s` | `60` | Independent finite positive checker timeout in seconds. Fractions and values above 60 are supported. |
| Task `preflight_timeout_s` | `15` | Combined time budget for local input checks and explicit preflight commands. |
| Task `preflight_command` | Absent | Shell command that must exit zero before a worker starts. Its output is retained. |
| Task `preflight_files` | `[]` | Files that must exist and be readable before dispatch. Relative paths use the actual task directory. |
| Task `preflight_write_paths` | `[]` | Existing directories a worker will edit. Ringer resolves symlinks, checks the directory, probes a temporary create/delete, and checks restricted Codex roots against the resolved command. |
| Task `preflight_python` | Absent | Explicit interpreter executable, used in the actual task directory. |
| Task `preflight_python_modules` | `[]` | Modules imported by that interpreter. Requires `preflight_python`. Imports execute module initialisers. |
| Task `source_sha256` | `{}` | Requested source paths mapped to exact SHA-256 digests. Mismatches block dispatch. |
| Task `retry_policy` | `"product"` | `"legacy"` explicitly restores retries for FAIL/TIMEOUT verdicts, subject to the hard exclusions below. |
| Task `retry_classes` | `[]` | Additional permitted failure classes, bounded by `max_attempts`. |

Example, using an already configured engine:

```json
{
  "run_name": "asset-review",
  "job_id": "asset-review",
  "workdir": "/tmp/asset-review",
  "max_parallel": 1,
  "tasks": [{
    "key": "review",
    "engine": "opencode",
    "task_type": "research",
    "spec": "Read the declared source and write report.md in the current task directory. Own only report.md. Explain the source evidence and uncertainty. The coordinator reviews the exported report and executed check before use.",
    "expect_files": ["report.md"],
    "preflight_files": ["/absolute/path/source.txt", "/absolute/path/check_report.py"],
    "preflight_write_paths": ["/absolute/path/source-directory"],
    "preflight_python": "/absolute/path/python3",
    "preflight_python_modules": ["json"],
    "preflight_timeout_s": 10,
    "check": "/absolute/path/python3 /absolute/path/check_report.py report.md",
    "check_timeout_s": 120,
    "max_attempts": 2,
    "verified": "The executable checker validates the report content against the declared source."
  }]
}
```

Replace the example paths with real inputs before use. To pin the exact source, add `"source_sha256": {"/absolute/path/source.txt": "<64 hex digits from the requested file>"}`. The placeholder is deliberately invalid. Obtain the digest locally with `shasum -a 256 /absolute/path/source.txt`.

Preflight runs once per task before any model attempt, not once per retry. Local file and hash checks run after the declared commands so those commands cannot substitute different source bytes after verification. It checks the declared engine executable and access gate, declared files/modules/source bytes, obvious missing interpreter scripts in the check, declared edit directories, and write capability at existing output parent directories. It does not guess arbitrary shell dependencies. A script declared in `expect_files` may be created by the worker. Checks may create output directories and expected exports; these need not exist during preflight. For generated checker scripts, declare them as expected outputs. File validation, hashing and write probes run in a local helper subprocess so the same deadline also bounds stalled filesystem access.

For a restricted Codex source-edit task, declare source files and hashes with `preflight_files` and `source_sha256`, then declare every existing edit directory with `preflight_write_paths`. A path outside the task directory must appear in the effective Codex argv through task `engine_args`, for example `["-c", "sandbox_workspace_write.writable_roots=[\"/absolute/source dir\"]"]`. The old top-level task field `writable_roots` is rejected because the engine never forwarded it. Inspect `run --dry-run` before launch. The worker's first action should also create and remove a temporary probe inside each edit root before it changes source.

The host create/delete probe proves host access only. For restricted Codex workers, Ringer separately checks that each resolved real path sits inside the task directory or an explicit `sandbox_workspace_write.writable_roots` value in the resolved command. It does not execute a Codex sandbox during preflight, and it cannot prevent a directory or symlink changing after the check.

Live work needs a read-only runtime contract before activation. Record the actual release paths, configuration path, service identity and operating mode, then run imports as the actual service user. This control does not replace an existing rollout's service-user import checks.

A preflight or setup failure produces `blocked`, unknown check evidence and zero model attempts. It never retries or asks the worker to repair a known missing input.

## Failure and retry policy

`failure_class` is one of `product`, `checker_timeout`, `worker_timeout`, `missing_dependency`, `missing_checker`, `export`, `provider/runtime`, `quota`, `permission`, `interrupted` or `unknown`. It is null for a passing attempt.

Only `product` automatically retries by default. This includes missing or empty expected deliverables after the check. A timeout does not imply that the artifact needs rewriting. Missing imports/checkers and explicit provider diagnostics are distinct from a failing product check. Unexplained worker failures stay `unknown`.

For a task that deliberately needs the former policy, use `"retry_policy": "legacy"`. For a narrower exception, use `"retry_classes": ["checker_timeout"]` with a bounded `max_attempts`. Quota, permission and interruption are never retried, even in legacy mode; listing them in `retry_classes` is invalid. Retried prompts retain the actual failure output. No policy changes the configured model, sandbox, access permission or engine.

## Journals and state

`<state_dir>/lifecycle.jsonl` is append-only and fsynced. Events are `queued`, `attempt_started`, `attempt_finished` and `terminal`. Every event contains `run_id`, `job_id`, `task_key` and `attempt_index` (zero before a model attempt). Its deterministic ID hashes those fields and the event name. Replaying the same ID preserves the original row. A torn tail is retained and reported as malformed; subsequent rows remain readable.

An `attempt_started` event is persisted before the worker invocation. It proves intent to spawn, not that the provider accepted or billed a request. Cancellation during a worker or checker retains an `INTERRUPTED` attempt row plus terminal lifecycle evidence. Queued cancellation records `not_started` without inventing a model attempt. A crash or SIGKILL may leave an open attempt; reconciliation labels that evidence partial instead of inventing a result.

Normal eval JSONL rows retain their old fields and add identity and evidence fields. The legacy Postgres insert column contract is unchanged; lifecycle rows are still local. `outcomes` reads local JSONL, not remote Postgres, and shows journal gaps rather than assuming a remote write exists. Worker stdin remains closed, sandbox arguments remain explicit, and raw worker and checker output goes to the task log. Check subprocess groups are terminated on timeout or cancellation, including descendants that keep stdout open.

Task state and attempt/lifecycle records include:

- `task_contract_state`: PASS or NEEDS_CHANGE for declared non-empty `expect_files`, UNKNOWN when no file contract was declared.
- `product_state`: PASS only for check exit zero, NEEDS_CHANGE for a failing check, UNKNOWN when interrupted, timed out or not executed. The check's actual strength remains the manifest author's responsibility.
- `export_state`: PASS only after harvested bytes match source size and SHA-256, NEEDS_CHANGE on harvest errors, UNKNOWN without export evidence.
- `promotion_state`: always BLOCKED. The separate human review receipt gate remains untouched.
- `failure_class`, `attempt_index`, `check_sha256`, `preflight_state`, requested `source_sha256`, and the harvested file manifest.

`check_sha256` identifies the check command text. Pin checker/source file bytes with `source_sha256` when required. This does not automatically hash every transitive dependency of a shell command.

## Export and read-back

Declared files are copied even if Git ignores them. An externally exported ignored file stays in its original location and gets a verified artifact-store copy. Copies have `bytes`, `sha256`, `source_path` and `source_sha256`; a `harvest-manifest.json` sits alongside them. Equal basenames are disambiguated. Oversized files (above the existing 20 MB limit), missing files, copy failures and mismatched bytes are reported as export errors. A worktree is retained if harvesting fails.

Fallback harvesting of undeclared top-level files remains for compatibility. It does not change UNKNOWN task-contract evidence into PASS. A patch file does not establish the presence of ignored outputs: explicitly export and declare those files too.

```sh
RINGER_NO_SELF_UPDATE=1 python3 ringer.py outcomes --json
RINGER_NO_SELF_UPDATE=1 python3 ringer.py outcomes --job-id asset-review --read-back --json
RINGER_NO_SELF_UPDATE=1 python3 ringer.py outcomes --state-dir /path/to/state --log /path/to/runs.jsonl --json
```

`outcomes` unions run-state tasks, attempt rows and lifecycle events. It reports every observed task, including historical state-only tasks, partial retries, absent model attribution, unknown verification and malformed rows. It never edits the historical inputs. `--read-back` rehashes the recorded exported paths; missing or changed files return `matches: false`. This reads artifact bytes, not runtime acceptance or human approval.

`success_coverage` uses all observed tasks as the denominator and requires a recorded passing check, task contract and export. Historical records that lack those fields remain unknown, even when their old verdict says PASS. `passed_checks`, `unknown_contract`, `unknown_export`, `unknown_verification`, failure-class counts and model attribution explain the gap. `models` remains the legacy attempt scoreboard; use `outcomes` for lifecycle accounting.

## Regression checks

All reliability workers are deterministic local shell processes. No tests call models.

```sh
RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests -p 'test_reliability.py'
RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests -p 'test_ringer.py'
RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests -p 'test_model_log.py'
RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests -p 'test_lint.py'
RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests -p 'test_verify_order.py'
RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests
```

The focused suite exercises both original regressions, real subprocess cancellation and stdout, finite short timeouts, a configured timeout above 60 without waiting, retry context, preflight source/module/access failures, the plausible-source trap, export errors, ignored exports, historical omissions and the blocked promotion boundary. The coordinator reviews the diff and test evidence; the user reviews release evidence. These checks do not issue a review receipt.
