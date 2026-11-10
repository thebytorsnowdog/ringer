# Model routing and the pre-dispatch assessment

Ringer requires a recorded model-fit assessment before every Ringer model job,
including one-task jobs, `ask`, `demo`, reviews, probes and repairs. The gate
proves that the recorded decision matches the command Ringer is about to run.
It does not prove that the model is objectively best or that a human approved
the result. Assessment is the coordinator's judgement, not a separate model
call or user-approval step.

Deterministic scripts run directly outside Ringer need no model call. Inside
Ringer, only the exact bundled offline mock command may skip assessment.
Unnamed, custom or apparently non-model wrappers must be assessed, or the job
stops. Historical manifests remain readable, but they need a fresh assessment
before a new execution; historical data reads are unaffected.

## Automatic use and capability-aware subscription routing

Use Ringer whenever the workflow benefits from it, without asking permission
or requiring model-selection confirmation. The model or harness starting the
request coordinates specs, checks, routing and review. Subscription tasks use
`billing_route` `subscription` and `service_tier` `standard`.

Independent tasks are a swarm. The coordinator routes by `registry/delegation-matrix.json`. `route_from_table()` chooses the engine from `task_class`, and `admit()` passes that class into the paid check. `assess_explicit_route` then keeps that engine or blocks it. The runner does not call `choose_unspecified_route`, and it does not move a stopped task onto another engine. A task that omits `engine` and names a `task_class` is `auto` until the table chooses. A task that omits both stays on `codex`.

Subscription work names a native engine and `billing_route` `subscription`. Codex is the native engine with a subscription probe. Subscription auth is ChatGPT login (`forced_login_method="chatgpt"`, account type `chatgpt`). That login is separate from an OpenAI API key and from OpenAI API credit balance. An API task sets `billing_route` `api` and needs observed `api_key` auth, plus a billing policy file.

- **Codex, OpenAI models:** `engine` `codex`. When a billing policy is loaded, this is the native subscription route admission can start. Codex does not require OpenCode.
- **Grok, xAI models:** `engine` `grok` after `grok login` on a SuperGrok or X Premium Plus plan. That OAuth session does not authenticate OpenAI, Gemini, or OpenRouter. With a billing policy loaded, the task stops before a worker starts, because no subscription probe is implemented. The published CLI reference has no headless status that separates that login from an `XAI_API_KEY` session. A cleared `XAI_API_KEY` or the engine name is not subscription evidence. Ringer does not read `~/.grok/auth.json`. Without a billing policy, this probe gate is off, and a Grok task with subscription billing can still start. It does not reserve paid spend. A Grok `billing_route` of `api` is refused with or without a policy file, because this repository defines no API-key route for Grok.
- **Gemini / Antigravity, Google models:** `engine` `gemini` via `engines/gemini_worker.py`, which execs `agy` and removes `GEMINI_API_KEY` and `GOOGLE_API_KEY`. The `gemini` binary is not this harness. With a billing policy loaded, the task stops before a worker starts, because no subscription probe is implemented. Documented sign-in is the OS keyring or an interactive browser flow, and there is no headless auth-status command. The keyring is not read. Without a billing policy, this probe gate is off, and a Gemini task with subscription billing can still start. It does not reserve paid spend. A Gemini `billing_route` of `api` is refused with or without a policy file, because this repository defines no API-key route for Gemini.
- **Claude is not a route.** There is no Claude subscription. `engine` `claude` and an Anthropic model are refused with or without a billing policy. The name remains in `ringer_capabilities.HARNESSES`. `install-agent` requires `--codex` and does not write `.claude`.
- **Hermes is retired.** It is not an engine. Notes that mention it record older runs.

A Codex run does not look for `opencode`. `coordination` and `consequential` stay on the subscription harness. `bounded-detail` can use the OpenRouter GLM/DeepSeek shortlist. On `bounded-detail`, an explicit shortlist model may only confirm the cheapest model or name a cheaper or equal one. At the stored catalogue prices that cheapest model is `openrouter/z-ai/glm-5.3-flash`. A missing `task_class` is not `bounded-detail`. Paid tasks need `task_class`. `ask` takes `--task-class`.

## Coordinator rule discovery

The Mac apply projected the canonical `.agents/skills/ringer` text into two existing directories: `~/.codex/skills/ringer` and `~/.agents/skills/ringer`. That is the home skill projection. It does not establish that every coordinator loaded those files.

Grok rule discovery cannot be claimed from that projection. The inventory found `~/.grok` with no skills directory. Nothing in this repository shows that the Grok CLI reads `~/.agents/skills` or `~/AGENTS.md`. A grok binary and a `grok models` exit do not show that the updated skill was loaded, and they are not subscription evidence. `~/.grok/auth.json` is not read. Auth state is unknown.

Antigravity rule discovery cannot be claimed from that projection. No Antigravity or Gemini skill directory was written. `engines/gemini_worker.py` execs `agy` and removes `GEMINI_API_KEY` and `GOOGLE_API_KEY`. The documented sign-in is the OS keyring or an interactive browser flow, and the command list has no headless auth-status. The keyring is not read. Auth state is unknown. That is not a claim that the user is logged out, and it is not a claim that `agy` loaded the Ringer skill.

`~/.claude` was not present for a skill projection. `pstack-ringer-defaults` was left in place. `~/AGENTS.md` and `~/.codex/AGENTS.md` were already telling harnesses to route through Ringer, and those files were not rewritten. Claude is refused with or without a policy file.

## Paid dispatch and fallback

**No automatic OpenRouter fallback.** A usage cap, missing binary, auth failure, or provider error stops the named route. Ringer does not rewrite the task onto `openrouter` or `opencode`, does not reserve OpenRouter spend for that failure, and does not treat `automatic_paid_fallback` or a budget cap as approval to switch engines. `select_openrouter_fallback_model` is not called by the runner.

The delegation table in `registry/delegation-matrix.json` chooses the engine from `task_class`. `coordination` and `consequential` stay on the native subscription harness. `bounded-detail` keeps a flagship model on that harness, and otherwise uses `engine` `openrouter` with the cheapest shortlist model whose worst case fits £0.25. An explicit shortlist model may only confirm that choice or name a cheaper or equal one. A missing `task_class` is not treated as `bounded-detail`. OpenCode is never selected. A missing billing policy still means no paid dispatch. Tell the user the route stopped. Do not install a CLI for them.

Paid OpenRouter dispatch requires every item below. Miss one and the attempt does not start.

- `engine` is `openrouter`. Naming `opencode` can pass `assess_explicit_route` and is then refused by `prepare_openrouter_dispatch`.
- `billing_route` is `api`.
- `task_class` is `bounded-detail`. `admit()` passes that class into the paid check.
- The model is on the approved shortlist: `openrouter/z-ai/glm-5.3-flash` or `openrouter/deepseek/deepseek-v4.1-flash`. The unprefixed aliases of those two ids are accepted. Any other slug is refused, including a `:nitro` suffix. An explicit model may only confirm the table's cheapest choice or name a cheaper or equal shortlist model.
- A billing policy file is loaded through `[billing].policy_path`. No policy file means no paid dispatch. The same refusal covers `billing_route` `api` on any engine, and engines `opencode` and `openrouter`, before a worker starts.
- The policy sets `task_api_cap_gbp` and `paid_retries` to false. Attempt 2 is refused. There is no automatic paid retry.
- The reservation is the worst case of the named token bounds, at most 200,000 input tokens and 64,000 output tokens, priced from the stored catalogue and a USD to GBP rate dated within 14 days. That figure must fit the task allowance, the policy caps, and the 8 November 2026 approval caps: £10 in a month, £1 for one swarm run, and £0.25 for one task. A lower policy cap still binds. A higher policy cap does not raise the approval caps.

The worker is `engines/openrouter_worker.py`: one chat completion, no tools, no redirect, and no retry. It does not depend on the Claude binary. `declared_runtime_dependencies` returns no extra CLI for a bin named `openrouter_worker.py`. `route_from_table()` calls `select_bounded_worker` for a `bounded-detail` shortlist route. Tell the user the route stopped. Do not install a CLI for them.

Choices within existing authority are coordinator decisions. Ask only for
genuinely missing authority. Preserve executable checks, sandbox confinement,
source integrity, raw logs and artifact-bound human review receipts. Model-fit
assessment is required before dispatch; assessment is NOT user approval.
Honour an explicit inline request; otherwise a single delegated task is a
one-task manifest.

This policy supersedes historical OpenRouter-first/OpenCode-only routing,
no-Codex-worker rules and job-specific subscription restrictions. Historical
model notes remain evidence of their original runs, not current routing orders.

## Required workflow

1. Write the task specification and executable check. Assess complexity and
   risk, the deterministic or cheaper alternative, selected model and effort,
   bounded context, verification, escalation, billing/access and uncertainty.
2. Generate a zero-model draft bound to those exact task records:

   ```bash
   ./ringer.py assess manifest.json --coordinator codex-orchestrator --output assessed.json
   ```

3. Fill every blank judgement field, and make the strategy identify the
   current coordinator model and effort. Keep short tasks short, but record a
   real reason and alternative. Do not copy a reason from another task.
4. Run `./ringer.py lint assessed.json`, then
   `./ringer.py run assessed.json --dry-run` and inspect the resolved command.
5. Run the assessed manifest. Ringer validates every task before it creates a
   worker process or reserves paid spend.

Changing execution-relevant task content, including `spec` or `check`, changes
its canonical SHA-256 `binding`. The binding is computed from the normalised
execution record, not arbitrary raw manifest JSON. Regenerate and reassess
after a change, including changed execution content in a retry or repair.
`max_parallel` and serialisation preserve the binding because they do not
change a task. Argument or config overrides cannot silently replace the
assessed model, effort or service tier. An engine template that cannot prove it
forwards the selected model and effort is blocked.

`ask` and real-worker `demo` follow the same boundary. `ask` takes `--task-class`. A paid shortlist question needs `bounded-detail`. First run the command
with `--dry-run` to print a source- or task-bound assessment draft, save and
fill that JSON, then pass it with `--assessment`. Dashboard flags and
`--allow-noncanonical-route` do not bypass the gate.

## Schema

The root `model_assessment` object uses schema
`ringer-model-assessment/v1` and contains:

- `coordinator`: the identity responsible for the recorded judgement.
- `strategy`: the job-level routing approach, including any model mix.
- `tasks`: exactly one assessment for each real model task.

Each task assessment requires these non-empty string fields:

| Field | Meaning |
|---|---|
| `binding` | Generated SHA-256 binding to the normalised execution-relevant task record. |
| `engine` | Selected configured engine, matching the resolved command. |
| `model` | Exact selected model identity. |
| `effort` | `minimal`, `low`, `medium`, `high`, `xhigh` or `max`. |
| `billing_route` | Explicit `subscription` or `api`. |
| `service_tier` | `standard` or explicitly authorised `fast`. |
| `rationale` | Why this route fits the task. |
| `alternative_considered` | Cheaper model or deterministic method considered. |
| `context_plan` | What bounded input the worker receives. |
| `verification` | Independent check or read-back used to judge the output. |
| `escalation` | Conditions that stop or return the task to the coordinator. |
| `evidence` | Evidence supporting the provisional choice. |
| `uncertainty` | What the routing evidence does not establish. |

Unknown fields, missing fields, empty strings, booleans and nulls fail closed.
A complete reusable example is in
[`templates/model-assessment/manifest.json`](../templates/model-assessment/manifest.json).

## Provisional routing policy

- Deterministic tooling first for exact extraction, comparison, conversion and
  validation.
- Luna at low or medium effort for small mechanical work with an independent
  check.
- Terra at medium effort for bounded everyday implementation.
- Sol at medium or high effort for established complex implementation,
  integration and consequential accounting.
- Astra at medium effort for coordination and architecture, high for complex
  diagnosis, and xhigh or max for the hardest bounded unresolved decisions.

Astra worker efforts are `low`, `medium`, `high`, `xhigh` and `max`;
`minimal` and `none` are unsupported.

These are starting assignments, not benchmark conclusions. Explicit task
overrides remain available when the assessment records the reason and evidence.
Astra's Ringer performance is unproven. Keep coordinator selection explicit
per job and do not turn it into a global model switch.

`ultra` is Codex automatic delegation, not a documented routine API effort. It
is blocked for Ringer workers until delegation ownership and accounting are
verified. Standard service is the default. Fast is a separate paid choice and
requires existing explicit authority plus `allow_fast_service = true`; effort
never implies Fast. Ringer does not auto-route or fall back to another model,
billing route or service tier.

An authorised Astra subscription selection still depends on observed ChatGPT
authentication and quota at each run. Use standard service and record the
selected effort explicitly, so a host's high effort is not inherited. There
is no automatic paid fallback or top-up. The example policy sets the monthly
API cap to £0. Record any subscription fee in your local cost policy. The earlier
job-specific Astra subscription fit-assessment permission is historical and superseded by the subscription
priority above. If access, authority or evidence is missing, stop with
`BLOCKED`. Assignments remain provisional.

## OpenRouter evidence before selection

For OpenRouter candidates review the current catalog and task-family evidence.
If capability assessment is missing or older than 24 hours, refresh it before
selection:

```bash
python3 scripts/assess_openrouter.py --refresh --out ~/.ringer/model-assessment
```

See [model evaluation](MODEL-EVALUATION.md). Automatic weekly catalog-only
refresh uses zero inference: it does not run probes, authorise API spending or
replace capability assessment before selection. Catalog compatibility does not
prove best quality. If refresh or relevant evidence is unavailable, do not
silently select a stale candidate. Paid dispatch uses `engine` `openrouter`
and one shortlist id from `docs/DELEGATION-MATRIX.md`. There is no implicit
default, and no model override hidden in engine arguments. Paid candidates
still need a billing policy file and the approval caps.

Exploration is optional when useful to the job within existing authority;
there is no compulsory exploration slot or user model-selection confirmation.
The evidence floor is 30 distinct jobs in the same named task family, at least
75% first-try and 85% final-check pass rates. Repeated rounds of one job, mixed
families, mismatched identities and unattributed rows do not meet the floor.
Keep a stable `job_id` and inspect per-family `routing_evidence`. A statistical
label is separate from human quality review; global promotion still needs human
review, and a worker cannot create its own approval receipt.

## Historical OpenRouter probation policy (17 September 2026)

This section records the restrictions written on 17 September 2026. It is not the live paid shortlist. The live shortlist is the 8 November 2026 approval in `docs/DELEGATION-MATRIX.md`: `openrouter/z-ai/glm-5.3-flash` and `openrouter/deepseek/deepseek-v4.1-flash`, on `engine` `openrouter`, inside £10 a month, £1 a swarm run, and £0.25 a task, with no paid retry. The September line that kept DeepSeek disabled does not apply to that flash id.

On 17 September 2026 the recorded restrictions were:

- MiMo, exact model `openrouter/xiaomi/mimo-v2.5`, only for small isolated implementation, one attempt, and an explicit positive `task_spend_allowance_gbp`, through `engine` `opencode`.
- GLM 5.3 Flash only for cost-first, non-urgent work.
- DeepSeek v4.1 Flash disabled until a fresh no-fallback provider smoke.

Those lines are not the dispatch rules now. Admission refuses `engine` `opencode`. MiMo is not on the shortlist. None of these models is globally promoted until there are 30 distinct real jobs in a task family, 75% first-try and 85% final-check pass rates, plus human review before global promotion.

## Evidence retained

The validated per-task assessment and binding are written to run state and each
attempt journal entry. The simple preview and run artifact show the selected
engine, model, effort, service, billing route and rationale where available.
Review those records alongside the executed check and the actual worker log.
They are evidence of a matching recorded decision, not deployment or human
acceptance.
