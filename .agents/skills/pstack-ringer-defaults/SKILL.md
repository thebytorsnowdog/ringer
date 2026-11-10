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
- **Worker routing (the delegation table chooses the engine, no OpenRouter fallback).** Subscription tasks use `billing_route: "subscription"` and `service_tier: "standard"`. Codex subscription auth is ChatGPT login and is not OpenAI API credit. When a billing policy is loaded, Codex is the native engine whose subscription probe can admit a worker. Grok OAuth does not authenticate OpenAI or Gemini. Gemini subscription is the Antigravity `agy` CLI, and its worker strips API keys. A loaded billing policy stops Grok and Antigravity before a worker starts, because those probes are not implemented.
  - From **Codex**: `engine: "codex"`. Do not require OpenCode.
  - From **Grok**: `engine: "grok"`. A loaded billing policy stops this route, because no subscription probe is implemented.
  - From **Gemini / Antigravity**: `engine: "gemini"` (`agy`). A loaded billing policy stops this route, because no subscription probe is implemented.
  - **Claude is not a worker route.** There is no Claude subscription, so `engine: "claude"` is refused with or without a policy file.
  - **Hermes is retired.** It is not an engine.
  - Independent tasks are a swarm (`max_parallel` above 1). One task, `ask`, and tasks that share a write root stay serial. Missing CLIs are not installed. A usage cap, missing CLI, auth failure, or provider error stops that route. Do not switch to OpenRouter after a failure. Planned GLM/DeepSeek delegation is only the shortlist in `docs/DELEGATION-MATRIX.md`, inside the 2026-11-08 approval (£10 a month, £1 a swarm run, £0.25 a task, no paid retry). A budget cap is not a second approval, and coordinator review stays on the subscription route. OpenCode stays refused. Planned OpenRouter work is one completion with an explicit token bound and a reserved worst case. A missing billing policy means no paid dispatch. Set `task_class`. `registry/delegation-matrix.json` chooses the engine, and `admit()` passes `task_class`. On `bounded-detail`, an explicit shortlist model may only confirm the cheapest model or name a cheaper or equal one.
- **Model-fit assessment before every model job.** Assessment is NOT user approval. Before selecting OpenRouter candidates, review catalogue and task-family evidence; refresh missing or >24h-old assessment with `python3 scripts/assess_openrouter.py --refresh --out ~/.ringer/model-assessment` (see `docs/MODEL-EVALUATION.md`). Catalogue compatibility does not prove quality. Exploration is optional. Global promotion requires 30 distinct jobs per family, 75% first-try and 85% final-check passes, plus human review.
- **Judgement panels** (`architect` exploration, `interrogate`, arena cross-judge, picking a base) need model diversity, and their output is a decision, not a checkable artefact. Run them as Ringer `adversarial-review` jobs on engines a loaded billing policy can admit. Claude Code is unsupported.
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

## 5. Host adapters

Codex, Grok, and Gemini keep separate subscription auth. OpenRouter is never an automatic fallback. Claude is unsupported. Hermes is retired.

| Host | pstack | Ringer |
|---|---|---|
| Antigravity | Read the leaf files under `~/.agents/open-pstack`. Panels run as Ringer `adversarial-review` (pstack's launcher needs `bun`, not installed). | `~/.agents/skills/ringer` via shell plus `ringer_nudge_antigravity.py` hook (`engine: "gemini"`). A loaded billing policy stops this worker before it starts. |
| Grok | Panellist or worker; read leaf files from `~/.agents/open-pstack` when needed | worker and CLI harness (`engine: "grok"`). A loaded billing policy stops this worker before it starts. |
| Codex | Coordinator or worker | worker engine `codex`. This is the native engine with a subscription probe. |
| OpenCode | Historical wrapper. Admission refuses paid dispatch. | `engine: "opencode"` is refused. Paid OpenRouter work uses `engine: "openrouter"`. |

Claude Code and Hermes are historical. `./ringer.py install-agent` requires `--codex` and does not write `.claude`. `./ringer.py uninstall-agent --claude` removes a leftover Claude install.

pstack leaf files: `~/.agents/open-pstack/plugins/pstack/skills/<name>/SKILL.md`. The old `~/.claude/plugins/marketplaces/open-pstack` path is historical.
Ringer: `~/Work/GitHub/ringer` (branch `ian`). Load skill `ringer` when orchestrating.
