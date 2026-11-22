---
name: pstack-ringer-defaults
description: Ian's default operating rules. Use for any coding, design, review, research, probe or batch-edit work to pick the tier, the pstack playbook and the Ringer pattern. pstack decides how to think; Ringer executes and proves.
---

# pstack + Ringer defaults

pstack is the brain: playbook, principles, data shape, what "proven" means.
Ringer is the hands and the audit log: who types, executed checks, raw logs, Ringside.
The coordinator owns specs, checks, design, routing and review. Workers implement.

## 1. Pick the tier first (by stakes, not size)

| Tier | When | pstack | Ringer |
|---|---|---|---|
| **Light** | Read-only search or question, git, prose from existing context, dotfile/Omarchy tweak, or ONE file, few lines, ONCE | `unslop` the reply. Observe the result directly. | None. `ringer.py ask` only when sources are too large to read cheaply. |
| **Standard** | Reversible work on a branch or scratch dir: bug fix, feature, refactor, review, research you will act on, probe, eval | Match a poteto-mode playbook and copy its steps as todos. Name the data shape before code. `how` first; `architect` when crossing a function boundary. | Manifest: assess, lint, `--baseline`, run. Ringside up. `task_type` on every task. One-line mission contract (outcome + evidence) in each spec. |
| **Consequential** | External state, money, publication, deletion, production or customer data, permissions, routing or harness policy, a result others rely on, or unattended/overnight work | Standard plus `interrogate` on the design, `show-me-your-work` trail, `blast-radius` on the diff | Standard plus the full 5-field mission contract (ringer `AGENTS.md`), a human review receipt bound to the artefact SHA before READY, and a pause before any irreversible step |

Escalate on doubt. A second edit pass on the same problem is a loop, and loops go through Ringer.

## 2. Who does what

- **Implementation, probes, review scouts, personas, mechanical edits** go through Ringer, automatically, without asking permission or model-selection confirmation. Tell Ian what was chosen and why.
- **Subscription routing.** Start on the originating harness with `billing_route: "subscription"` and `service_tier: "standard"`. Use `engine: "codex"` from Codex, `"grok"` from Grok, and `"gemini"` from Antigravity (`agy`). With a billing policy loaded, only Codex has a native subscription admission probe. It requires observed ChatGPT auth and fresh, available quota. OpenAI API credit is billed separately.
  - Grok and Antigravity have no subscription admission probe on `ian`, so a loaded billing policy blocks them before dispatch. Antigravity has no subscription quota probe on `ian`. Its worker strips Gemini and Google API keys.
  - PR #20 (`feat/gemini-api-credit-fallback`) is unmerged and awaits Ian's decision. Update the Gemini routing rule once its Antigravity `fetchQuotaStatus` subscription probe lands. The PR's Google AI Studio fallback is opt-in paid API usage, separate from the subscription route.
  - Claude is unsupported with or without a billing policy. Hermes is retired.
- **Quota stops.** A detected pre-dispatch cap, quota refusal at billing admission, or in-run quota failure blocks the task on its selected route. Ringer logs the notice and prints it to stderr. Report the stop to Ian and state that OpenRouter was not selected and no harness was substituted.
- **Planned OpenRouter work.** Set `task_class` and use [the delegation matrix](../../../docs/DELEGATION-MATRIX.md) for Ian's 2026-11-08 approval. OpenRouter fails closed outside the approved `bounded-detail` GLM/DeepSeek shortlist. Plan dispatch before work starts with `engine: "openrouter"`, `billing_route: "api"`, and a loaded billing policy. The matrix chooses the cheapest eligible model. An explicit model may confirm that choice or name a cheaper or equal shortlist model. Ringer reserves the worst-case cost of one token-bounded completion within the task, run, and monthly caps. Budget caps alone confer no approval. OpenCode is refused. Any later paid attempt requires new approval. Coordinator review stays on subscription.
- Independent tasks are a swarm (`max_parallel` above 1). One task, `ask`, and tasks that share a write root stay serial. Missing CLIs are not installed.
- **Model-fit assessment before every model job.** Assessment is NOT user approval. Before selecting OpenRouter candidates, review catalogue and task-family evidence; refresh missing or >24h-old assessment with `python3 scripts/assess_openrouter.py --refresh --out ~/.ringer/model-assessment` (see `docs/MODEL-EVALUATION.md`). Catalogue compatibility does not prove quality. Exploration is optional. Global promotion requires 30 distinct jobs per family, 75% first-try and 85% final-check passes, plus human review.
- **Judgement panels** (`architect` exploration, `interrogate`, arena cross-judge, picking a base) need model diversity, and their output is a decision, not a checkable artefact. Run them as Ringer `adversarial-review` jobs on engines a loaded billing policy can admit.
- **Arena on code** is split. Candidates are Ringer tasks (same spec, different models, separate worktrees). Judging, picking and grafting stay with the coordinator plus the pstack cross-judge.
- **Workers never orchestrate.** Specs carry every hard rule. Do not load these defaults into worker prompts.

## 3. Workflow routing

| Workflow | pstack | Ringer pattern | Proof that counts |
|---|---|---|---|
| Question (how/why) | Investigation, `how` / `why` | none / `ask` | Cited answer, each claim labelled measured, inferred or guess |
| Bug fix | Bug fix playbook: reproduce yourself, binary-search the cause | `probe` for diagnosis, one-task `repo-feature` for the fix | Repro fails at baseline, passes after, on the same surface |
| Feature | Feature playbook: `how`, `architect`, throughput checkpoint | `repo-feature` (one owner for coupled code), `fix-swarm` for disjoint slices | Build, tests and a behavioural check on the matching surface |
| Refactor / migration | Refactoring or `figure-it-out`; subtract-first, migrate-then-delete, build-the-lever | `migration-swarm` driven by a codemod | Behaviour unchanged before and after; legacy API gone |
| Review / audit | `thermo-nuclear-code-quality-review`, `blast-radius` | `review-swarm`, coordinator confirms, then `fix-swarm` | Each finding reproduced before fixing; the finder never fixes |
| Design choice | `architect`, `arena`, Prototype | arena candidates as tasks, or `bakeoff` | Per-criterion rubric plus cross-judge |
| Model / prompt eval | Eval playbook | `bakeoff`, `probe`, the 20-case benchmark | First-pass and final-pass rates, cost per accepted task |
| Research to act on | `why`, `figure-it-out` | `research-with-proof`, `competitive-teardown` | A proof task executes the claim; citation allowlist |
| Content / personas | `unslop`, `technical-writing` | `focus-group`, `launch-kit`, `asset-swarm` | Parseable verdicts, render-as-check |
| Long / overnight | Autonomous run or Orchestrate, plus `show-me-your-work` | same `run_name` every round | Decision trail plus run history |
| PR shepherding | Babysit / Shipping playbooks | none | CI green plus an independent verdict |

## 4. Always

1. Only executed checks and direct read-back count. Worker claims and file existence do not. Coordinator review precedes integration.
2. Checks print why they fail and can never be `true`. New-behaviour checks FAIL at baseline; unchanged-behaviour checks PASS. Checks time out after 60s unless the task sets `check_timeout_s`. Prefer targeted tests and behavioural asserts, and run the full suite yourself as the integration gate.
3. Every writer gets its own worktree or output dir. Export deliverables out of worktrees before PASS. Preserve sandbox and source/receipt integrity.
4. Review every patch even when green. Stage specific paths; never `git add -A` in a shared checkout.
5. One job, one `run_name`. Review on Ringside, not by `cat`-ing results.
6. Record reusable routing lessons in `docs/MODEL-NOTES.md`.
7. Pause for force-push, deploys, deletion, customer messages, money and credential changes.
8. An explicit "do it inline" from Ian wins.
9. Honour `~/.config/ai/CONSTITUTION.md` and `~/.config/ai/context.md` across every session, and load `~/.config/ai/TELOS.md` on demand when advising on strategy, architecture, communication, or personal/wellbeing decisions.
10. Standardise all Git commits and GitHub attribution on `user.name = Snowy` and `user.email = 215750096+thebytorsnowdog@users.noreply.github.com` (never a personal real name or personal email address).

## 5. Ringside and fork sync

- Launch Ringside or the per-run dashboard through Ringer to get an authenticated session. Both bind to loopback and require a matching loopback `Host` for the listening port. Private data, logs, and artifacts require the per-launch `X-Ringer-Token` credential. The browser launcher supplies a derived session token. Ringside's open-folder POST also checks `Origin` against the same-port browser and approved native HUD origins, and native CORS preflight checks `Host` and the native origin.
- Follow [the fork guide](../../../docs/fork/README.md). Keep `main` a fast-forward mirror of upstream. The sync workflow publishes a tested `fork-sync/*` PR into protected `ian`. Review it and use **Create a merge commit**, never squash, to preserve upstream history and contributor credit. Land changes to `ian` through PRs.

Read pstack leaf files from `~/.agents/open-pstack/plugins/pstack/skills/<name>/SKILL.md`.
Load skill `ringer` when orchestrating from the Ringer checkout.
