# Ringer workspace instructions

## Mission fit gate

Before consequential or delegated work begins, write a brief mission contract:

1. **Outcome:** State what should exist, and where, without using `done`, `complete` or `successful`.
2. **Access:** Name the tools, data, permissions and time required. If required access is missing, the status is `BLOCKED`.
3. **Quality:** Define what makes the result fit for use and who is qualified to judge it.
4. **Evidence:** Name the source-of-truth read-back that proves the outcome. A worker's account and file existence are not proof by themselves.
5. **Supervision:** Name who reviews the evidence before the result is used.

Keep the contract proportionate for routine work. Treat work as consequential when it changes external state, permissions, money, publication, deletion, customer or production data, or produces a result another person will rely on.

For Ringer manifests, put the mission contract in the task specification and implement the evidence requirement in the executed check. Verify the strongest state the evidence supports. Do not promote `request accepted` to `settled`, `file written` to `delivered`, `tests passed` to `requirement satisfied`, or a plausible substitute to the requested source.

Use `$clean-my-ai-harness-mission-fit` for a read-only audit when reviewing false success, repeated corrections, capability gaps or whether recurring work fits the harness. Use 3-10 representative runs when available. Treat a correction as a repeated pattern only after three independent occurrences, while labelling a single high-consequence case honestly.

After an approved harness change, replay the same known cases, including a plausible-source trap, an approval boundary and an impossible mission whose only passing result is `BLOCKED`. Keep a change only when the known failures improve without weakening stop behaviour.

Harness recommendations stay proposals until the user approves numbered items. Installing a skill, approving one item or saying that a report looks good does not approve other changes.

### Executed states

Ringer checks report three distinct states that must not be conflated:

- **Task contract:** the named deliverable checker (`scripts/check_mission_fit_gate.py deliverables ...`) verifies the task-contract deliverables, such as `notes.md`, in addition to `expect_files`. A missing, empty or out-of-bounds deliverable is `NEEDS_CHANGE`, not a product pass.
- **Product state:** the project's product validators stay cumulative: every earlier contract check keeps running and must pass. Their pass is a product-state pass, never a promotion.
- **Promotion state:** `READY` requires a human review receipt bound to the artifact SHA-256. A worker cannot approve its own output; absent or mismatched evidence is `BLOCKED`.

A human review receipt that does not match the current artifact SHA-256 is `BLOCKED`. Promotion to `READY` or `USED` is blocked until that receipt exists, names the correct artifact, and matches the current artifact SHA-256.
