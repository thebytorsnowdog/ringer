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
| `bounded-detail` | native subscription, or a planned shortlist worker | Cheapest shortlist model whose conservative estimate fits £0.25. Cost arithmetic only. |
| `coordination` | native subscription | Coordinator review does not move to OpenRouter. |
| `consequential` | native subscription | Auth, money, deletion, and publication stay on the subscription route. |

Aliases `z-ai/glm-5.3-flash` and `deepseek/deepseek-v4.1-flash` map to the `openrouter/` canonical ids. Anything else is refused.

The conservative figure is rounded up to 0.0001 GBP: input tokens times the input price, plus output tokens times the output price, times a USD-to-GBP rate. The default inputs are 200,000 input tokens and 64,000 output tokens when the task does not name a smaller budget. That figure is a policy estimate, not a measured bill and not a provider hard cap.

OpenCode 2.2.6 `run --help` lists `-m provider/model#variant`, `--format default|json`, `--auto`, and `--standalone`. It does not list `--variant`, `--dir`, or a token, output, or tool-call ceiling. No variant name is confirmed, so Ringer does not append `#medium`. Because the CLI can tool-loop past the estimate, paid dispatch is refused before a reservation and before a worker starts. The £0.25, £1, and £10 ceilings remain the approval bounds for any later bounded call.

The rate must be dated and no older than 14 days. A rate dated 2026-09-02 is stale on 2026-11-08. The authorised refresh is the ECB daily euro foreign-exchange feed, no API key:

`https://data-api.ecb.europa.eu/service/data/EXR/D.GBP+USD.EUR.SP00.A?lastNObservations=1&format=csvdata`

Fetched 2026-11-08, latest observation 2026-11-06: EURGBP `0.83415`, EURUSD `1.1165`. USD to GBP is that ratio, `0.83415 / 1.1165`, half-up to five decimal places: `0.74711`. Record `fx.dated` as the observation date `2026-11-06`, `fx.usd_to_gbp` as `0.74711`, and `fx.source` as the URL plus both observations. Do not use the September rate. Do not change the pricing table in the same edit.

The approved ceilings bind even if a policy file names a higher cap: £10 in a month, £1 for every paid attempt in one swarm run, and £0.25 for one worker task. A lower cap in the policy file still binds. There is no automatic paid retry.

## What this does not make operational

The Mac checkout has no `opencode` or `claude` binary. The OpenCode wrapper path in config remains valid, and preflight still stops that route while `opencode` is absent. Ringer does not install it and does not read `~/.local/share/opencode/auth.json`. A later install can reuse a credential OpenCode already has there. Codex subscription auth cannot stand in for that route.
