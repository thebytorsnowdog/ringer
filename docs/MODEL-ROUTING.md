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

## Automatic use and subscription routing

Use Ringer whenever the workflow benefits from it, without asking permission
or requiring model-selection confirmation. Current Codex remains coordinator
for specs, checks, routing and review; Codex subscription workers are allowed.
For complicated tasks prioritise suitable already-authenticated subscription
models through their native configured harness. Read local configuration and
observed auth/quota, then choose the exact locally available model and supported
effort per task. Record explicit `billing_route: subscription` and
`service_tier: standard` for subscription tasks.

Choices within existing authority are coordinator decisions. Ask only for
genuinely missing authority. Preserve executable checks, sandbox confinement,
source integrity, raw logs and artifact-bound human review receipts. Model-fit
assessment is required before dispatch; assessment is NOT user approval.
No silent paid API or Fast fallback, top-up, credential change or budget-cap
change is authorised by this policy. Honour an explicit inline request;
otherwise a single delegated task is a one-task manifest.

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

`ask` and real-worker `demo` follow the same boundary. First run the command
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
is no automatic paid fallback or top-up. Current API caps are £0, alongside
the shared £90 subscription fee. The earlier job-specific Astra subscription
fit-assessment permission is historical and superseded by the subscription
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
silently select a stale candidate. Use `engine: opencode` and an explicit
current `model: openrouter/<exact-model-slug>` after review; no implicit default
or model override hidden in engine arguments. Paid candidates still need
existing API authority and unchanged caps.

Exploration is optional when useful to the job within existing authority;
there is no compulsory exploration slot or user model-selection confirmation.
The evidence floor is 30 distinct jobs in the same named task family, at least
75% first-try and 85% final-check pass rates. Repeated rounds of one job, mixed
families, mismatched identities and unattributed rows do not meet the floor.
Keep a stable `job_id` and inspect per-family `routing_evidence`. A statistical
label is separate from human quality review; global promotion still needs human
review, and a worker cannot create its own approval receipt.

## Historical OpenRouter probation policy (17 September 2026)

Use deterministic tools first. The current GPT-6 Codex model remains the
coordinator/reviewer. These restrictions govern OpenRouter exploration and
implementation; they do not activate a global route. Subscription priority
supersedes the former OpenRouter-first preference. Retain these historical
reliability restrictions unless fresh assessment and executed evidence justify
a change within existing authority.

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
jobs in a task family, 75% first-try and 85% final-check pass rates, plus
human review before global promotion. Existing API authority and budget
limits still apply.

## Evidence retained

The validated per-task assessment and binding are written to run state and each
attempt journal entry. The simple preview and run artifact show the selected
engine, model, effort, service, billing route and rationale where available.
Review those records alongside the executed check and the actual worker log.
They are evidence of a matching recorded decision, not deployment or human
acceptance.
