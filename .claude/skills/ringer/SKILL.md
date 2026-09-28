---
name: ringer
description: >-
  Orchestrate verified work with Ringer. Load before model-backed probes,
  smoke tests, simulations, graders, persona harnesses or CLI agents;
  edit-test-edit loops or batches of edits; reviews of failed worker output;
  manifest writing, model-fit assessment, engine selection or run diagnosis.
  Assess model fit before dispatch, then delegate implementation with an
  executable check. A single task is a one-task manifest; bounded read-only
  questions use ringer.py ask. Skip read-only file search, git operations,
  prose from existing context, conversation, and one one-file, few-line,
  one-shot edit. A second edit pass triggers Ringer.
---

# Ringer orchestrator playbook

## Read this first — the four rules that actually get broken

1. **You review; workers type.** Your lane: specs, checks, pattern choice,
   reading results. If you are typing implementation, running probes, or
   babysitting a retry loop yourself, you have left your lane.
2. **A single task is a one-task manifest.** Same verification, zero
   ceremony. "Too small for Ringer" is how drift starts — the smoke test,
   the probe script, the three-edit fix are all one-task manifests.
3. **Beware the tiny-edit death spiral.** The named anti-pattern: each step
   is individually small enough to justify inline, and two hours later the
   exception has become the workflow and nothing was verified or visible.
   The one-shot exception is ONE file, a few lines, ONCE. The second pass on
   the same problem is a loop, and loops are manifests.
4. **Runs are watched, not hidden — and the screen comes up FIRST.** The
   moment this skill loads for real work, before you write a single spec,
   put Ringside on the human's screen: `./ringer.py hud` (idempotent — if
   one is already up it says so and opens the page; runs also auto-start
   it). Ringside is the PAGE at http://127.0.0.1:8700 — NEVER launch the
   Ringside.app application (`open -a Ringside`); it is a parked prototype
   with a stale frontend. And never go dark: if your prep (research,
   check-writing, manifest drafting) will take more than ~30 seconds,
   tell the human in one sentence what you're doing and roughly how long
   before you start — they should be watching the empty arena and reading
   your one-liner, not wondering if anything is happening. Never pass
   `--no-dashboard` except in automated tests or when the user explicitly
   asks.

Ringer runs manifest tasks in parallel across cheap CLI workers (Codex,
OpenCode/GLM, others via config) and verifies every task by **executing a
check command**. A product PASS requires an executed passing check and the
declared file contract. Only product or missing-deliverable failures retry once
by default, with the actual check failure in the retry prompt. You —
the orchestrating model — pay tokens only for specs, orchestration, and
review.

```bash
./ringer.py assess manifest.json --output assessed.json
./ringer.py lint assessed.json            # always lint before running
./ringer.py run manifest.json --identity <who-you-are>
./ringer.py run manifest.json --dry-run   # print the plan, spawn nothing
```

Runs land in `~/.ringer/runs/`. Raw worker logs land in `<workdir>/logs/`.
Full reference: `README.md`. Ready-made manifest skeletons: `templates/`.
Lint catches unverifiable checks, silent checks, worktree deliverable/commit
loss, serial fan-out, write collisions, and underspecified specs; `run`
prints the same findings as non-blocking warnings.

## Assess model fit before work

Before every Ringer model job, including one-task runs, `ask`, `demo`, reviews,
probes and repairs, record an explicit model-fit assessment in the manifest.
This is the coordinator's judgement, not a separate model call or user
approval. Assess complexity/risk, deterministic or cheaper alternatives,
selected model and effort, bounded context, verification, escalation,
billing/access and uncertainty. Make the strategy or identity state the
current coordinator model and effort. Generate the bound zero-LLM draft with
`./ringer.py assess manifest.json --coordinator codex-orchestrator --output assessed.json`,
then fill every judgement field.

Lint and dry-run must accept the assessment and show the resolved route before
the run. Ringer checks every task's exact binding, engine, model, effort,
billing route and service tier again at the runner boundary before any worker
process or spend reservation. If execution-relevant task content or the
checker changes, regenerate and reassess. The binding is based on the
normalised execution record, not arbitrary raw JSON. Never use `engine_args`, config defaults,
`--allow-noncanonical-route` or dashboard flags to bypass the recorded route.
An engine template must forward the selected model and effort.

This gate proves a matching recorded decision. It does not prove the model is
best or create human approval. Ask for authority only when it is genuinely
missing. `ask` first uses `--dry-run` to produce its source-bound draft and then
requires `--assessment`; real demo workers have the same requirement. Only the
exact bundled offline mock may skip assessment. Unknown, custom or apparently
non-model wrappers assess or stop; scripts run directly outside Ringer do not
call a model. See `docs/MODEL-ROUTING.md` and
`templates/model-assessment/manifest.json`.

Use deterministic tooling first. Provisional Codex routing is Luna low/medium
for mechanical work, Terra medium for bounded everyday implementation, Sol
medium/high for established complex implementation, and Astra medium for
coordination/architecture, high for complex diagnosis, or xhigh/max for the
hardest bounded unresolved decisions. Record an explicit reason for an
override. Astra worker efforts are low, medium, high, xhigh and max; minimal
and none are unsupported. Astra performance under Ringer is unproven. Astra `ultra` is Codex
automatic delegation, not a routine worker effort, and remains blocked until
delegation ownership and accounting controls are verified. Standard service is
the default and a host's high effort is never inherited. Fast requires existing
explicit paid authority and is never inferred from effort. Require observed
ChatGPT authentication and quota for subscription routes. There is no
automatic paid fallback or top-up; current API caps are £0 alongside the
shared £90 subscription fee. Astra subscription fit assessment is authorised
for this work, not a global model change. Never auto-route or fall back to
another route.

## Bounded read-only questions: `ask`

Rule 2 holds for anything that changes a file, runs a build, or produces an
artifact worth checking. One workflow is lighter: the human asks a bounded,
read-only question over source you can already point at, and the answer is
prose. It is still a Ringer model job and still needs the source-bound
assessment; the lighter path is only the manifest shape.

```bash
./ringer.py ask "<the human's request>" --source /absolute/path/to/source \
  --model gpt-5.6-luna --reasoning-effort low --dry-run
```

The dry-run prints a source-bound JSON assessment draft. Save it, fill every
judgement field, then run the same command with `--assessment /path/to/assessment.json`
and without `--dry-run` for actual execution. No automatic route changes means
Ringer will not switch models at runtime without reassessment; the coordinator
selects the route before work begins.

`ask` selects the passages that match the request, caps the packet, spawns one
clean worker on it, and allows a single attempt. Repeat `--source` for several
files or directories; `--state` takes a small file of settled decisions;
`--dry-run` shows you the packet and spends nothing. If everything that matched
is too large for the packet it says so and stops before the model call rather
than letting a worker guess — but a source small enough to fit whole is sent
whole, relevant or not, so choosing the sources IS the work. Directory scans
stay inside the tree you name; a symlink leading out of it is skipped and
reported. Runs appear on Ringside like any other, and `--redact` hides the
request from Ringer's own state and eval records — it cannot scrub raw worker
output, which is captured verbatim by design.

**Be honest about what it verifies.** The check is that `answer.md` exists and
is non-empty. That is the weakest check in the tool, and it is also the best
available — there is nothing to execute against free-form prose. `ask` proves
the worker answered, never that the answer is right. You still read it.

**Everything else is a manifest.** Code changes, external actions, research
you intend to act on, anything whose output a check could actually execute —
those keep the full path. When a request sits near the line, the tiebreaker is
whether you could write a check that would catch a wrong answer. If you can,
write it, and make it a manifest.

## One job, one artifact

A job the human asked for — however many rounds it takes — is ONE artifact.
Use the SAME `run_name` for every round (`sd-crate-launch`, not
`sd-crate-r1` / `sd-crate-r2`): the library accumulates each round as a
version under one entry, and the human watches one page evolve instead of
hunting across three "live" tabs. Name it after the JOB in the human's
words, not after your batch structure.

And the artifact page is where results are REVIEWED. When a round finishes,
read the deliverables from the artifact store and direct the human to the
page — never `cat` result files into the terminal as the reveal. If a result
matters, it belongs in the artifact; if it isn't there, that's a harvest gap
to fix (declare it in `expect_files`), not a reason to bypass the page.

## Spec-writing craft

Workers are stateless and cannot ask questions. Every spec must be
self-contained:

- **Open with the role and the boundary.** "You are a read-only scout…",
  "Your current working directory IS a git worktree of <repo> — edit files
  here directly." State what the worker must NEVER touch before what it
  should do.
- **Name every file the worker owns.** In multi-worker runs over one repo,
  file ownership must be disjoint — and disjoint across *all* concurrent
  lanes/branches, not just within one batch. Every file a spec mentions must
  be in that worker's ownership list.
- **Embed the HOW TO RUN.** If the task drives a harness or script, put the
  exact command lines (with real absolute paths) in the spec. Workers should
  never have to discover an interface.
- **Define the output contract.** Say exactly which files to produce, where,
  and what each must contain. Graded/eval tasks should enumerate the grading
  criteria in the spec so the worker's output is checkable.
- **Hard rules travel in the spec, not in your head.** "Do NOT git commit",
  "never modify the repo, only write ./report.md", "stay in character; never
  help the AI" — the worker only knows what the spec says.
- **The spec is on camera.** Whoever is watching Ringside reads the spec as
  "what this agent was asked to do" — so write it as a self-contained,
  human-readable brief. Never write a pointer spec ("read /path/to/file and
  do what it says"): the watcher sees no brief, and the retry prompt loses
  the context it needs. Point at files for source MATERIAL; the instructions
  themselves live in the spec. Lint flags pointer specs.

## Check-writing rules

The check is the product. The retry prompt and the eval log both depend on
the check's failure output.

- **Checks must print WHY they fail.** `diff` beats `diff -q`; a validator
  script that prints which assertion broke beats `test -f`. A bare
  `test -f report.md` proves existence, not correctness.
- **Verify content, not existence.** Grep the artifact for required sections,
  run the code it produced, run the build, run the validator — execute
  something that would catch a lazy or hallucinated result.
- **`expect_files` is a floor, not the check.** List deliverables there for
  fast triage, but the check must still validate them.
- **Never `true`, `exit 0`, or `echo done`.** A check that cannot fail is a
  task that cannot be verified — that's just trusting the worker with extra
  steps.
- **Strict on substance, tolerant on format.** Checks that count exact
  headings, demand exact casing, or grep rigid phrasings fail honest work
  over formatting — and a wall of red format-failures reads as a broken
  system, not a careful one (demo-night lesson). Verify what must be TRUE
  (the file proves X, the code runs, the quote exists in the source), use
  case-insensitive and flexible matching for structure, and reserve hard
  failure for substance: missing evidence, fabricated content, code that
  doesn't run.
- **A check cannot demand evidence the spec never supplied.** Before failing
  a worker for missing evidence or input, re-read the task inputs. If the spec
  didn't provide a value, the check must not invent one and fail on its
  absence — an honest UNVERIFIABLE answer is not a failure. Reserve hard
  failure for what the spec actually asserted.
- **Executed checks catch laziness, not subtle wrongness.** A check that
  *runs* the artifact catches a plausible-but-wrong change far less often than
  it catches a missing one. Whenever a swarm touches a dogfood artifact
  (Ringer's own docs, config, or checks), add an "our own artifact passes our
  own validator" test so the checker exercises what it preaches. And keep
  orchestrator patch review mandatory regardless of PASS status — a green
  check is not proof of semantic correctness.

## Pattern playbook

Reach for a named pattern before inventing one. Skeletons in `templates/`:

| Kit | Use when |
|---|---|
| [review-swarm](../../../templates/review-swarm/) | You need broad read-only review coverage before deciding what to fix. |
| [fix-swarm](../../../templates/fix-swarm/) | You have confirmed independent fixes that can be split across isolated worktrees. |
| [focus-group](../../../templates/focus-group/) | You need isolated persona feedback on a product, pitch, prompt, or workflow. |
| [bakeoff](../../../templates/bakeoff/) | You need evidence for choosing a model, prompt, or configuration across shared scenarios. |
| [research-with-proof](../../../templates/research-with-proof/) | You need research backed by a proof task whose check executes the claim. |
| [launch-kit](../../../templates/launch-kit/) | You need a go-to-market package built across research, persona review, and final assembly rounds. |
| [asset-swarm](../../../templates/asset-swarm/) | You need media assets produced in parallel with executable checks for renders, batches, diagrams, or captures. |
| [adversarial-review](../../../templates/adversarial-review/) | You want several models to review the same artifact before the orchestrator synthesizes findings. |
| [repo-feature](../../../templates/repo-feature/) | You know what to build and need sandboxed workers to edit a real repo with build and git checks. |
| [migration-swarm](../../../templates/migration-swarm/) | You have mechanical codebase transforms that can be partitioned across worktrees. |
| [doc-swarm](../../../templates/doc-swarm/) | You need module docs with executed examples and checks against invented APIs. |
| [test-hardening](../../../templates/test-hardening/) | You need stronger tests by module while keeping production source edits off-limits. |
| [competitive-teardown](../../../templates/competitive-teardown/) | You need competitor research with citation allowlists and a synthesis phase. |
| [data-pipeline](../../../templates/data-pipeline/) | You need fetch, transform, and validate stages with executed validators and honesty rules. |
| [probe](../../../templates/probe/) | You need a one-task manifest for a smoke, probe, or post-mortem. |

Pattern-selection judgment:

- **Browse the catalog first.** Before writing any manifest, browse
  `templates/README.md`: choose a kit, mix pieces from several, or write
  your own having seen the prior art.
- **Review before fix.** Run a read-only review swarm, read the reports
  yourself, then compile the confirmed findings into a fix-swarm manifest.
  Don't let the same worker find and fix.
- **Personas must be separate workers.** Parallel personas in one context
  bleed into each other. One persona per task, one session dir per task.
- **Iterating on a prompt/product? Re-run the same panel.** A fixed persona
  panel across rounds tells you whether a change fixed what the panel
  actually complained about.
- **Probes, smokes, and diagnosis loops are manifests too.** A model-calling
  smoke test is a one-task manifest with the transcript as `expect_files`
  and a validator as the check. Diagnosing a failed worker's output is a
  read-only scout task. If it calls a model, it runs under Ringer — that is
  what makes it visible, verified, and logged.

## OpenRouter probation policy (17 September 2026)

Use deterministic tools first. The current GPT-6 Codex model remains the
coordinator/reviewer. These restrictions govern OpenRouter exploration and
implementation; they do not activate a global route.

- MiMo uses `engine: opencode` and exact model `openrouter/xiaomi/mimo-v2.5`
  only for small isolated implementation with a strong executable check.
  Allow one attempt, overriding the general retry guidance, and require an
  explicit positive `task_spend_allowance_gbp` after paid API authority.
- GLM uses exact model `openrouter/z-ai/glm-5.3-flash` only for cost-first,
  non-urgent work with an early write checkpoint and a clear stop condition.
- DeepSeek, exact model `openrouter/deepseek/deepseek-v4.1-flash`, stays
  disabled until a fresh no-fallback provider smoke is consistently stable.

No automatic fallback or top-up. Route changes require fresh assessment.
None of these models is globally promoted until there are 30 distinct real
jobs in a task family plus human review. Existing API authority and budget
limits still apply.

## Engine selection

**The engine choice belongs to the human — but the recommendation comes
from THEIR evidence.** Before the FIRST run of a job: read what's wired up
(`[engines.<name>]` blocks in `~/.config/ringer/config.toml`), run
`./ringer.py models --task-type <this job's type>` for the local scoreboard,
and glance at `./ringer.py catalog --changes` for anything newly free or
newly cheap. Then ask the user which model should do the typing — top 2–3
options with the NUMBERS in the pitch and a recommendation, e.g.: *"GLM is
6/6 first-try on persona work here at ~2¢/task — recommended. Codex is also
100% but ~8x the tokens. And kimi went free on OpenRouter yesterday — want
it auditioning one of the small tasks?"* Honor their pick via the per-task
`engine`/`model` fields; don't re-ask every round of the same job unless
the mix isn't working. This is per-user by design: the scoreboard learns
THIS user's workload — never import another machine's conclusions or
recommend from a different user's numbers.

**Explore or the scoreboard fossilizes.** Always recommending the proven
pick means never learning a new one. In any run of 3+ tasks that has a
low-stakes lane (docs sweeps, mechanical edits, persona reviews — strong
executed check, retry to absorb failure), assign roughly ONE task to an
exploration candidate from `./ringer.py models --explore --task-type <type>`
(untested + cheap or free, text-capable, decent context). Free promos from
`catalog --changes` jump the queue — a temporarily-free model is a zero-cost
experiment. Never explore on time-critical work, never with more than a
small slice of a batch, and name the experiment when presenting the engine
ask so the human can veto it. Promotion ladder (computed by --explore):
untested → probation → proven requires at least 30 distinct jobs in the same
named task family, first-try pass rate ≥75% and final-check pass rate ≥85%.
Mixed families, repeated rounds of one job, identity mismatches and unattributed
rows cannot meet the floor. A first 20-case microbenchmark is preliminary.
This is a statistical label, separate from human quality and promotion. Keep a
stable job_id across rounds and inspect the per-family routing_evidence.

**OpenCode is the harness; the model is a manifest field.** Unless a model
ships its own first-class harness (Codex does), it runs through the
`opencode` engine with the task's `"model"` field set to the OpenRouter
slug — e.g. `"engine": "opencode", "model": "openrouter/moonshotai/kimi-k2.7-code"`.
This holds even when someone — including the user, in the heat of a run —
says to "call kimi directly" or reach for the model's own CLI: the harness
is what provides the sandbox, raw logs, token counts, and executed
verification, so routing around it silently drops all four. Never clone an
engine block or splice `-m` through `engine_args` to change models; that's
what the `model` field is for, and a bakeoff is only real when the MANIFEST
names each competitor (2026-07-06 lesson: an engine block with a hard-coded
model ran one model under three competitors' names).

Engines are config blocks (`[engines.<name>]` in config.toml), selectable
per task via the manifest `engine` field. Defaults are deliberate:

- **codex** (public default): choose a model from the user's local evidence.
  Use per-task `engine_args`
  to set reasoning effort — spend it on hard tasks, not boilerplate.
- **opencode**: the universal lane — any OpenRouter model via the `model`
  field. Keep `model_default` empty and select an explicit assessed route
  under the probation policy above.
  Validate a model new to you with a trivial one-task manifest before
  trusting it with a batch.
- Small/flash-class models are the first to choke on long conversational or
  multi-turn harness tasks — watch their retry counts before scaling them.
- Match `timeout_s` to the task: conversational harness tasks and
  build-and-test checks need far more than file edits.
- **Check the evidence before assigning models to tasks.** Run
  `./ringer.py models` (optionally `--task-type <type>`) — the local
  scoreboard aggregating every executed-check outcome per (model,
  task_type): first_try_pass_rate is the routing signal; pass_rate includes
  retry rescues. Then read `docs/MODEL-NOTES.md` (in the ringer repo) for
  the judgment the numbers can't carry. Routing is grounded in performance,
  not vibes (Jon directive 2026-07-06).
- **"Show me the scoreboard" is one command.** When the human asks to see
  the model scoreboard, rankings, model costs, or "which models work best,"
  run `./ringer.py models --open` — it renders the full scoreboard (tiers,
  first-try rates, est. $/task, usage, MODEL-NOTES excerpts, free-promo
  watchlist) as a zero-LLM HTML page in the artifact library and opens it
  in their browser. Costs no tokens; never hand-summarize the numbers when
  the page can show them.
- **Give every task a `task_type`** (canonical vocabulary in the README —
  code-feature, code-fix, code-review, research, persona-review, site-build,
  image-gen, docs, probe, bakeoff, ...). Untyped tasks bucket as (untyped)
  and teach the scoreboard nothing; lint nudges you when it's missing.

## Worktrees-mode footguns (learned the hard way)

Run-level `"worktrees": true` gives each task an isolated git worktree of
`repo`, detached at HEAD. Three consequences:

1. **Passing tasks get their worktree DELETED.** Deliverables must land
   outside the task worktree, or the check must export them first.
2. **Worker commits die with the worktree.** Pattern that works: the worker
   leaves changes uncommitted; the check runs
   `git add -A && git diff --cached > <path-outside-worktree>.patch` and
   validates the patch. You apply and commit on your branch after review.
3. **Logs survive** (they go to `<workdir>/logs/`), so post-mortems work
   even on deleted worktrees.
4. **Gitignored outputs silently vanish from patch exports.** `git add -A`
   cannot stage ignored files (build dirs like `dist/`), so a worker's edits
   there pass its checks, export an incomplete patch, and die with the
   worktree. If a task touches any gitignored path, the check must `cp`
   those files to a path outside the worktree explicitly — verify the patch
   AND the copies before trusting the run.

And on your own side of the fence: when integrating patches into the real
repo, stage specific paths — never `git add -A` in a checkout that may hold
someone's untracked scratch files.

## Post-run review ritual

1. Read the run JSON in `~/.ringer/runs/` — statuses, retries, durations.
2. For any retried or failed task, read the raw worker log in
   `<workdir>/logs/` before deciding anything. Retries that passed on
   attempt 2 often reveal a spec ambiguity worth fixing in your next
   manifest.
3. Spot-check at least one PASSING task's artifact per run. The check
   catches most laziness; you catch the rest.
4. Failures with useless error messages mean your CHECK needs work, not
   (only) the worker.
5. **Update `docs/MODEL-NOTES.md`** (in the ringer repo) when a run taught
   you something about a model: one dated line under the model — task type,
   what happened (attempts, tokens, failure mode), what you'd do
   differently. Only what the executed checks and raw logs support. The raw
   numbers took care of themselves — every attempt already landed in the
   local model log (`./ringer.py models` to see the updated scoreboard).

## Spend your own context deliberately

The scoreboard exists so that worker tokens buy evidence. Your own tokens are
not free either, and nothing in the tool constrains them:

- **Reach for code before a model.** Counting, sorting, exact-text search,
  field extraction, format conversion, file comparison, validation — `rg`,
  `jq`, a parser, a two-line script. A model imitating `grep` is an expensive
  way to get a worse `grep`.
- **Select passages; don't load files.** Search first, then read what matched.
  Loading a whole transcript because the answer is somewhere inside it is how
  a cheap question turns expensive. `ask` does this for you; when you are not
  using `ask`, do it by hand.
- **Load a tool when the job needs it** — not every connector and schema at
  the top of a session on the chance that one gets used.
- **Answer the question that was asked.** A sentence when a sentence was asked
  for. No process diary, no restating the human's request back to them, no
  unrequested options.
- **Never retry into a limit.** A token- or usage-limit failure is not a
  transient error; retrying it just burns the budget faster. Reduce the input
  or take a cheaper path.

When you claim a saving, count the whole job — every call, including your own
planning and review. Moving tokens from your context into a worker's is only a
saving if the total came down.

## Admission, accounting and review

The user's chosen engine/model policy controls this portable skill. Subscription
and paid API routes are separate choices; an engine name or missing token count
does not establish that a call is included. Billing is off in the public default.
A coordinator can enable `[billing].policy_path` to a reviewed local JSON policy.
Each task then names `billing_route` (`subscription` or `api`) and an explicit
model. Paid API attempts also need a finite positive `task_spend_allowance_gbp`
and available monthly/run reservations. Never switch routes automatically.

With billing enabled, the runner checks admission before every actual attempt,
including retries. Codex subscription needs fresh observed ChatGPT auth/quota
and a command that forces `forced_login_method="chatgpt"` and forwards the exact
model. Paid Codex needs observed API-key auth and forces `api`. Explicit paid
OpenCode tasks require one exact OpenRouter model argument and budget admission;
they never use subscription quota. Unsupported engines block admission.
Sample Codex config uses `--json` and
`{model_args}`; JSON-only identity is command-attested unless the provider reports
its model. Usage estimates are not invoices. Unknown or interrupted paid attempts
keep their reservations unresolved; only provider actual cost with dated FX can
settle them. No personal subscription price or universal host model policy is
part of this portable skill.

Use finite positive `preflight_timeout_s` and `check_timeout_s` (defaults 15 and
60 seconds), with a deliberate checker allowance. Product-only retry is the
default. Quota, permission and interruption never retry. A pre-spawn block has
zero model attempts; a blocked retry retains the earlier attempt evidence.

Read `ringer.py outcomes --job-id JOB --read-back --json` after a run. Joined
state, attempt and lifecycle evidence must account for all tasks. Read-back checks
exported bytes and hashes, including explicitly declared ignored files. Product
PASS, export PASS and human promotion are separate: a worker never creates its
own human approval receipt. The coordinator reviews before use.

References: [reliability](../../../docs/RELIABILITY.md),
[subscription/API accounting](../../../docs/SUBSCRIPTION-COSTS.md), and
[model comparison](../../../docs/MODEL-COMPARISON.md), relative to this skill.

## Baked-in invariants (preserve in any change to ringer.py)

Stdin closed (`< /dev/null`); sandbox mode explicit; verification executes
the artifact; logs carry raw worker output only. These are load-bearing —
engine and invocation changes must keep all four.
