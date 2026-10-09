# Public catalog assessment and model evaluation

`scripts/assess_openrouter.py` produces a public metadata assessment of every entry returned by the OpenRouter catalog. It uses Python's standard library, an unauthenticated public GET, and deterministic rules. It calls no inference endpoint, reads no credentials, and changes no routing, budgets, timers or benchmark selectors. The existing routing policy restricts paid spend; this does not mean executable billing admission is enabled on this laptop, whose current config has no `[billing]` table. This public data check is not paid model testing and does not authorise any future API dispatch. Any future paid benchmark dispatch requires an authorized budget and configured admission; this work makes no cap changes.

## Reproduce a snapshot

```bash
python scripts/assess_openrouter.py --source /absolute/path/to/catalog-source.json --out ~/.ringer/model-assessment
python scripts/assess_openrouter.py --refresh --out ~/.ringer/model-assessment --timeout 30
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p test_openrouter_assessment.py -v
```

`--source` reads the original raw API JSON. Its retrieval time is **unknown**, even if the file was just copied or described as fresh. `assessed_at_utc` is the assessment time, not proof of catalog freshness. File mtime is not retrieval evidence. Identical bytes and the same assessment time yield identical assessment content; expiry rules explicitly depend on that UTC time.

`--refresh` fetches exactly `https://openrouter.ai/api/v1/models` with a finite positive timeout (default 30 seconds), no authentication and no redirect following. Only a validated HTTP 200 full response is published. Invalid JSON, duplicate JSON keys, duplicate or missing IDs, an empty/malformed data array, an inconsistent `total_count`, or an advertised next page are rejected. Other missing or malformed model metadata stays unknown in its retained row. Failures return a non-zero exit code; they do not fall back to local data or inference, or update an old retrieval timestamp.

The output directory contains `assessment.json`, `assessment.md` and the original `catalog.json` bytes, resolved through a single `current` symlink. Each update creates a new immutable snapshot bundle under `snapshots/`: the script never overwrites a snapshot, and its files are read-only. All files are staged and flushed before an atomic pointer switch. A failed fetch, validation, staging write or pointer switch leaves the prior published bundle unchanged. Existing unrelated directories, modified output aliases and symlink output directories/parents are refused. Do not manually edit the generated directory. A reader that needs a consistent view across several files should resolve `current` once and read that bundle; separate reads through `current` could straddle a later refresh.

JSON retains the original raw JSON SHA-256, UTC assessment/retrieval provenance, total and assessed entry counts, unique ID count, sorted-ID SHA-256 and per-role candidate counts. SHA values establish reproducibility and byte identity, not provider truth or model quality. Markdown includes every model, including excluded entries. Descriptions and marketing benchmarks are not used to classify, rank or execute anything, and long provider descriptions are not copied.

## Capability rules and candidate order

These are **snapshot capability inferences**, not empirical evidence of quality or endpoint reliability. Every row and candidate is explicitly `UNTESTED`.

| Role | Required advertised metadata |
|---|---|
| Agentic coding | Text input and output, `tools`, and context at least 32,000 tokens |
| Structured extraction | Text input and output, and `response_format` or `structured_outputs` |
| Reasoning | At least one of `reasoning`, `reasoning_effort`, `include_reasoning` in supported parameters |
| Vision analysis | Image input and text output |
| Text drafting | Text input and output |
| Long context | Context at least 128,000 tokens |
| Image generation | Image output, assessed separately from vision analysis |
| Audio generation | Audio output, assessed separately from audio input |

Modality arrays and supported-parameter arrays are authoritative for this assessment. A modality string, model name, description or provider claim does not fill a missing array. Context must be a positive JSON integer; strings, booleans and malformed values remain unknown. **Lack of advertised tools makes agentic coding ineligible**. Each role records the met and unestablished conditions. Unknown metadata cannot establish eligibility.

Expired entries keep their assessment rows but are excluded from all current candidate lists. Date-only expiry is conservatively effective at the start of that date in UTC; timezone-bearing timestamps are compared to the assessment instant. Malformed expiry is unknown and also excluded. A missing/null expiry means none is advertised; it does not prove availability. A local-file candidate list remains provisional until a fresh public refresh establishes the catalog's age.

Prompt and completion prices are converted exactly to **USD per million tokens** using decimal arithmetic. Original bytes are retained in the snapshot. Fractional JSON numbers are preserved as lexical strings in row raw fields (flagged in price records), avoiding binary-float rounding; they cannot masquerade as metadata strings or integer context. Known cache/reasoning token fields use the same conversion. Missing, malformed, negative/variable sentinel, non-finite and extreme out-of-range prices have explicit states and no guessed numeric cost. Request, image, audio, search, override and other non-token or unspecified fees are retained separately in native catalog form; their units are not guessed. **Zero prompt/completion prices never establish a free route.** User-specific access, quotas, provider routing, discounts, limits, effective prices and availability are unavailable from this public response.

Only capability-eligible, non-expired candidates are ordered within each role. Order is known prompt-plus-completion USD per million token sum ascending, then unknown costs last, context descending and exact ID ascending. This is a fixed 1M-input plus 1M-output **token cost proxy**, not task cost. Non-token/conditional/override charges and cache economics are excluded from that proxy. For image/audio generation especially, inspect the separately retained fees before any cost comparison. Candidate order is not a best-model benchmark or a recommendation to dispatch.

## Refresh and reassessment methodology

The installed local systemd timer `ringer-model-assessment.timer` runs Mondays at 09:00 with up to 15 minutes of randomized delay and `Persistent=true`; it starts `ringer-model-assessment.service`. The public refresh performs no inference and uses no credentials. Inspect the timer with `systemctl --user status ringer-model-assessment.timer`; run the service manually with `systemctl --user start ringer-model-assessment.service`. The coordinator installed, enabled and read back the timer; the service exited 0 on 2026-10-09. The next read-back is Monday 2026-10-12 09:11:51 BST. The timer itself does not compare benchmark quality. A **successful refresh no more than 24 hours old is mandatory before any OpenRouter selection**; an offline source of unknown retrieval age does not meet that gate. If refresh fails, retain the historical report as stale evidence and stop selection until fresh evidence exists.

A new model release, removal, price change, expiration or capability change triggers candidate reassessment. Compare exact IDs/canonical identities, context, supported parameters/modalities, all price fields and expiry between immutable snapshots. This script provides snapshots, not a scheduler, automatic diff watcher or route change. A disappearing entry is a removal, not permission to substitute another model. Capability filtering precedes empirical testing, with the actual task's inputs, tools, output schema, context and spending authority included in the filter.

## Matched actual Ringer bakeoff before routing changes

The next empirical step is a matched, actual Ringer bakeoff using the existing `scripts/ringer_benchmark.py` suite of **20 distinct cases**, documented in [MODEL-COMPARISON.md](MODEL-COMPARISON.md). First review the fixed cases and executed checks. Retain identical prompts, fixtures, seeds, case identities, checker/source hashes and checks across candidates. Repeated runs of a case do not create additional distinct jobs. A smoke run proves execution plumbing only; marketing statements, catalog benchmarks and one smoke cannot establish a winner.

Record the **explicit exact model identity, harness and version, effort, provider and fallback policy**, billing route and resolved identity per attempt, rather than trusting task labels or moving aliases. If provider/effort is unavailable, label it unknown; do not claim a controlled comparison. Use no silent fallback or substitution. Separate model/content failures, checker/spec failures, timeout/latency failures, harness/authentication/provider failures, boundary violations and correct `BLOCKED` decisions. A correct refusal does not count as an accepted deliverable.

For each candidate retain coordinator-owned executed check outcomes, raw logs and direct artifact read-back. Report **first-pass and final-pass results**, failure classes, latency, input/output/reasoning tokens where observed, retries and **cost per accepted task** with an explicit acceptance definition. Unknown cost/tokens stay unknown; check passes and human acceptance are separate measures. Preserve safety boundaries and judge correctness, maintainability and fit for the user's current work, not only whether a file exists.

The benchmark generator currently has an explicit allowlist of older model selectors. Testing a new release requires an **explicit reviewed selector addition** and verified exact resolved identity before generation; never silently substitute an allowed old model or alter engine arguments to bypass the selector. This catalog update does not modify that allowlist.

Keep **20 distinct benchmark jobs per candidate as preliminary evidence**, insufficient by themselves for promotion. The current proven-routing thresholds require **30 distinct jobs per task family, first_try >= 0.75 and final_check >= 0.85**, followed by a separate human promotion review under [MODEL-ROUTING.md](MODEL-ROUTING.md). Do not lower thresholds to fit a convenient sample. Human review evidence must be bound to the exact artifact SHA under the repository's promotion rules.

Subscription Codex access and OpenRouter API access are separate evidence questions. Existing subscription access does not prove user-specific OpenRouter access or quotas, nor authorize paid calls. No credentials or account state are inspected here.

## Sources

- [OpenRouter model-list API reference](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties): public response fields used by this metadata assessment.
- [ChatGPT authentication documentation](https://learn.chatgpt.com/docs/auth): authentication context for future subscription-access checks; no user entitlement is inferred here.
- [Existing Ringer comparison methodology](MODEL-COMPARISON.md) and [routing policy](MODEL-ROUTING.md): executed benchmark evidence and promotion/spending boundaries.
