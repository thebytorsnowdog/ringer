# bakeoff

## What it is

A bakeoff runs a scenario-by-model matrix and grades every cell against one shared contract. Each task is one cell: one scenario, one candidate model, one session directory, one evaluator report.

Use it when you need evidence about which model or configuration performs better on your real surface. The manifest must name the model per task so the run state proves which model produced each result.

## When to use

- Choosing a model for a product workflow, review lane, prompt, or harness.
- Comparing cost, failure modes, and output quality across the same scenarios.
- Re-running a small evidence-backed benchmark after changing prompts or product behavior.
- Detecting when a cheaper model is good enough for mechanical work but not enough for long conversational tasks.

## Fill in

| Placeholder | What goes there |
|---|---|
| `{{PRODUCT}}` | Product, prompt, repo, or workflow being tested. |
| `{{WORKDIR}}` | Absolute scratch directory where Ringer creates matrix cell session directories. |
| `{{MODEL_KEY}}` | Short model label for the task key, such as `glm52` or `kimi27`. |
| `{{SCENARIO_KEY}}` | Stable key for the scenario row. |
| `{{ENGINE}}` | Ringer engine block to use. Paid OpenRouter cells use `openrouter` and a shortlist model. Admission refuses `opencode`. |
| `{{CANDIDATE_MODEL — e.g. openrouter/z-ai/glm-5.3-flash}}` | The model slug for this cell. This belongs in the task `model` field, not a cloned engine block. Paid cells use a shortlist id. |
| `{{SCENARIO — the persona, prompt, or task; keep wording identical for every model in this scenario row}}` | The exact scenario text. Keep it unchanged across candidate models. |
| `{{HARNESS_COMMAND_WITH_MODEL — exact command that runs this scenario against the manifest model and writes to the session dir}}` | Command that drives the product for this cell using the manifest-selected model. |
| `{{OUTPUT_CONTRACT — same numbered criteria for every cell}}` | Shared grading rubric for every model in the bakeoff. |
| `{{CHECK_SCRIPT_PATH — absolute path to templates/bakeoff/checks/bakeoff.py}}` | Absolute path to this kit's validator after you copy or reference the kit. |
| `{{SESSION_VALIDATOR — exact command that proves this session ran with the expected model}}` | Command that verifies the transcript/run metadata used the expected model and prints why it failed. |

## Checks

The manifest invokes `checks/bakeoff.py`. The validator checks the evaluator report, requires the expected model to be named, requires a final `VERDICT:` line, and runs the session validator.

This catches the specific bakeoff failure mode from prior runs: task keys named different competitors while the engine block ran one hard-coded model. A real bakeoff has the candidate in the per-task `model` field and verifies the model column or run metadata.

## Mix with

- `focus-group`: use focus-group to define fixed personas and then bakeoff models against those same scenario rows.
- `research-with-proof`: use it when the grading contract depends on facts, pricing, current docs, or a runnable proof.
- `review-swarm`: bake off reviewers by giving each model the same read-only surface and comparing evidence quality, false positives, and missed issues.

## Gotchas

- Do not clone engine blocks to change models. The per-task `model` field is the source of truth.
- Keep scenario wording identical across a row. Small wording changes ruin comparability.
- A harness failure is a result. Record it instead of hiding it.
- Verify the run state or session metadata, not just the task key, before claiming a model won.
