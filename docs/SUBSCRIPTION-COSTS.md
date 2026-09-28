# Subscription and cost accounting

`ringer_costs.py` provides local accounting and billing preflight interfaces.
`ringer.py` calls the preflight before each admitted attempt, while
`scripts/ringer_cost_report.py` reads journals, state and referenced worker
logs. The accounting helpers use only the Python standard library. They do not
read credentials, change authentication or make network calls.

The model-fit gate runs before any worker process or spend reservation. Billing
admission remains a separate boundary and retains the existing caps and route
authority. Executed tests establish only the behaviours they exercise. They do
not supply human approval, deployment evidence or a provider-enforced cap.

## Evidence and account assumptions

The supplied evidence was verified on 3 September 2026 against [Codex pricing](https://developers.openai.com/codex/pricing), [Codex authentication](https://developers.openai.com/codex/auth), [API pricing](https://developers.openai.com/api/docs/pricing) and the [OpenRouter model catalogue](https://openrouter.ai/api/v1/models). The exact Astra entries were added from the [API pricing](https://developers.openai.com/api/docs/pricing) and [ChatGPT credit pricing](https://learn.chatgpt.com/docs/pricing) sources dated 5 September 2026. This implementation does not refresh those sources automatically.

## Exact GPT-6 Astra accounting

Only the exact identity `gpt-6-astra` receives built-in Astra rates. Unknown or
alias identities remain `UNKNOWN`; Ringer does not infer their prices.

| Standard route | Input | Cache read | Cache write | Output |
|---|---:|---:|---:|---:|
| USD per million, request input at or below 272,000 tokens | 10 | 1 | 12.5 | 50 |
| USD per million, request input above 272,000 tokens | 20 | 2 | 25 | 75 |
| Standard short-request credits per million | 250 | 25 | 0 | 1,250 |

The long threshold is strict and applies per request: 272,000 input tokens is
short, while 272,001 is long. Aggregate usage without request boundaries is
reported as bounded short and long estimates. It is not priced as one long
request. Long or aggregate Astra credits, Fast API rates and Fast credits are
`UNKNOWN` until supported by explicit evidence. The GPT-5.6 2x Fast shortcut is
not applied to Astra.

These figures are estimates unless a provider supplies actual cost evidence.
Cash charged, calculated equivalents, subscription credits and the fixed
subscription fee remain separate categories.

## Account for the whole job

A route comparison should include the coordinator, every worker attempt,
retries, cached and fresh input, output, and the review needed to judge the
evidence. Moving tokens from the coordinator into a worker is not itself a
saving. Report the fixed subscription fee separately from API cash. Label
source-bound calculations as estimates, and leave unsupported values unknown.

The user confirmed GBP 90/month for ChatGPT Pro. That personal assumption is in `config/cost-policy.example.json`, not a library constant or the CLI's default policy. The account snapshot observed at `2026-09-03T08:23:39.686Z` reported:

- `planType=prolite`. The mapping to a published Pro tier is unverified.
- Codex primary window: 10,080 minutes, 74% used, resetting at **7 September 2026, 03:38:35 Europe/London**.
- No separate five-hour Codex window in that response. The other named model's window is not a substitute.
- Purchased credit balance `0`, `hasCredits=false`, no purchased resets reported, and `spendControlReached=false`.

The published Pro 5x local-message estimates per five hours are Sol 50–500, Terra 125–1,000 and Luna 1,250–10,000, with additional weekly limits. These ranges are estimates, not guaranteed entitlements, and the exact underlying quota is unavailable. A percentage is not a remaining token balance. Neither preflight nor scenario calculations convert it into one.

ChatGPT authentication uses subscription access. API-key authentication is a different billing route; exhaustion does not automatically roll over to it. Credits are another paid category, distinct from API-key and OpenRouter billing. Existing explicit OpenRouter routes remain API routes.

The example disables automatic paid fallback and sets the incremental monthly API cap to GBP 0. Enabling fallback alone never permits dispatch: an explicit API route, task allowance and available reservation are still required.

## Policy boundary

Call `validate_cost_policy(raw) -> dict` once after reading JSON. The returned mapping is JSON serialisable and can be passed back into validation. Unknown keys, invalid types, booleans used as numeric values, non-finite or negative money, invalid dates and zero FX rates are rejected with `ValueError`.

| Key | Meaning and default |
|---|---|
| `monthly_api_cap_gbp` | Non-negative monthly cap; default `0`. |
| `run_api_cap_gbp` | Optional cap across the entire named run, including month boundaries. Requires `run_id` when reserving. |
| `automatic_paid_fallback` | Boolean; default `false`. Never changes a route automatically. |
| `subscription_auth_modes` | List containing only `chatgpt`, or empty to disable subscription dispatch. API auth cannot be relabelled as included. |
| `snapshot_max_age_seconds` | Positive integer; default `900`. This is an operational freshness limit, not a provider quota guarantee. |
| `monthly_subscription_gbp` | Optional fixed fee. Omission means UNKNOWN, not zero or GBP 90. |
| `subscription_fee_evidence` | Optional text identifying the fee assumption. |
| `fx` | Optional positive `usd_to_gbp`, ISO `dated` date and optional `source`. |
| `pricing` | Optional exact model identities mapped to dated USD-per-million input/cache/output rates. |

The example's USD-to-GBP input is `0.7416652271549491`, dated 2 September 2026, supplied from the [ECB daily reference XML](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml). It is a dated comparison assumption, not a fixed future exchange rate or a claim about invoice FX. Without explicit dated FX, the report keeps USD and GBP separate and GBP conversion UNKNOWN.

Each `pricing` entry requires `input`, `cached`, `output`, `source` and `dated`; `cache_write` is optional. Rates are USD per million tokens. Nonzero cache writes with an unavailable rate remain UNKNOWN. Exact identities are required; add an explicit separate entry if a provider returns a different identity. The supplied example records:

| Exact model | Input | Cached input | Output |
|---|---:|---:|---:|
| `openrouter/moonshotai/kimi-k2.7-code` | 0.66 | 0.18 | 3.4 |
| `openrouter/z-ai/glm-5.2` | 0.966 | 0.1932 | 3.036 |
| `openrouter/z-ai/glm-5.3` | 1.4 | 0.14 | 4.4 |
| `openrouter/z-ai/glm-5.3-flash` | 0.075 | 0.015 | 0.25 |

These are source-dated comparisons. Provider-reported actual costs take precedence for cash accounting.

## Runner integration and reservations

```python
from pathlib import Path
from ringer_costs import SpendLedger, billing_preflight

state_dir = Path("/path/to/run-state")
decision = billing_preflight(
    engine="opencode",
    model="openrouter/z-ai/glm-5.2",
    policy={"monthly_api_cap_gbp": "5", "run_api_cap_gbp": "2"},
    observed_auth_mode="api_key",  # attested by the caller, never read here
    usage_snapshot=None,
    task_spend_allowance_gbp="0.50",
    attempt_id="run-123/task-a/attempt-1",  # stable, globally unique
    run_id="run-123",
    month="2026-09",
    state_dir=state_dir,
    billing_route="api",
)
if not decision.allow:
    raise RuntimeError(decision.reason)
ledger = SpendLedger(state_dir)
if not ledger.mark_started(decision.reservation_id):
    raise RuntimeError("Dispatch claim refused")
# The caller may now dispatch this attempt once.
# After independent actual-cost evidence, using explicit FX if necessary:
ledger.settle(decision.reservation_id, "0.37", source="provider-invoice-id; FX 2026-09-02")
```

`billing_preflight` is keyword-only. Required arguments are `engine`, `model`, `policy`, `observed_auth_mode` and `usage_snapshot`. Optional arguments are `task_spend_allowance_gbp`, `attempt_id`, `state_dir`, `month`, `billing_route`, `auto_fallback_requested`, `run_id` and `now`. `month` defaults to the current UTC month. Caller-supplied `now` is a timezone-aware datetime for deterministic tests or historical replay, not a way to pass stale live evidence.

The return type is immutable `BillingDecision(allow: bool, reason: str, route: str, reservation_id: str | None)`. Invalid billing inputs or ledger data produce a blocking decision. It does not change authentication or switch a subscription request to API after quota exhaustion.

A default Codex route expresses subscription *intent*, not proof of subscription authentication. It is allowed only with observed `chatgpt` auth and a valid fresh snapshot. Explicit Codex API routing requires observed `api_key` auth. OpenCode/OpenRouter routes are classified API even if a caller incorrectly labels them subscription. Credit dispatch is blocked by this allowance interface; reporting credits does not authorise their purchase or use.

`inspect_usage_snapshot(snapshot, *, now=None, max_age_seconds=900) -> QuotaEvidence` reads the sanitised `observed_at` and `rateLimitsByLimitId.codex` shape. It validates primary and returned secondary windows, percentages, positive duration, future reset, credit-status types and spend-control state. Missing, empty, malformed, stale, future-dated, reset-expired or exhausted required evidence blocks. An unparsed non-null individual quota limit also blocks. It does not use a different model's quota as a substitute. `QuotaEvidence` contains `available`, `reason`, `observed_at`, `windows`, `plan_type` and `credits`; there is no token-entitlement field.

The ledger uses a separate `fcntl.flock` lock, validates every record, writes a temporary file, flushes and fsyncs it, replaces the ledger atomically, and fsyncs the directory. Newly created directory entries are persisted before acknowledging a reservation. Supported platforms are macOS/Linux with local filesystem locking and fsync semantics. All workers sharing a cap must use the same ledger directory.

| Method | Contract |
|---|---|
| `reserve(attempt_id, amount_gbp, cap_gbp, month, *, run_id=None, run_cap_gbp=None)` | Atomically reserve a positive allowance. Exact pending/unstarted replay returns true. Conflicting amount, month or run raises `ValueError`. Started, settled or released attempts cannot reserve again. |
| `mark_started(attempt_id)` | Atomic dispatch claim. Call before spawning. Exactly one call returns true; repeats return false. A crash after claiming leaves the allowance held. |
| `settle(attempt_id, actual_gbp, *, source=...)` | Caller attests an authoritative actual GBP amount, never an estimate. Unknown (`None`) stays held. Exact settled replay is true; conflicting actual/source, new or released IDs are false. |
| `release(attempt_id, *, evidence=None)` | Unstarted reservations may be released as `not_dispatched`. Once started, only authoritative provider no-spend evidence is accepted. Exact evidence replay is idempotent. |

A started attempt's no-spend evidence has the shape `{"kind":"provider_no_spend","reference":"provider evidence identifier","observed_at":"2026-09-03T10:00:00+00:00"}`. A timeout, missing usage, killed process, syntax-check failure or worker assertion is not evidence of no spend. The caller is responsible for verifying the referenced provider evidence. This module validates the schema, not the external truth of an invoice.

The immutable reserved amount is stored separately from the actual. Settlement above the reservation records `overspend=true`; further allowances and unstarted dispatch claims in that month are blocked, even if the monthly cap would otherwise have room. Reservations are admission controls, not per-request provider hard caps. They cannot stop an already running request from spending more. The runner must also supervise live processes.

Unresolved reservations remain held after crashes. Ledger schema is `ringer-spend/v2`; earlier or malformed files fail closed and are not silently migrated, reset or overwritten. Reconcile them against provider evidence before changing format. No reservation ID should be reused for a new retry; retries have new IDs and consume their own allowance.

## Usage schema and pricing semantics

`parse_usage_jsonl(path, *, expected_model=None, service_tier=None) -> list[UsageEvent]` reads raw local logs without modifying them. `parse_usage_lines(lines, *, expected_model=None, namespace="", service_tier=None)` supports in-memory/segmented input. `expected_model` is an explicit caller attestation; it is kept distinct from event/header evidence in `model_source`.

The immutable `UsageEvent` fields are:

- Identity/evidence: `model`, `provider`, `source`, `event_id`, `session_id`, `attempt_id`, `model_source`, `billing_category`, `service_tier`, `issue`.
- Counts: `input_tokens`, `cached_input_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`.
- Accounting: `actual_cost_usd`, `harness_estimate_usd` and `aggregate`.

Counts are non-negative integers, not booleans or numeric strings. `None` means unavailable. Normalised input includes cache-read and cache-write subsets. Normalised output is the chargeable output count.

For real OpenCode `type=step_finish` events, read `part.id`, `part.tokens` and `part.cost`. The observed OpenCode accounting shape has ordinary `input` excluding `cache.read/write`, and `output` separate from `reasoning`. The adapter adds both cache subsets to normalised input and adds reasoning to normalised output once. For example, `input=100`, `cache.read=200`, `cache.write=2`, `output=10`, `reasoning=5` becomes input 302 and output 15.

OpenCode 1.18.20 [`part.cost` is computed from its local model catalogue](https://github.com/anomalyco/opencode/blob/v1.18.20/packages/opencode/src/session/session.ts#L321). It is retained as `harness_estimate_usd`, including zero, and reported separately from cash coverage. It is not an invoice, provider actual, or evidence that may settle or release a paid reservation. Only explicit `provider_cost` metadata or a manually constructed `actual_cost_usd` is treated as declared actual USD. `estimate_event` returns the recorded harness figure as an `estimate` by default; `prefer_actual=False` instead produces the separate published-rate comparison where sufficient token data exists.

For Codex `turn.completed`, `usage.input_tokens`, `cached_input_tokens` and `output_tokens` are **turn aggregates**, potentially spanning many requests. The optional `cache_write_input_tokens` field is preserved as a separate input subset. Codex output already includes reasoning, so reasoning details are not added again. These events carry `aggregate=true`. A turn exceeding 272,000 input tokens does not establish any request's context size.

An exact `model:` text header, JSON header, session metadata or turn context supplies header identity. Codex text `tokens used` is retained as a total-only UNKNOWN-cost event. No suffix or substring matching is used: the explicit aliases are `sol`, `terra`, `luna` and their `gpt-5.6-…` names. Other identities remain unknown unless explicitly priced. Headers are cleared at attempt/session boundaries. Old Codex journal `model` fields are not trusted as identity; use `reported_model`, explicitly attested `expected_model`, or raw headers.

Repeated stable event IDs are deduplicated within attempt and session namespaces. Reusing an ID with different usage raises an error. Equal ID-less completions remain distinct because equal token counts do not prove the same request. Stream item deltas and unrelated rows are not treated as completions. Total-only legacy counters and malformed JSON events retain unknown coverage rather than becoming zero. Raw logs remain the source of truth outside the derived report.

`estimate_event(event, *, category="api", pricing=None, service_tier=None, prefer_actual=True) -> CostResult` returns immutable fields `status`, `amount`, `currency`, `source`, `coverage`, `reason`, `lower`, `upper`. Status is `actual`, `estimate` or `unknown`; unknown has no amount. A bounded estimate also has `amount=None`, with explicit lower/upper values. All money uses `Decimal`.

`pricing` is the validated policy's pricing map. Provider actual USD takes precedence for API cash. `prefer_actual=False` is for the separate hypothetical API-equivalent comparison. Never sum the actual and hypothetical value for the same event. `category="credits"` uses credit units and does not relabel actual USD as credits.

Published standard API rates, USD per million tokens, dated 3 September 2026:

| Model/context | Input | Cache read | Cache write | Output |
|---|---:|---:|---:|---:|
| Sol short | 4 | 0.4 | 5 | 20 |
| Sol long | 8 | 0.8 | 10 | 30 |
| Terra short | 2 | 0.2 | 2.5 | 12 |
| Terra long | 4 | 0.4 | 5 | 18 |
| Luna short | 0.2 | 0.02 | 0.25 | 1.2 |
| Luna long | 0.4 | 0.04 | 0.5 | 1.8 |

Long context is strictly above 272,000 normalised input tokens **per request**. Aggregates with known standard service use a short/long bound, not a precise long-context price. Unknown service tier remains UNKNOWN. Verified GPT-5.6 fast short requests use twice standard short rates; other fast/long/aggregate cases remain unknown. No fast multiplier is assumed for historical models. Configured OpenRouter comparisons use their explicitly dated standard rates.

Standard short credit rates per million input/cache-read/output are Sol `100/10/500`, Terra `50/5/300`, Luna `5/0.5/30`. Unverified aggregate, long-context, fast or cache-write credit pricing remains UNKNOWN.

## Cost report and scenarios

```sh
python3 scripts/ringer_cost_report.py \
  --log ~/.ringer/runs.jsonl --state-dir ~/.ringer \
  --policy config/cost-policy.example.json \
  --usage-snapshot /path/to/sanitised-usage-snapshot.json \
  --monthly-jobs 100 --included-jobs 80 --accepted-rate 0.8 \
  --api-cost-per-job-usd 2
```

Add `--json` for `ringer-cost-report/v2`. Defaults are `~/.ringer` for state, `STATE_DIR/runs.jsonl` for the journal and `STATE_DIR/cost-policy.json` for policy. If the default policy is absent, conservative empty-policy defaults apply. Missing explicitly named policy/snapshot files fail. Missing journals or referenced raw logs produce coverage warnings and unknown costs. The personal example is never loaded implicitly. Optional `--human-review-cost-gbp` and `--coordinator-cost-gbp` supply whole-period costs; omission means UNKNOWN.

The report reads `STATE_DIR/runs/*.json`, joins tasks by `(run_id, task_key)`, and reads raw `log_path` references from state and journal attempts. Relative references resolve against their containing file's directory. Raw log attempt markers (`[ringer.py] attempt N started …`) preserve failed, retried and interrupted attempts. A referenced path is read once per task; stable event IDs prevent repeated event transport from adding cost twice. Raw usage replaces journal usage summaries for the corresponding attempt. Missing attempts retain UNKNOWN coverage. Tasks cancelled before any attempt still count in task success denominators.

When a multi-attempt log lacks attempt markers, unattributed events are shown in attempt bucket `0` (reported as a usage batch, not an extra known attempt); missing individual attempts remain unknown rather than pretending that the final log covers every attempt. Keep runner attempt markers and explicit attempt indices for reliable attribution.

Top-level JSON sections:

| Section | Evidence |
|---|---|
| `api`, `credits`, `subscription`, `unclassified` | Separate actual subtotals and unknown coverage. Known actual subtotal `0` is not proof that unknown rows were free. |
| `api_equivalent` | Hypothetical known-event lower/upper subtotal, covered count and unknown count. Never added to invoices. |
| `actual_cost_coverage` | Known actual and unknown cash event counts; incomplete logs prevent complete coverage. |
| `jobs` | All tasks/attempts, state-only tasks, first/final executed-check passes and rates, human acceptance/rejection/unknown counts, review/coordinator costs. |
| `groups` | Exact route/model cohorts with task/attempt/event counts, first/final pass evidence and cost coverage. Tasks changing model may appear in more than one cohort. |
| `events` | Per-event attribution, token breakdown, actual USD, inclusion evidence and cost-estimate provenance. |
| `scenario` | Explicit assumptions, monthly route comparison, per-accepted figures and break-even. |
| `policy`, `warnings` | Normalised assumptions and missing/malformed source coverage. |

Subscription marginal cash is GBP 0 only for a per-attempt `included_usage_established=true` attestation with observed `chatgpt` auth. An optional supplied snapshot must also be valid and fresh; an empty/stale measurement cannot establish inclusion. Merely selecting Codex, seeing an available quota, or using the flag on an API route does not make usage included. A current quota measurement alone cannot establish historical billing for unrelated attempts.

A journal `PASS` measures only the executed check. It is never renamed human acceptance. Human acceptance counts require a caller-supplied `human_review` object containing `status=accepted` (or `rejected`), `reviewer`, explicit `reviewer_type=human`, `reference` and `artifact_sha256`, matching the final task's `artifact_sha256`. Missing reviewer type, agent/worker reviewer types, known worker labels and a matching worker/agent ID are not human acceptance. These are read-only external attestations for the coordinator to verify. Workers must not create or approve them. The report creates no receipt and does not promote any artifact. `task_contract_state`, `product_state` and `promotion_state` are reported separately as supplied evidence.

Observed cash per human-accepted job is `(configured monthly fee + actual incremental cash) / human-accepted jobs`. This requires one identifiable calendar month, known cash coverage, a configured fee and a nonzero accepted denominator. Missing inputs remain UNKNOWN. Whole-job cost additionally requires supplied human review and coordinator cost. The fee is allocated once across accepted jobs, including the spend of rejected/failed attempts in the numerator.

For a scenario, let `J` be monthly jobs, `I` included jobs, `r` assumed acceptance rate, `P` USD API cost per whole job, `F` fixed GBP fee and `x` explicitly dated USD-to-GBP FX:

- Accepted monthly jobs: `J × r`.
- Subscription plus overflow: `F + (J − I) × P × x` GBP.
- All API: `J × P × x` GBP.
- Cost per accepted job: divide either monthly total by `J × r`, once.
- Break-even: `I × P × x >= F`, or required included jobs `F / (P × x)`.

`--api-cost-per-job-usd` must include every failed attempt and retry for a whole requested job. It is a transparent assumption, not inferred from syntax checks or missing invoices. Both alternatives use the same assumed acceptance rate; no measured quality claim follows. `I` is a scenario assumption, not a derived subscription token/message entitlement. Zero acceptance or a zero break-even denominator is explicitly UNKNOWN. With 100 jobs, 80 included and acceptance rate 0.8, a GBP 90 fee contributes `90/80 = GBP 1.125` per accepted job before overflow. There is no second division by observed journal passes.

## Verification

```sh
python3 -m unittest discover -s tests -p test_cost_accounting.py
```

Tests use synthetic local fixtures and processes, including the real provider event shapes, all supplied GPT-5.6 prices, per-request versus aggregate pricing, cache/reasoning semantics, unknown cash, quota/auth boundaries, corrupt ledgers, parallel reservations, abrupt process exit, duplicate dispatch claims, settlement/release evidence, raw-log retries, state omissions, acceptance distinctions and scenario arithmetic. They make no model/API calls. Synthetic review fixtures are not live approval receipts.
