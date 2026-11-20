# Blueprint — adapt with care

# doc-swarm

## What it is

A doc swarm assigns one module or surface to each worker and writes all docs into a shared docs directory with disjoint file ownership. The spec teaches the audience, the surface, and the section contract so workers produce comparable docs instead of generic summaries.

This is a blueprint, not a recorded proven kit. Adapt the section contract and example runner to the target repo before trusting it.

## When to use

Use this when a codebase needs module docs, route docs, command docs, or workflow docs across many independent surfaces.

Do not use this when docs require one worker to make cross-module product judgments. Run a review-swarm first or assign one orchestrator-owned synthesis task after the per-surface docs exist.

## Fill in

| Placeholder | What goes there |
|---|---|
| `{{PROJECT}}` | Short project name used in the run name. |
| `{{WORKDIR}}` | Scratch run directory for Ringer. |
| `{{DOC_KEY}}` | Stable task key for one module or surface. |
| `{{REPO_PATH}}` | Absolute path to the source tree workers may read. |
| `{{DOCS_DIR}}` | Absolute shared docs directory workers may write into. |
| `{{DOC_FILE}}` | File owned by this worker, relative to `{{DOCS_DIR}}`. |
| `{{AUDIENCE — who will read this doc, what they already know, and what decision or task the doc should help them perform}}` | The specific reader and job the doc serves. |
| `{{SURFACE — module, route, package, component, command, or workflow with source entry points}}` | The exact surface and source entry points to document. |
| `{{SECTION_CONTRACT — exact headings and what each section must contain, including a Documented Symbols section and at least one runnable example}}` | The required headings and content rules. |
| `{{SOURCE_SCAN_COMMAND — exact command from the source root that lists or opens the relevant files}}` | Command the worker can run to inspect the source. |
| `{{LOCAL_DOC_VERIFY — exact preview, formatter, or docs command if one exists; otherwise write NONE}}` | Optional local docs command for the worker to run before finishing. |
| `{{SYMBOL_SECTION}}` | Heading name that contains documented API symbols as Markdown code spans, usually `Documented Symbols`. |
| `{{RUNNABLE_EXAMPLE_LANGUAGE}}` | Fence language treated as runnable, for example `python`, `bash`, or `javascript`. |
| `{{EXAMPLE_CWD}}` | Absolute path to the read-only source or fixture directory used for relative imports and reads. Each example starts in a fresh scratch directory. |
| `{{EXAMPLE_RUNNER_COMMAND}}` | Command template for each runnable fence. Use `{file}` for the temporary script and `{example_cwd}` for the read-only fixture path. Ordinary runners such as `python3 {file}` and `node {file}` work. |
| `{{PYTHON}}` | Python executable for the check script. |
| `{{KIT_DIR}}` | Absolute path to `templates/doc-swarm` in this Ringer checkout or copied kit location. |
| `{{RINGER_CHECK_MODULE_DIR}}` | Absolute Ringer checkout root. For a copied kit, put the three listed Ringer modules beside `doc_check.py` and use that directory here. |
| `{{REQUIRED_SECTIONS}}` | Semicolon-separated required Markdown headings, without `#`, for example `Overview;Documented Symbols;Examples;Assumptions`. |
| `{{MIN_WORDS}}` | Minimum total word count for the doc. |
| `{{MIN_SECTION_WORDS}}` | Minimum word count for each required section. |
| `{{SYMBOL_ALLOWLIST}}` | Semicolon-separated code spans in the symbol section that do not need to appear in source, usually `NONE`. |

## Checks

`checks/doc_check.py` validates the doc path, required headings, total word count, and per-section substance.

It searches regular source files for the documented symbols, excluding common generated and dependency directories. Each matching fence runs in its own isolated process with a fresh working directory, read-only source and fixture access, a minimal environment, and networking disabled. Source and fixture paths in the manifest are explicit read grants. Select trees that contain only material the examples may read.

Linux uses bubblewrap and libseccomp. macOS uses Seatbelt through `sandbox-exec`. Verification fails if the OS sandbox cannot start.

This is hard to game because invented API names fail source lookup and fake examples fail execution. It still cannot prove the prose is good; the orchestrator should spot-check a passing doc and run a synthesis pass for cross-surface consistency.

## Mix with

Use `templates/review-swarm.json` first to discover which modules need docs and what risks each doc must cover.

Use `templates/test-hardening/` when docs expose missing examples that should become executable tests. Use `templates/migration-swarm/` before doc-swarm when a mechanical API rename would make freshly written docs stale.

## Gotchas

The shared docs directory only works when every task owns a different file. Do not let two workers write the same index, nav file, or sidebar file.

Source lookup catches many invented API names, but not invented behavior described without a symbol. Require file-path citations in the section contract for behavior claims.

Runnable examples must be self-contained. If they need credentials, network state, or a dev server, convert them to test fixtures or mark them as non-runnable text and adjust the contract honestly.

