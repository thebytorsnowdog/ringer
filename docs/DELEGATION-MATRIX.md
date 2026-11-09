# Delegation matrix

This is the decision policy for planned worker choice. It is not a benchmark and not a ranking of model quality.

The approval is Ian's chat message at `2026-11-08T21:11:00Z`, minute precision:

> I approve automatic GLM/DeepSeek OpenRouter delegation within £10 per month, £1 total per swarm run and £0.25 per worker task, with no automatic paid retries.

That approval lets the native subscription coordinator plan a shortlist worker before dispatch. It does not authorise a switch after a quota error, a provider error, a top-up, another model family, or a second paid attempt.

## Catalogue facts

Read from `https://openrouter.ai/api/v1/models` on 2026-11-08 with no API key. Prices below are USD per token, as the endpoint returns them. Per-million figures are that value times one million. A helper report of `0.034` input for GLM 5.3 Flash was the rounded per-million form of `0.0000000335`.

| Model | Input USD/token | Output USD/token | Context | Tools | Reasoning | Initial shortlist |
|---|---:|---:|---:|---|---|---|
| `z-ai/glm-5.3-flash` | 0.0000000335 | 0.000000083 | 1048576 | yes | yes | yes |
| `deepseek/deepseek-v4.1-flash` | 0.000000051 | 0.000000567 | 1048576 | yes | yes | yes |
| `z-ai/glm-5.3` | 0.0000000699 | 0.0000014 | 1048576 | yes | yes | no |
| `deepseek/deepseek-v4-pro` | 0.0000002088 | 0.0000004176 | 1048576 | yes | yes | no |

`glm-5.3` and `deepseek-v4-pro` are different models, not aliases of the flash ids. A `:nitro` style suffix is a redirect and is not accepted. No quality order is inferred from the names.

The machine-readable copies are `registry/openrouter-catalogue-2026-11-08.json` and `registry/delegation-matrix.json`.

## Policy judgments

| Class | Route | Choice |
|---|---|---|
| `bounded-detail` | native subscription, or a planned shortlist worker | Cheapest shortlist model whose conservative estimate fits £0.25. Cost arithmetic only. An explicit task may name that model or a cheaper or equal shortlist model. |
| `coordination` | native subscription | Coordinator review does not move to OpenRouter. |
| `consequential` | native subscription | Auth, money, deletion, and publication stay on the subscription route. |

`route_from_table()` reads this file. `admit()` passes the task's `task_class` into the paid check, so a coordination or consequential task cannot reserve OpenRouter spend. A missing class does not default to `bounded-detail`. Claude is refused before either choice. OpenCode is not selected.

Aliases `z-ai/glm-5.3-flash` and `deepseek/deepseek-v4.1-flash` map to the `openrouter/` canonical ids. Anything else is refused.

The reserved figure is rounded up to 0.0001 GBP: the named maximum input tokens times the input price, plus the named maximum output tokens times the output price, times a USD-to-GBP rate. For the single OpenRouter request those maxima are 200,000 input tokens and 64,000 output tokens. That reservation is the worst case of the request that will be sent, not a smaller estimate.

OpenCode 2.2.6 `run --help` lists `-m provider/model#variant`, `--format default|json`, `--auto`, and `--standalone`. It does not list `--variant` or `--dir`. No variant name is confirmed, so Ringer does not append `#medium`. Help text is not the whole control surface. The tagged source `anomalyco/opencode` `v2.2.6` (`dc2ffcf`, 2026-11-06) also has these controls:

| Control | Where | What it bounds |
|---|---|---|
| `agents.*.steps` | Agents doc and `packages/core/src/session/runner/llm.ts` | Primary model steps. On the last step tools are removed. |
| `limit.output` | `packages/core/src/session/model-request.ts` `outputLimit` | Primary and compaction `maxTokens`, clamped to 256000. `packages/ai/src/protocols/openai-chat.ts` writes that value as `max_tokens` or `max_completion_tokens`. |
| Provider retry | `packages/core/src/model-retry.ts` | Hardcoded `Schedule.recurs(10)`. No config field sets it to zero. |
| Length continuation | `llm.ts` `CONTINUE_AFTER_OUTPUT_LIMIT` | Up to three `finish: length` responses inside one step, then the step fails. |
| Title call | `packages/core/src/session/title.ts` and `selectTitle` | A second call on an untitled session. Its request options are empty, so `maxTokens` is not set. |

`opencode run` also waits for background subagents with no time limit (v2.2.6 changelog, #60699). `engines/opencode-sandboxed.sh` allows writes under `~/.config/opencode`, so the worker can replace a steps or output setting before the next step. The runner re-reads agents on each step. A task-local config file does not survive that write, so OpenCode stays refused. The paid route is `engines/openrouter_worker.py`: one chat completion, no tools, no redirect, and no retry. It sends `--max-tokens` and refuses a prompt whose UTF-8 length exceeds `--max-input-tokens`. Ringer reserves the catalogue worst case of those two bounds before the process starts and settles the reported token counts afterwards. A missing billing policy, a missing `task_api_cap_gbp`, or `paid_retries` other than false refuses the call. The £0.25, £1, and £10 ceilings stay hard. A provider that ignores `max_tokens` remains a residual risk; the reservation stays held and a reported overrun is marked overspend.

The rate must be dated and no older than 14 days. A rate dated 2026-09-02 is stale on 2026-11-08. The authorised refresh is the ECB daily euro foreign-exchange feed, no API key:

`https://data-api.ecb.europa.eu/service/data/EXR/D.GBP+USD.EUR.SP00.A?lastNObservations=1&format=csvdata`

Fetched 2026-11-08, latest observation 2026-11-06: EURGBP `0.83415`, EURUSD `1.1165`. USD to GBP is that ratio, `0.83415 / 1.1165`, half-up to five decimal places: `0.74711`. Record `fx.dated` as the observation date `2026-11-06`, `fx.usd_to_gbp` as `0.74711`, and `fx.source` as the URL plus both observations. Do not use the September rate. Do not change the pricing table in the same edit.

The approved ceilings bind even if a policy file names a higher cap: £10 in a month, £1 for every paid attempt in one swarm run, and £0.25 for one worker task. A lower cap in the policy file still binds. There is no automatic paid retry.

## What this does not make operational

Homebrew OpenCode 2.2.6 is installed on the Mac. The same release is installed on the ThinkPad and the mini through the existing mise shims, and those two trees were not updated or reconfigured. Auth state was not read. That is not a claim that a login is absent. Ringer does not read `auth.json` or the keyring. With a billing policy loaded, Grok and Antigravity stop before a worker starts: a CLI name, a cleared API-key variable, or a saved login is not subscription evidence. Claude stops whether or not a policy file is loaded, because there is no Claude subscription. Grok and Antigravity without a policy file are unchanged. Codex is the engine with a supported account probe. Codex subscription auth cannot stand in for the OpenRouter route. OpenCode stays undispatched. The shortlist can run only as that one bounded OpenRouter request.
