# Ringer

[![tests](https://github.com/NateBJones-Projects/ringer/actions/workflows/tests.yml/badge.svg)](https://github.com/NateBJones-Projects/ringer/actions/workflows/tests.yml)

![Ringer — she reviews; the wall works](docs/hero.png)

**Parallel AI-agent swarms that prove their work. The coordinator plans and reviews; suitable workers do the implementation.**

Frontier models are finally good enough to trust with real implementation — but their tokens are priced like senior-engineer hours, and most of a build is not senior-engineer work. It's scaffolding, migrations, test suites, batch transforms. Mechanical labor.

Split the roles: the coordinator writes specs and checks, chooses the route and reviews results. Suitable workers implement in parallel. For complicated work, prioritise already-authenticated subscription models through their native configured harness, including Codex subscription workers; exact model and effort are chosen per task.

One problem: parallel agents lie. "Done" doesn't mean working. Ringer doesn't take the worker's word for anything — it **executes your check command** against the artifact. Pass or fail is decided by running the code, not by reading the agent's summary. Failures retry once with the failure context injected, and every attempt is logged so your setup gets measurably better over time.

And because a swarm you can't see is a swarm you don't trust: **Ringside**, a local web page every run opens automatically, showing every live swarm on your machine — who's running it, what each worker is doing, elapsed time, token burn — in real time, plus a versioned library of what past runs produced.

## How it works

```
manifest.json ──▶ ringer.py ──▶ N parallel workers (codex exec, each in its own dir)
                      │                │
                      │                ▼
                      │         executed checks ── fail ──▶ retry once w/ failure context
                      │                │
                      ▼                ▼
              ~/.ringer/runs/    eval log (JSONL or Postgres)
                      │
                      ▼
              Ringside, in the browser (live, all swarms, all identities)
```

## Quickstart

Ringer runs on macOS and Linux (Windows via WSL) and needs Python 3.12+.

1. Install a worker CLI and sign in (Codex is the built-in default engine):

```bash
npm install -g @openai/codex   # or: brew install --cask codex
codex login                    # sign in with your ChatGPT plan
```

2. Get the repo:

```bash
git clone https://github.com/NateBJones-Projects/ringer && cd ringer
mkdir -p ~/.config/ringer && cp config.sample.toml ~/.config/ringer/config.toml   # optional — sane defaults without it
```

3. Teach your agent to route work through Ringer:

```bash
# optional but recommended: teach your agent to route work through ringer
./ringer.py install-agent
```

4. Assess and preview the demo before dispatch:

```bash
./ringer.py demo --dry-run                           # zero-model assessment draft
# Save and fill every judgement field, then run demo with --assessment PATH.
```

The demo spawns three Codex workers in parallel, verifies each artifact by executing it, and prints a verdict table — and Ringside, the live dashboard, opens in your browser on its own. Passing executed checks establish product state; coordinator review is still required, and no human promotion is implied.

Draft your own batch (replace the model placeholder with an exact locally available subscription model before assessment):

```bash
./ringer.py assess swarm.json --coordinator codex-orchestrator --output assessed.json
# Fill every judgement field; select an exact locally available model and effort.
./ringer.py lint assessed.json
./ringer.py run assessed.json --dry-run
./ringer.py run assessed.json --max-parallel 4
```

```json
{
  "run_name": "my-batch",
  "workdir": "/tmp/my-batch",
  "max_parallel": 3,
  "tasks": [
    {
      "key": "alpha",
      "engine": "codex",
      "model": "REPLACE_WITH_LOCAL_MODEL",
      "billing_route": "subscription",
      "task_type": "docs",
      "engine_args": ["-c", "model_reasoning_effort=low", "-c", "service_tier=default"],
      "spec": "Create alpha.txt containing exactly one line: alpha ready\nEnd the file with exactly one newline. Do not add punctuation.",
      "check": "printf 'alpha ready\\n' | diff -u - alpha.txt || { echo 'FAIL: alpha.txt must contain exactly alpha ready followed by one newline'; exit 1; }",
      "expect_files": ["alpha.txt"]
    }
  ]
}
```

Each task gets its own directory, its own worker, its own log, and its own verdict. `check` is any shell command — exit 0 is the only thing Ringer believes.

> **Write checks that print why they fail.** A silent `exit 1` (the `git diff --quiet` style) costs you twice: the retry prompt gets no failure context to fix against, and the eval log records an undiagnosable row. `diff` beats `diff -q`; an assert with a message beats a bare test.

> **A check cannot demand evidence the spec never supplied.** Before failing a worker for missing evidence or input, re-read the task inputs. If the spec didn't provide a value, the check must not invent one and fail on its absence — an honest UNVERIFIABLE answer is not a failure. Reserve hard failure for what the spec actually asserted.

> **Executed checks catch laziness, not subtle wrongness.** A check that *runs* the artifact catches a plausible-but-wrong change far less often than it catches a missing one. Whenever a swarm touches a dogfood artifact (Ringer's own docs, config, or checks), add an "our own artifact passes our own validator" test so the checker exercises what it preaches. And keep orchestrator patch review mandatory regardless of PASS status — a green check is not proof of semantic correctness.

**Identity**: runs are stamped with an orchestrator identity (shown in Ringside and eval rows). Resolution order: `--identity` > `FLEET_IDENTITY`/`RINGER_IDENTITY` env > a `.fleet-agent` file found walking up from the working directory (drop one in a repo root to give that repo's swarms their own name) > `identity_default` in config > short hostname.

### Manifest fields

| Field | What it does |
|---|---|
| `key` | Task name — becomes the working subdirectory and the label everywhere |
| `spec` | The prompt handed to the worker |
| `check` | Shell command run after the worker exits; exit 0 = PASS |
| `expect_files` | Files that must exist and be non-empty before the check runs |
| `engine` | Which configured engine runs this task (default `codex`) |
| `model` | Exact locally available model selected per task and recorded in the bound assessment; OpenRouter candidates use an explicit current `openrouter/<exact-model-slug>` through `opencode`, not an implicit default |
| `billing_route` | Explicit `subscription` or `api`; subscription priority never grants paid API authority |
| `task_type` | Optional free-form string naming the kind of work this task is, so the model-performance log can slice pass rates by task shape rather than only by model. Suggested vocabulary: `code-feature`, `code-fix`, `code-review`, `test-hardening`, `docs`, `research`, `persona-review`, `copywriting`, `site-build`, `motion-design`, `image-gen`, `data-pipeline`, `format-conversion`, `probe`, `bakeoff`. Empty is allowed; the log just reports it under `(none)`. |
| `timeout_s` | Per-task kill timer (default 900) |
| `max_attempts` | How many times this task may run (default 2 — one try plus one retry with the check's failure output injected). Set `1` for a hard no-retry lane |
| `redact_spec` | Replace this task's spec with `[redacted request packet]` in the run state, the logged command line, and the eval row, for specs carrying sensitive material. Redacts Ringer's own records only — captured worker output is never rewritten (invariant), so a worker that echoes its request still puts that text in `worker.log` |
| `engine_args` | Extra CLI flags for this task's worker, spliced in at the engine's `{engine_args}` placeholder — e.g. `["-c", "model_reasoning_effort=low"]` so the orchestrator picks reasoning depth per task |
| `verified` | One plain-English sentence saying what the check proves — shown on the results page next to "finished & checked" |
| `full_access` | Worker runs unsandboxed — required for workers that spawn their own sub-workers; must also be enabled in config |
| `worktrees` (run-level) | Give each task an isolated git worktree of `repo` so parallel workers can't collide |

> **Worktree footgun:** on PASS the task's worktree is removed — including anything written inside it. In worktrees mode, worker logs live outside task worktrees in `workdir/logs/`; have workers write deliverables outside the worktree too, or have your `check` copy artifacts out before it exits 0.

Not sure what your tasks even are yet? [`docs/interview-prompt.md`](docs/interview-prompt.md) is a prompt you paste into any chatbot; it interviews you about the job and hands back a brief your orchestrating agent can turn into a manifest. Ready-made skeletons for the patterns that work live in [`templates/`](templates/).

## `ask` — one bounded question, one clean worker

Not every question deserves a manifest. When you want a read-only answer over
source you can already point at, `ask` selects the passages that match the
request, caps the packet, and runs a single worker on it:

```bash
./ringer.py ask "Why did the Wednesday release slip?" --source notes/status.md --dry-run
# Save and fill the source-bound draft, then repeat with --assessment PATH.
./ringer.py ask "..." --source src/ --source docs/ --dry-run   # show the packet, spend nothing
```

Repeat `--source` for more files or directories. `--state` takes a small file
of settled decisions and is preferred over ordinary sources when the packet is
tight. `--max-packet-bytes` sets the budget (default 16,000). `--dry-run`
prints the selection report and source-bound assessment draft, and stops before
any model call. Fill the draft and pass `--assessment` before dispatch; the
lighter `ask` path still requires model-fit assessment. `--redact` keeps
the request out of the run state and eval row. The run appears on Ringside and
in the artifact library like any other.

If everything that matches is too big for the packet, `ask` says so — naming the
budget you'd need — and stops **before** calling a model. It never sends an
empty packet. A source small enough to fit whole is included whole, whether or
not it looks relevant, so pointing `ask` at unrelated material still costs one
call: the packet is only as good as the sources you name.

Directory scans stay inside the tree you named. A symlink pointing out of it, or
one resolving to a sensitive filename, is skipped and reported. A file you name
explicitly is always read — naming it is consent.

> `ask` verifies only that an answer was produced and is non-empty. There is
> nothing to execute against free-form prose, so this is the one lane in Ringer
> where the check does not prove the result is right. Read the answer. Anything
> whose output a check could actually execute belongs in a manifest.

## Lint

Lint checks a manifest for the mistakes that make swarms hard to trust: checks that cannot fail, silent checks, worktree deliverables that disappear, worker commits that die with deleted worktrees, serial fan-out, write collisions, and underspecified specs.

```bash
./ringer.py lint templates/review-swarm/manifest.json
lint: clean (1 tasks)
```

`run` and `demo` also print any lint findings as non-blocking warnings after the manifest loads. They teach at the moment of use; they do not stop a run.

A check that cannot fail is trusting the worker with extra steps.

### Baseline: prove your checks before spending tokens

Lint reads the manifest; `--baseline` executes it — every task's `check` runs against the unmodified tree, spawning no workers and writing no eval rows:

```bash
./ringer.py run swarm.json --baseline
```

Each check runs in a fresh scratch dir (a detached worktree when the manifest uses worktrees) through the same verifier as a real run. Reading the results: an assertion that demands the NEW behavior workers will build is *expected* to FAIL baseline; an assertion about UNCHANGED behavior that fails baseline is a bug in the check itself, and at run time it would burn a worker's attempts against something no model can satisfy. Fix the check before spawning.

## Make your agent actually use this

Between swarms, agents drift back to invisible inline work. Reminders decay, so enforcement ships with the product.

For Claude Code, run:

```bash
./ringer.py install-agent
```

It installs the ringer skill — the orchestrator playbook — user-level for Claude Code, and registers two gentle hooks: a Bash hook that notices model-calling or harness commands running outside a live Ringer run, and an edit-loop hook that notices batch editing without a run. Each hook nudges ONCE per session, pointing the agent at the skill.

For Codex, run:

```bash
./ringer.py install-agent --codex
```

This installs the Codex-native ringer skill under `~/.agents/skills/ringer` (the path Codex discovers repository and user skills) and merges equivalent `PreToolUse` and `PostToolUse` nudges into `~/.codex/hooks.json` (or `$CODEX_HOME/hooks.json` if `CODEX_HOME` is set — skill discovery is not tied to `CODEX_HOME`, only hook/config storage is). Start a new Codex session, open `/hooks`, and approve the two Ringer hooks before they can run. Codex owns hook trust; the installer does not bypass that review.

The playbook installed under `~/.agents/skills` (read by Codex and Antigravity) keeps whichever host is running in the coordinator seat, with Antigravity as the default; Codex subscription workers remain allowed but are not preferred. Use Ringer automatically when beneficial, without permission or model-selection confirmation; prioritise suitable authenticated subscription models through their native configured harness for complicated work. The hooks understand canonical Codex `Bash` and `apply_patch` payloads (with the command source in `tool_input.command`) as well as the `functions.exec` compatibility shape, while preserving the same once-per-session, non-blocking behavior.

The hooks never block anything. A user who says "just do it inline" is obeyed. Uninstall with `./ringer.py uninstall-agent` for Claude Code or `./ringer.py uninstall-agent --codex` for Codex. Add `--project` to either command to use the current project's `.claude` directory or the current project's `.agents` (skill) and `.codex` (hooks) directories instead of the user-level installation. Running `install-agent --codex --project` from inside the Ringer repository itself is safe: the canonical skill is left in place (the source is the target) and only the hooks are written.

For CI and evals, `config.sample.toml` includes `[engines.mock]` so the enforcement stack can be tested without an API bill.

## Automatic routing and assessment

Choices within existing authority are coordinator decisions; only genuinely
missing authority warrants a question. Select exact locally available model
and effort per task from configured harnesses and observed auth/quota. Record
explicit subscription billing and standard service for subscription workers.
Keep executable checks, sandbox, source integrity and artifact-bound review
receipts. Never silently fall back to paid API or Fast, change credentials,
top up or change budget caps.

Every real Ringer model job requires a bound model-fit assessment, including
one-task jobs, `ask`, `demo`, reviews and repairs. Assessment is NOT user
approval. Generate the zero-model draft with `ringer.py assess`, fill each
judgement, lint and inspect the dry-run command before dispatch. Reassess after
execution-relevant changes. See [model routing](docs/MODEL-ROUTING.md) and
[model evaluation](docs/MODEL-EVALUATION.md). This supersedes historical
OpenRouter-first/OpenCode-only routing, no-Codex-worker rules and job-specific
subscription restrictions; historical notes remain evidence, not current orders.
Product PASS and export PASS remain separate from human promotion: a worker
cannot issue its own human review receipt, and coordinator review precedes use.

## Engines are pluggable

![Identical workers, each under its own light](docs/engines.png)

Ringer ships with three worker lanes: **Codex CLI** is the built-in default, and `config.sample.toml` carries verified engine blocks for **Grok Build CLI** (works as-is once you `grok login`) and **OpenCode + OpenRouter** (one edit: point `bin` at the sandbox wrapper in your clone). Anything else with a headless CLI is a config block away:

```toml
[engines.mymodel]
bin = "/usr/local/bin/mycli"
args_template = ["run", "{spec}", "--dir", "{taskdir}"]
```

Per-task `"engine": "mymodel"` routes work to it — the invariants (stdin closed, process-group kill, executed verification, raw logs) apply to every engine identically.

### The OpenRouter harness: OpenCode

For OpenRouter candidates, OpenCode is the configured harness: use per-task `"engine": "opencode"` and an explicit currently available `"model": "openrouter/<exact-model-slug>"` after catalog and task-family evidence review. Subscription models use their native configured harness. Historical July 2026 setup note: the GLM `z-ai/glm-5.2` default was estimated at $0.74/M input and $2.33/M output, roughly 20–30x cheaper output than frontier models and around a penny per checked task. Those estimates describe an earlier cheap lane; they do not authorise an implicit model or prove current quality. Keep the selected model explicit in the task and matching assessment, rather than cloning engine blocks or hiding model switches in `engine_args`.

OpenCode ships no OS sandbox, so the engine's `bin` points at an absolute path to `engines/opencode-sandboxed.sh` (ringer does not resolve engine bins relative to the repo): a macOS Seatbelt wrapper that leaves network and reads open but confines writes to the task dir, a per-run scratch dir (wired as the agent's `TMPDIR`/`XDG_CACHE_HOME`), and OpenCode's own state/config dirs. Its `--dangerously-skip-permissions` flag only silences OpenCode's interactive prompts; Seatbelt is the actual containment. Task paths reach the profile as `sandbox-exec -D` parameters rather than string interpolation, so a task dir with quotes or parens can't inject sandbox rules. `--no-sandbox` is wired as the engine's `full_access_args`, so ringer's `allow_full_access` gate still governs escapes. Non-macOS installs need their own sandbox (or full-access mode).

To let a worker edit a real repository, pass the repo checkout to the wrapper with its repeatable `--writable-root PATH` option — e.g. `engine_args = ["--writable-root", "{{REPO_PATH}}"]` (the `repo-feature` kit does exactly this). The wrapper strips every `--writable-root` pair before OpenCode sees the args and opens each root to the Seatbelt write allowlist (again via `-D` parameters, never interpolation). The Codex-only config string `-c sandbox_workspace_write.writable_roots=[...]` is NOT understood by this wrapper and is silently ignored — so a custom engine that wants OS-level write confinement must supply its own equivalent writable-root mechanism, or its workers hit EPERM on every repo edit.

Setting it up takes about five minutes:

```bash
# 1) Install the OpenCode CLI (pick one)
curl -fsSL https://opencode.ai/install | bash
# or: npm install -g opencode-ai
# or: brew install anomalyco/tap/opencode

# 2) Connect OpenRouter — create a key at https://openrouter.ai/settings/keys
opencode auth login   # select OpenRouter, paste the key

# 3) In ~/.config/ringer/config.toml, uncomment [engines.opencode] and set
#    bin to the ABSOLUTE path of engines/opencode-sandboxed.sh in this clone.
#    (Linux/WSL: the wrapper is macOS-only — set bin to the opencode binary
#    itself; there is no OS write-confinement then, so keep manifests scoped.)
```

Choose a supported reasoning variant explicitly in `engine_args`, and verify that the bound assessment matches the resolved command. Paid OpenRouter routes require existing API authority and budget admission; these calls do not use Codex subscription quota. A catalog listing, free promotion or compatible command does not establish quality or grant paid authority.

### The plan lane: Grok Build CLI

If you already pay for SuperGrok or X Premium Plus, Grok Build is a second flat-rate worker lane — no per-token bill:

```bash
# 1) Install (pick one)
curl -fsSL https://x.ai/cli/install.sh | bash
# or: npm install -g @xai-official/grok

# 2) Sign in — OAuth on a SuperGrok or X Premium Plus plan
grok login

# 3) In ~/.config/ringer/config.toml, uncomment [engines.grok]
```

Route with per-task `"engine": "grok"` and pick the model with `"model": "grok-build"` or `"model": "grok-composer-2.5-fast"` (the shipped default — the speed pick). Grok brings its own OS sandbox on macOS (profile `workspace`: read everywhere, writes confined to the task dir, temp, and `~/.grok`), and its JSON output exposes no token counts — plan-billed workers report cost as included in plan.

`args_template` is an argv array, not a shell string. Ringer replaces `{taskdir}`, `{spec}`, and `{model}` inside each argv element. `{access_args}`, `{sandbox_args}`, `{full_access_args}`, `{model_args}` (becomes `-m <resolved model>` when the task or engine names one), and `{engine_args}` (the task's per-task `engine_args`) expand to multiple argv elements only when they appear as their own array item.

Watch for variadic CLI flags. If an engine has a flag that consumes all following values, put `{spec}` before that flag. For Claude-style CLIs, prefer:

```toml
args_template = ["-p", "{spec}", "--allowedTools", "Bash"]
```

not:

```toml
args_template = ["-p", "--allowedTools", "Bash", "{spec}"]
```

Each worker process runs with cwd set to `workdir/<task.key>/`. Use absolute paths in `spec` when workers need shared inputs outside their task directory.

## Ringside — mission control

![Ringside in the browser: a run's live results page with per-worker status and verification](docs/ringside.png)

Ringside is a local web page — no install, no account, nothing leaves your machine. Your first run opens it automatically; every later run streams into the same tab:

```bash
./ringer.py run manifest.json   # starts Ringside and opens the tab for you
./ringer.py hud                 # or open it any time → http://127.0.0.1:8700
```

**Live runs** shows every swarm, its progress, and a searchable task table. Select a task to inspect its brief, model, harness, attempts, last executed check and raw worker log. A failed check stays visible while its task retries; stopped workers are marked explicitly. **Outputs** keeps each run's result and saved versions together, with a live preview and access to its folder. **Models** shows first-try pass rates alongside task/attempt counts, task-type filtering and a low-sample warning. Expand all signals for the full scoreboard and judgment notes. Compact view keeps live runs and runs needing attention in a small window.

Multiple swarms at once is the designed-for case: run three batches under three identities and Ringside shows all three, live. `--browser` opens a simpler per-run fallback dashboard, and `--no-dashboard` runs headless.

A native desktop build (Tauri, under `hud/`) exists as a v0.1.1 prototype. It shares `dashboard/ringside.html`, `ringside.css` and `ringside.js` with the browser, plus the native transport in `hud/frontend/hud.js`. The browser remains the primary interface. Native Models and folder actions need the local `ringer.py hud` server; runs and artifact reads also have native transports. Build assets under `hud/dist/` are generated by `hud/scripts/sync-dist.sh`, never edited directly. The older `dashboard/dashboard.html` remains the per-run `--browser` fallback.

The visual direction is recorded in [the Ringside Figma design](https://www.figma.com/design/iG6yecFGAcwTH4rUos3Juk?node-id=5-2). Verification and a disposable preview fixture are documented in [tests/TESTING.md](tests/TESTING.md).

## Self-update

Ringer checks `origin/main` at process start, before it dispatches the requested command. Checks are throttled to once per hour by default. You can also run `./ringer.py self-update` for an immediate, human-readable check that ignores the throttle.

An automatic update applies only when the checkout containing `ringer.py` is on `main`, has no tracked changes, and `origin/main` can be reached with a fast-forward-only update. Untracked files do not block it. After applying, Ringer restarts the original invocation so the requested command runs on the new code.

Ringer never creates a merge commit, never rebases, never stashes or deletes changes, and never updates a dirty tracked tree. Regular commands do not update in the middle of a run: their only check happens at process start before dispatch.

The persistent `hud` command is the exception for long-running code. It checks on the configured interval and restarts itself after an ff-only update. It also restarts when the checkout's on-disk HEAD changes after a manual pull. Before restarting it closes the HTTP server, whose socket is configured for immediate reuse. If an update is available but blocked, Ringside keeps serving the running code and shows the reason in a dismissible banner.

Disable automatic checks for one invocation with `--no-self-update`, for an environment or service with `RINGER_NO_SELF_UPDATE=1`, or permanently in config:

```toml
[update]
auto = false
check_interval_s = 3600
```

## The eval loop

![Timed, verified, logged](docs/eval-loop.png)

Every worker attempt — pass, fail, timeout, retry — is logged with its spec, engine, duration, token count, and the raw check output. Local JSONL by default; point `[eval.postgres]` at a database to aggregate across machines. Failure rows are the point: they tell you which spec styles, engines, and task shapes actually work, so the swarm gets better on evidence instead of vibes.

## Model performance log

### Model identity taxonomy

The scoreboard keeps the trained model, its lab, the invoking harness, the access plan, and any explicit reasoning effort as separate fields. Reserved test names never render, and historical rows without a stamped model are quarantined instead of being credited to an engine default. Models with a declared canonical access route are enforced at lint and run time — a manifest that reaches a model through a non-sanctioned harness/slug is refused unless you pass `--allow-noncanonical-route`, and historical rows from such routes display as `misrouted` and are never ranked. See the normative [model identity taxonomy](docs/TAXONOMY.md).

Every task attempt is logged **automatically and locally** to `~/.ringer/runs.jsonl` — no setup, no account, nothing leaves your machine. Each row carries the per-attempt verdict straight from the EXECUTED check, plus duration, tokens, the resolved `model`, the task's `task_type` (if the manifest set one), and the `retry` number.

Read it with:

```bash
./ringer.py models          # per-(model, task_type) scoreboard across the local log
```

The scoreboard reports, per model and task_type: tasks, attempts, `pass_rate`, `first_try_pass_rate`, median duration and token count, and `last_seen`. The signal for routing is `first_try_pass_rate` — the share of tasks that passed on attempt 1 without a retry; `pass_rate` is the rescued rate after Ringer's single retry, so the gap between the two is the cost of the retry lane. Slice the log with `--log` (a different JSONL), `--task-type`, `--model`, `--engine`, `--since`, or `--json` for piping elsewhere.

History from before the `model` / `task_type` / `retry` columns existed can be seeded in one pass:

```bash
./scripts/backfill_model_log.py \
  --log ~/.ringer/runs.jsonl \
  --runs-dir ~/.ringer/runs \
  --mapping mapping.json
```

The `--mapping` file joins old log rows to a `task_type`. Each line uses one of three key forms, applied in order:

- `run_id:task_key` — names one task in one run (most specific).
- `run_id` — names every task in that run.
- `name:prefix` — names every task whose key begins with `prefix`, across all runs (least specific, the usual way to cover a whole kit's keys).

Rows that match nothing keep their old `task_type` (empty); rows whose run-state JSON can't be found keep their old `model`.

`docs/MODEL-NOTES.md` is where the human-readable judgment lives on top of these numbers — the scoreboard tells you the pass rates; the notes tell you why a model shines or chokes on a given task shape.

### Evidence-based routing

The scoreboard only knows models you've already run. To reason about models you *haven't* tried yet, Ringer keeps a local snapshot of the OpenRouter catalog and a change log alongside the runs log:

```bash
./ringer.py catalog                  # fetch/refresh ~/.ringer/openrouter-catalog.json
```

| Flag | What it does |
|---|---|
| `--refresh` | Force a re-fetch even if the snapshot is fresh |
| `--source URL_OR_PATH` | Pull from a non-default URL or local file instead of the live OpenRouter API |
| `--file PATH` | Read a catalog document you already have on disk, no network |
| `--free` | Filter to models with a $0 price — promo models included |
| `--changes` | Print the recorded add/remove/price_change/went_free/went_paid events from `.changes.jsonl` |
| `--json` | Emit the snapshot (or, with `--changes`, the event log) as JSON for piping |

The snapshot lives at `~/.ringer/openrouter-catalog.json`; the change log sits beside it as `~/.ringer/openrouter-catalog.changes.jsonl`, appending one row per added, removed, price-changed, went-free, or went-paid event between snapshots. Free promotions are catalog signals, not automatic experiment slots or permission to spend.

Catalog fetches are throttled to once per 24 hours. A `run` triggers that refresh in the background on its way up; it never blocks or fails a run — if the fetch is slow or the network is down, Ringer carries on with the snapshot it has. The throttle and the auto-refresh-on-run are both documented in `./ringer.py run --help` and can be turned off there.

Before selecting an OpenRouter candidate, review the current catalog and
task-family evidence. If capability assessment is missing or older than 24
hours, refresh it before selection:

```bash
python3 scripts/assess_openrouter.py --refresh --out ~/.ringer/model-assessment
```

See [model evaluation](docs/MODEL-EVALUATION.md). Automatic weekly catalog-only
refresh uses zero inference; it does not run probes or benchmarks, authorise
API spending or replace capability assessment before selection. Catalog
compatibility does not prove best quality. If refresh or relevant evidence is
unavailable, do not silently select a stale candidate. The run's non-blocking
catalog fetch does not satisfy this pre-selection requirement.

`models --explore` supplies optional candidates rather than a compulsory lane:

```bash
./ringer.py models --explore                 # tiers across all task types
./ringer.py models --explore --task-type docs # tiers for one task shape
```

Models with local evidence are sorted into tiers:

- **proven** — at least 30 distinct jobs in the same named task family, with first-try pass rate at least 75% and final-check pass rate at least 85%. This is an evidence label, not human promotion.
- **probation** — some attempts logged but not enough volume or not enough first-try passes. Use it; don't lean on it.
- **untested** — nothing in the log yet. Pulled from the catalog: text→text, 32k+ context window, up to 10 candidates, FREE models first then cheapest. These are your audition queue.

Exploration is optional when it benefits the job, fits existing authority and has a strong executed check; there is no forced exploration task per batch. Candidates remain provisional while evidence accumulates. Repeated rounds of one job, mixed families, identity mismatches and unattributed rows cannot meet the 30-job floor. Keep a stable `job_id` across rounds and inspect per-family `routing_evidence`. A first 20-case microbenchmark is preliminary. Human review is required before global promotion, regardless of the statistical label; a worker cannot create its own approval receipt. The historical three-job/67% ladder is superseded.

The per-user philosophy, stated plainly: every user's workload is different, so the scoreboard learns what works for *your* tasks on *your* machine. A model that's proven in someone else's log is untested in yours until you've run it. The numbers are not portable between users, and the routing recommendations get personal as the log grows — which is exactly why the catalog and the change log stay local and the explore tiers are computed from your own `runs.jsonl`, not from anyone's aggregate.

## Steering profiles

Ringer can optionally load per-model steering profiles, prepend applicable worker rules to both first-attempt and retry prompts, print driver guidance for the orchestrator, and collect one local observation row per attempt. The feature is fail-open: missing or malformed steering data never blocks a run. Setup, the profile contract, and the observation schema are documented in [`docs/STEERING.md`](docs/STEERING.md).

## Hard-won invariants

Four rules are baked into every worker invocation. They all cost us real debugging hours; you get them for free:

1. **stdin is always closed** (`< /dev/null`) — headless CLI agents hang forever waiting on a TTY that isn't there.
2. **Sandbox mode is always explicit** — default sandboxes silently resolve to read-only in temp directories and block every artifact write.
3. **Verification executes the artifact** — an agent's own "done" is not evidence. Exit codes are.
4. **Raw output only** — logs and eval rows carry verbatim worker output, never a summary. Anything that needs judgment reads the raw data.

## Contributors

Every community PR that lands in main is credited here — that's a project rule, enforced by a test. Thank you:

- [@thebytorsnowdog](https://github.com/thebytorsnowdog) (Ian Dunsmore) — maintains the [thebytorsnowdog/ringer](https://github.com/thebytorsnowdog/ringer) fork
- [@Fiddlehead-MB](https://github.com/Fiddlehead-MB) (Melinda Byerley) — exact-byte demo checks, explicit newline instructions, and regression coverage (#101)
- [@oceanonline](https://github.com/oceanonline) — portable `python3` in template checks + lint quickstart path fix (#24)
- [@davekopecek](https://github.com/davekopecek) (Dave Kopecek) — committed the design-reference fixture so the design-token guard runs on every machine (#30)
- [@snapsynapse](https://github.com/snapsynapse) (Sam Rogers) — graceful shutdown on SIGINT/SIGTERM with worker-tree cleanup and finished state, plus the 14-test end-to-end CLI regression suite (#4)
- [@mlava](https://github.com/mlava) (Mark Lavercombe) — named setup failures across every diagnostic surface (#37), `run --baseline`, the no-workers check preflight (#38), guidance on check-writing failure modes (#57), early warnings for missing worker commands (#59), and preserving fix-swarm patches across retries (#56)

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the philosophy and what gets a PR merged fast. The short version: small and scoped, rebased on current main, every claim backed by an executed test. Authorship is always preserved — where a maintainer pushes a mechanical fix to your branch, you remain the commit author.

## License

[PolyForm Shield 1.0.0](LICENSE.md) — free to use, modify, and share, including inside your own commercial work. The one thing you can't do is offer Ringer or Ringside (or a derivative that competes with them) as a product or service of your own. Commercial rights to the tool itself belong to Nate Jones Media LLC.

## Requirements

- Python 3.12+ (stdlib only; `psycopg` needed only for the optional Postgres eval backend)
  - **Changed:** the supported floor moved from 3.11 to 3.12. CI has only ever run 3.12, so 3.11 was a promise nothing enforced — the honest fix is to state the version we actually test. Today's code still happens to run on 3.11; that is no longer guaranteed, and 3.11 breakage won't be treated as a bug.
- At least one agent CLI (Codex works out of the box)
- Rust toolchain, only if you're building Ringside from source

![Between rounds](docs/between-rounds.png)

---

Built by [Nate Jones](https://natejones.com) and maintained by [LEJ](https://limitededitionjonathan.com) — a Claude orchestrator wrote the specs and reviewed the diffs, Codex swarms wrote the implementation, and this repo's own eval table caught its first three bugs. The tool is its own proof of concept.

## Fork additions

This is [thebytorsnowdog/ringer](https://github.com/thebytorsnowdog/ringer), a fork of [NateBJones-Projects/ringer](https://github.com/NateBJones-Projects/ringer). Its `main` mirrors upstream; its `ian` branch is upstream plus Codex as a coordinating host, `--writable-root` for sandboxed OpenCode workers, a GLM 5.3 Flash registry entry, cost, billing, reliability and model-routing evidence (with a required pre-dispatch model-fit gate), the `ringer-discover` companion and a mission-fit gate for agents. See [docs/fork/README.md](docs/fork/README.md).
