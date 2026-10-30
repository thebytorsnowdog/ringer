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
- **Worker routing (originating-harness subscription first, OpenRouter usage-cap fallback).** Active subscriptions consolidate to **Antigravity (`engine: "gemini"`)** and **Grok (`engine: "grok"`)** as the primary continuing subscription harnesses, with Claude Code and Codex subscriptions retiring on 6 November 2026. Always start Ringer tasks on the model/harness initiating the request using its subscription tokens (`billing_route: "subscription"`, `service_tier: "standard"`):
  - When coordinating from **Gemini / Antigravity** (primary continuing subscription): use `engine: "gemini"` (Antigravity CLI `agy` subscription worker, e.g. `gemini-4-argon-medium`, `gemini-4-argon-high`, or `gemini-3.8-flash-low`).
  - When coordinating from **Grok** (primary continuing subscription): use `engine: "grok"` (Grok subscription worker, e.g. `grok-4.7`).
  - When coordinating from **Claude Code** (retiring 6 November 2026): use `engine: "claude"` (Claude Code subscription worker, e.g. `opus` or `sonnet`).
  - When coordinating from **Codex** (retiring 6 November 2026): use `engine: "codex"` (Codex subscription worker, e.g. `gpt-5.4`).
  - All subscription engines are bwrap-sandboxed with isolated runtime dirs and forward `--effort`.
  - **Automatic usage-cap fallback to OpenRouter:** If the subscription engine hits its usage cap (either pre-dispatch via `~/.local/state/omarchy/agents/usage/<agent>.json` at `percent >= 1.0` with a future `resetsAt`, or mid-run when `failure_class == "quota"`), Ringer automatically reverts the task to `engine: "openrouter"` (`billing_route: "api"`, authenticated via `/home/snowy/.config/ringer/openrouter.key`) so the work continues without stopping. Ringer emits a stderr notice, a run summary section, a `quota_fallback` lifecycle event + HUD state badge, and an Omarchy desktop notification (`notify-send`), and **the coordinator must explicitly inform the user in its response** that the subscription usage cap was reached and work continued on OpenRouter.
- **Model-fit assessment before every model job.** Assessment is NOT user approval. Before selecting OpenRouter candidates, review catalogue and task-family evidence; refresh missing or >24h-old assessment with `python3 scripts/assess_openrouter.py --refresh --out ~/.ringer/model-assessment` (see `docs/MODEL-EVALUATION.md`). Catalogue compatibility does not prove quality. Exploration is optional. Global promotion requires 30 distinct jobs per family, 75% first-try and 85% final-check passes, plus human review.
- **Judgement panels** (`architect` exploration, `interrogate`, arena cross-judge, picking a base) need model diversity, and their output is a decision, not a checkable artefact. From Antigravity, run them as Ringer `adversarial-review` jobs across the configured subscription workers. Inside Claude Code, pstack's own frontier dispatch is fine.
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
9. Honour `/home/snowy/.config/ai/CONSTITUTION.md` and `/home/snowy/.config/ai/context.md` across every session, and load `/home/snowy/.config/ai/TELOS.md` on demand when advising on strategy, architecture, communication, or personal/wellbeing decisions.
10. Standardise all Git commits and GitHub attribution on `user.name = Snowy` and `user.email = 215750096+thebytorsnowdog@users.noreply.github.com` (never `Ian Dunsmore` or a personal email address).

## 5. Host adapters

Antigravity (`engine: "gemini"`) and Grok (`engine: "grok"`) are the primary continuing subscription harnesses; Claude Code and Codex subscriptions retire on 6 November 2026, while OpenRouter automatic usage-cap fallback remains active across all Ringer runs.

| Host | pstack | Ringer |
|---|---|---|
| Antigravity (primary continuing subscription, rules owner) | Read the leaf files under `/home/snowy/.agents/open-pstack`. Panels run as Ringer `adversarial-review` (pstack's launcher needs `bun`, not installed). | `~/.agents/skills/ringer` via shell plus `ringer_nudge_antigravity.py` hook (`engine: "gemini"`) |
| Grok (primary continuing subscription) | Panellist or worker; read leaf files from `/home/snowy/.agents/open-pstack` when needed | worker and CLI harness (`engine: "grok"`) |
| Claude Code (retiring 6 November 2026) | Native `/pstack:*` | `~/.claude/skills/ringer` plus hooks (`engine: "claude"`) |
| Codex (retiring 6 November 2026) | Worker only (no longer a coordinator) | worker engine `codex` |
| Hermes | `pstack-*` subset installed; read missing ones from the leaf files | via shell |
| OpenCode | Worker harness; follows its spec, not these rules | n/a |

pstack leaf files: `/home/snowy/.agents/open-pstack/plugins/pstack/skills/<name>/SKILL.md` (`~/.claude/plugins/marketplaces/open-pstack` is retained only as a legacy fallback until 6 November 2026).
Ringer: `~/Work/GitHub/ringer` (branch `ian`). Load skill `ringer` when orchestrating.
