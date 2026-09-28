# Matched model reliability microbenchmark

This supplies offline comparison infrastructure: 20 distinct bounded Python jobs, fixed semantic specifications, independent assertions, generation and reporting. It does not establish model superiority or real task success. Human readability and maintainability remain **not assessed**.

The coordinator should review the replay results before running candidates. The existing model strategy requires at least **20 distinct jobs per candidate and 30 jobs per task family before promotion review**. These eight fix, six feature and six operational jobs do not satisfy the latter threshold. A three-job smoke run only checks the execution plumbing. Repeating a case does not make it a new distinct job.

## Generate and run through Ringer

Run from the isolated checkout. Choose an absolute output directory and an explicit comma-separated model list. Generation is local and never invokes an engine, a model or an API.

```bash
python3 scripts/ringer_benchmark.py generate \
  --out /private/tmp/ringer-comparison-smoke \
  --models codex:gpt-5.6-luna,codex:gpt-5.6-terra,codex:gpt-5.6-sol \
  --cases utc-order,receipt,capability-preflight \
  --max-parallel 2 --timeout-s 180

python3 ringer.py lint /private/tmp/ringer-comparison-smoke/manifest.json
python3 ringer.py run /private/tmp/ringer-comparison-smoke/manifest.json --identity benchmark-coordinator
```

Only the final `ringer.py run` command invokes workers. The commands above are instructions for the coordinator; the implementation and replay tests do not execute it. Do not invoke provider CLIs directly or bypass Ringer's engine.

Omit `--cases` for all 20. IDs or 1-based numbers are accepted, for example `--cases 1,11,15`. Generate the full comparison into a fresh absolute directory, using the same candidate list. All manifests use `run_name: ringer-model-comparison`, with a stable job ID bound to the generated bundle. The actual Ringer `tasks` list includes `key`, `spec`, `engine`, `model`, `task_type`, `timeout_s`, `check`, `expect_files` and `max_attempts: 1`. Top-level `workdir` is the bundle's `tasks` directory; starter files live at exactly `workdir / task.key`.

Supported selectors:

- `codex:gpt-5.6-luna`
- `codex:gpt-5.6-terra`
- `codex:gpt-5.6-sol`
- `opencode:openrouter/moonshotai/kimi-k2.7-code`
- `opencode:openrouter/z-ai/glm-5.2`
- `opencode:openrouter/z-ai/glm-5.3-flash`
- `opencode:openrouter/google/gemini-3.8-flash`

Codex tasks use explicit `engine_args`: `-c forced_login_method="chatgpt"`, `-c service_tier="default"`, and `-c model_reasoning_effort="medium"`. This selects ChatGPT authentication, standard service and medium reasoning for parity. A generated selector is a declaration, not evidence that a model actually executed. Availability is determined during the later Ringer run.

Generated tasks declare `billing_route: subscription` for Codex and `billing_route: api` for OpenCode. Generation never allocates a paid allowance automatically. To supply an authorised amount, add `--task-spend-allowance-gbp` with a finite positive GBP value. That value applies to each paid API cell; subscription cells receive no allowance. Without it, generation explains that paid cells still require a budget before dispatch. The normal billing admission and local policy caps still apply, including a zero local cap blocking paid dispatch even when an allowance is supplied.

Each task owns only `solution.py` and `notes.md`. Its brief prohibits reading reference answers and editing checker sources, fixtures, logs, score files or other tasks. Runtime fixture access is confined to explicitly supplied paths; only the argv case executes a supplied safe local Python command. No live services, credentials or billing are needed.

## Workload contracts

The controlling public briefs are in `benchmarks/reliability/cases.py`. Each names the callable, all input and output semantics, representative expected values and constraints. The generator copies the complete brief and a syntactically valid defective starter, never a reference implementation. The fixed seed is `20260903`; there is no model-dependent input sampling.

| Number | Case ID | Family | Behaviour and representative expectation |
|---:|---|---|---|
| 1 | `utc-order` | code-fix | `order_records(records)` sorts actual UTC instants across offsets and fractions, retaining equal-instant order. |
| 2 | `cache-tokens` | code-fix | `billable_tokens(events)` subtracts cache subsets once: total 10, cached 7, then uncached 3 gives 6. Invalid counts raise `ValueError`. |
| 3 | `retry-idempotence` | code-fix | `unique_events(events)` retains the first event per ID; changed retry payload does not replace it. |
| 4 | `safe-path` | code-fix | `safe_artifact_path(root, name)` rejects absolute names, traversal, root itself and real symlink escapes with `None`. |
| 5 | `usage-dedup` | code-fix | `unique_usage_count(events)` deduplicates `(job, request)`, preserving cross-job requests. |
| 6 | `strict-record` | code-fix | `extract_record(text)` requires one exact JSON object, non-empty string ID and integer value, rejecting duplicate keys, booleans and echoed queries. |
| 7 | `no-default` | code-fix | `new_items(items=None)` returns independent empty defaults and the exact supplied list object. Mutation across calls tests identity. |
| 8 | `half-up` | code-fix | `display_units(value)` rounds decimal strings half away from zero: `'-2.5'` gives `-3`, with large exact values tested. |
| 9 | `lifecycle` | code-feature | `reconcile(events)` selects highest sequence per job, detects conflicting duplicate sequences, and tolerates arrival reordering. |
| 10 | `budget` | code-feature | `reserve(amount, balance)` uses decimal strings and HALF_UP before exact affordability comparison: `('1.005','1.01')` gives `'1.01'`; balance `'1.009'` refuses. |
| 11 | `receipt` | code-feature | `valid_receipt(artifact, receipt)` requires exact boolean human and approval fields, a distinct named reviewer and the current artifact SHA. Stale hash and self-approval fail. |
| 12 | `submission-lock` | code-feature | `submission_state(operations)` models atomic admission before finish, concurrent interleavings, duplicate requests and separate jobs. |
| 13 | `regression` | code-feature | `new_failures(current, baseline)` computes a sorted unique directional difference against the supplied baseline. |
| 14 | `config-merge` | code-feature | `merge_config(base, overlay)` recursively merges only the engines scope, retaining nested siblings and independent output containers. |
| 15 | `capability-preflight` | operational | `preflight(capabilities, required)` runs when exact capabilities exist; a required unavailable network yields `BLOCKED`. |
| 16 | `source-precedence` | operational | `choose_source(requested, available)` selects only the exact source, even behind a plausible mirror; unavailable exact sources yield `BLOCKED`. |
| 17 | `argv-literals` | operational | `invoke_args(args)` actually executes a supplied safe Python command with a list and explicit `shell=False`. Spaces, empty args, quotes, `$HOME` and `$()` stay literal. |
| 18 | `export-files` | operational | `export_content(root, destination, files)` copies actual explicitly listed ignored build bytes, returns digests and rejects invalid lists before writing. No unlisted files appear. |
| 19 | `redaction` | operational | `redact(text)` removes multiple quoted/unquoted secrets while preserving useful messages and similarly named non-secret fields. |
| 20 | `cost-rate` | operational | `cost_per_accepted(accepted, cost)` returns exact HALF_UP known money per accepted job; `(2,'1.01')` gives `'0.51'`, while invalid, negative, non-finite or unknown inputs yield `None`. |

## Independent execution and evidence

`assertions.py` holds fixed expected values. `fixtures/references.py` contains coordinator-only known-good and fixed defective replay implementations. References are not used as runtime oracles. The probe executes candidate functions; the parent checker compares outcomes against assertions and assigns scores. Correctness and boundary assertions have different labels and denominators. Input preservation, subprocess invocation and filesystem read-backs are substantive checks; there are no token-count or line-count quality scores.

A fresh candidate subprocess runs each assertion, with a two-second timeout by default and process-group cleanup. Identity/mutation tests deliberately make several calls within one process. Import failures, exceptions, hangs, extra stdout and protocol failures produce structured failing evidence. Skipped checks after an infrastructure failure do not pass. The coordinator-side process writes scores and source snapshots after child execution. It never accepts a worker-authored score.

Generated check commands use the actual `sys.executable` and shell-quote every argument. They pin an aggregate SHA-256 covering checker, probe, assertions, case definitions, replay references, contract tests and CLI. Ringer's `source_sha256` map additionally checks individual source files before dispatch. The checker compares the expected aggregate at execution and rechecks source and candidate hashes afterwards. Changing a covered source requires generating a fresh bundle. Task ownership and content hashes detect source edits; this is not an operating-system sandbox against actively malicious Python. Run within the normal Ringer sandbox and preserve coordinator ownership of evidence.

A direct local replay can emit full JSON to stdout:

```bash
python3 scripts/ringer_benchmark.py check \
  --case utc-order --candidate /absolute/path/solution.py
```

Direct unpinned checks are for development. The generated manifest always supplies `--expected-source-sha256`, `--require-notes` and coordinator-owned evidence paths. Generated checks also require non-blank `notes.md`. Each score includes checker ID, source/spec/case/candidate digests, seed, passed/total checks, separate correctness/boundary scores, failure class and `nohumanpromotion: true`. The result is `PASS` only when the callable meets all normal and boundary assertions and the generated task contract passes.

Expected `BLOCKED` is a decision within a check. The capability and source cases mix normal and impossible inputs, so the whole case passes only when both are handled correctly. `correctly_blocked_decisions` counts correct refusals separately, with zero completed deliverables attributed to those refusals. This simulated receipt validation never creates a human review receipt or promotion.

## Report actual runs without hiding missing work

```bash
python3 scripts/ringer_benchmark.py report \
  --manifest /private/tmp/ringer-comparison-smoke/manifest.json \
  --state-dir "$HOME/.ringer" \
  --out /private/tmp/ringer-comparison-smoke/report.json
```

`--state-dir` accepts Ringer's state directory or its `runs/` directory. The report uses the actual `StateWriter` schema: run IDs, task keys, task directories, status/verdict, attempt counts, check command/result, check output and explicitly recorded log paths. It keeps every manifest cell, including missing, queued, interrupted and failed cells. Each cell retains per-run and per-attempt results, first try, final task outcome, failure classes, independent quality dimensions and repeat identity.

Only score pointers in an executed check section of the recorded attempt log, or the final check output in a state snapshot, may lead to evidence. The report validates the pointer digest, allowed coordinator evidence directory, generation/cell/attempt binding, current source/spec hashes and archived candidate SHA. The final current artifact must still match its score. Missing or pruned logs remain gaps; earlier attempts cannot be reconstructed from the final state alone. Unreferenced `score.json` files are ignored.

Older Ringer versions retain checker output in `check_output_tail` without appending it to `worker.log`. The final state pointer is therefore eligible even when a valid worker segment exists. It must match that segment's exact task directory, attempt index and start timestamp. Worker exit, check return code, timeout, state verdict, duplicate evidence and state/log disagreement checks remain in force. State evidence does not reconstruct earlier attempts.

Engine and model fields from manifests or states remain `DECLARED`. A bounded actual Codex CLI header in the worker execution log can establish `LOG_HEADER_VERIFIED` identity, including a mismatch with the requested model. Task names, `solution.md`, a `reported_model` field or a command flag cannot verify identity. OpenCode identities remain declared until a supported exact execution-header parser exists. Scores cannot override identity metadata.

The header parser recognises only an immediate bounded header after a supported preamble. Multiline CLI commands and startup-warning prefixes remain `DECLARED`; it does not scan past an unknown prefix or user prompt to find a plausible header in worker text. A verified quality score does not promote declared model identity.

JSON includes full run/attempt evidence and digest results; Markdown summarises the cells. A raw Ringer pass, an interrupted check or a stale score cannot establish a quality pass. Neither format infers `READY`, `USED`, human approval or model promotion.

Cost is optional and currently left unavailable in this report. Observed total tokens are preserved as tokens; they are never converted to money. Subscription access does not establish a free allocation. If monetary comparison is needed, the coordinator must supply per-request usage and explicit monetary evidence separately.

Historical audits appear only when explicitly passed with `--historical /absolute/path/audit.json`. They are identified and hashed as separate context, never merged into the matched cell sample.

Re-evaluating a historical matched trial requires its corresponding frozen checker snapshot. The finished trial pinned source SHA-256 `da8181e76fb1d5702182a472d62d6488b14585767b0adb94ebbdbc6f82464c6e`, preserved in `.ringer-work/ringer-success-improvements/benchmark-frozen`. This updated checkout has a different source hash. Reporting old evidence against the new checkout must retain `SOURCE_OR_SPEC_MISMATCH`; neither the old manifest nor its historical proof should be rewritten. The coordinator's read-only `output/ringer-success-improvements-2026-09-03/evaluate_trial.py` adapter uses the original frozen checker, authoritative state and usage session IDs for that trial.

## Billing-enabled ask

`ringer.py ask` accepts `--billing-route subscription|api` and `--task-spend-allowance-gbp`. When billing is enabled, Codex defaults only to the subscription route and resolves the explicit `--model` first, then `engines.codex.model_default`. A missing model stops the request with a configuration error. Other engines require an explicit billing route; they never acquire a paid route by default. Their model override is available when a billing route is explicitly supplied.

The resulting task passes through the same runner billing admission as a manifest task. An explicit API route still needs an allowance and available policy budget. Billing-disabled calls retain the existing defaults. Packet selection, source boundaries, redaction and the single-attempt answer check retain their existing behaviour. The answer check proves only that `answer.md` is non-empty; the coordinator must review its content.

## Output and replay controls

Generation preflights the entire output bundle before writing. Repeating an identical untouched generation is a no-op; any modified or incomplete bundle is refused. Files are staged and atomically published without overwriting existing targets, with the manifest published last. An ordinary failed publication rolls back only files created by that invocation. A hard process kill can leave staging files or an incomplete new bundle; choose a fresh output directory after inspection. No recursive deletion of existing files is used. Reports likewise preflight both JSON and Markdown together and refuse modified output. Symlink output redirects are rejected; the normal macOS `/var` and `/tmp` aliases are canonicalised.

Run the deterministic contract tests and the coordinator's separate read-only checker:

```bash
python3 -m unittest discover -s tests -p test_benchmark_contract.py
python3 .ringer-work/ringer-success-improvements/check_benchmark_contract.py \
  .ringer-work/ringer-success-improvements/repo
```

The replay suite checks all 20 references, a meaningful fixed defect for each case, all starters, real Ringer lint, source tampering, literal execution, file bytes, preservation, declared identity, missing/interrupted cells, repeated runs, first/final attempts, stale/orphan evidence, promotion refusal and output transactions. It uses actual Ringer state snapshots without invoking worker engines.

Next step: the coordinator reviews these artifacts and replay results, then runs the matched subscription comparison through Ringer.
