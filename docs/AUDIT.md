# Audit

Read on 29 October 2026 against `ian` at `65859cf`. The scope is security, performance, and efficiency in this repository. Ian Dunsmore reviews the pull requests before anything is merged.

## Fixes

**Lifecycle journal.** `LifecycleJournal.append` in `ringer_reliability.py` re-read and parsed all of `state_dir/lifecycle.jsonl` on every event, while holding the exclusive lock. That file keeps every run. [Scan the lifecycle journal once per process](https://github.com/thebytorsnowdog/ringer/pull/4) remembers event ids already scanned and reads only new bytes. The first append in a process still reads the existing file. A replay still keeps the first payload, and a torn tail is still isolated.

On this machine, before that change, 10 appends against a synthetic journal took 5.3 ms each at 2,000 rows, 17.4 ms at 8,000 rows, and 41.7 ms at 20,000 rows. After the change, the first append still read the file. The next 10 appends took 0.67 ms, 0.15 ms, and 0.14 ms each at those sizes.

**Identity sync.** `refresh_catalog_tables` in `ringer.py` returns when the catalog size and mtime match. `refresh_identity_tables` deleted and reinserted its rows on every `sync_read_model_db` call. `PersistentHudServer` calls that sync from `/api/models`, and `dashboard/ringside.js` requests `/api/models` every 15 seconds while the models view is open. [Skip identity table rewrite when the registry file is unchanged](https://github.com/thebytorsnowdog/ringer/pull/3) stores the registry size and mtime and skips that rewrite until the file changes. A failed or empty read is remembered until the size or mtime changes, which is the same rule the catalog snapshot already uses.

Before that change, one unchanged sync of a 5,000-row synthetic log rewrote 14 identity rows, 28 insert and delete events, in 0.004 seconds.

## Left in place

**Deliverable harvest follows symlinks.** `_harvest_deliverables_on_pass` in `ringer.py` uses `stat`, `is_file`, and `shutil.copy2` on the declared path, so a symlink is copied as its target file. `source_files` in the same file refuses a directory-scan symlink that resolves outside the named tree, or onto a hidden or sensitive name. `CONTRIBUTING.md` declines worker confinement and credential deny-lists, so this stays a risk rather than a patch.

**Ringside log reads.** `hud_task_log_path` in `ringer.py` returns the `log_path` stored in the run state, and the `/logs/` handler tails that path. The runner writes the path. The server listens on `127.0.0.1`. `send_response_body` reflects CORS only for the Tauri origins. The handler does not check the Host header. `dashboard/ringside.js` loads worker HTML in an iframe without a sandbox, and the comment there says local output documents are executable previews. A Host allow-list or an iframe sandbox would change that stated behaviour.

**Models view cost.** After the identity rewrite is skipped, `/api/models` still loads every attempt row and aggregates it in Python. On this machine, five builds of a 5,000-row synthetic log took 0.419 seconds in total. A payload cache is a separate change and is not in these pull requests.

**Run index.** `StateWriter._loop` in `ringer.py` flushes every second. Each flush with artifacts enabled calls `scan_run_states`, which reads every `state_dir/runs/*.json`. Skipping that when historical runs have not changed is a wider change than these fixes.

**Checks are shell commands.** `run_bounded` in `ringer_reliability.py` starts a string check with `create_subprocess_shell`. The check text is the operator's manifest field. That matches the executed-check design in `CONTRIBUTING.md`.

**OpenCode sandbox reads.** `engines/opencode-sandboxed.sh` allows the default Seatbelt rules and then denies file writes outside the task directory, scratch, and OpenCode state directories. The header comment says reads and network stay open.

**Self-update tracks main.** `perform_self_update` in `ringer.py` fast-forwards only a clean `main`. This fork's working branch is `ian`. `.github/workflows/fork-sync.yml` updates `ian` on a schedule. Left as documented in `config.sample.toml`.

## Secrets

A search of Python, shell, TOML, YAML, and Markdown for key, password, secret, token, and private-key markers found documentation and token-counting code, not a committed credential. No secret scanner was run.

## Checks

`RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests` on the journal branch reported `Ran 583 tests in 65.199s`, OK, 7 skipped.

The same command on the identity branch reported `Ran 583 tests in 59.584s`, OK, 7 skipped.

Both runs were on Linux. The GitHub workflow also runs the suite on macOS. That job was not run here. The Windows job is non-blocking and was not run.
