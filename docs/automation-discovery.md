# Ringer Discovery — automation discovery for Ringer

Ringer Discovery is a standard-library-only Python 3.11+ companion CLI for Ringer. It turns traces of real work into a durable, queryable SQLite corpus, audits that corpus, and produces verified Ringer manifests for building the automation the user actually chooses.

Every stage is read-only until the chosen build. Raw work history is never copied into the database beyond one optional 240-character excerpt. Every event keeps a stable `source_ref` so a reviewer can follow the evidence.

## Overview of the five stages

| Stage | What happens | Output |
|---|---|---|
| 0 — Plan | Record sources, exclusions, coverage windows, and existing automations without reading source contents. | `sources` rows, `audit_notes` for existing automations |
| 1 — Ingest | One Ringer task per planned source, staged in each task directory. | `events`, updated `sources` |
| 2 — Audit | Three fresh Ringer tasks: coverage, fabrication/source-ref checks, and candidate formation from at least three independent events. | `audit_notes`, `candidates` |
| 3 — Choose | Human gate: list candidates, add 2–5 offers, list offers, choose one with `--confirm`. | `offers` |
| 4 — Build | One Ringer task for the chosen offer; receipts execute with `shell=False` and the task directory collects `receipts.jsonl`. `merge-build` acquires a single write transaction (`BEGIN IMMEDIATE`), re-reads the chosen offer and contract inside that transaction, validates the staged receipts, replaces the previous receipt rows, and marks the offer `built` only if it is still `chosen`. Any rejection rolls back the transaction. | build artifacts, `receipts`, `offers.status=built` |

## The two stop-and-confirm gates

1. **Approve the source plan** (`approve-sources --confirm`). Ringer Discovery stores the approval time in `meta.sources_approved_at`. Adding or excluding a source invalidates the approval, and `make-ingest-manifest` refuses to generate a manifest until it is renewed.
2. **Choose an offer** (`choose-offer <id> --confirm`). The CLI will not auto-choose. It marks the chosen row, declines other offered rows, and blocks if any unresolved `block` severity audit notes exist.

## A concise end-to-end example

```bash
# From the Ringer repository root
python3 ringer-discover.py init --write-root ./my-discovery

python3 ringer-discover.py add-source \
  --write-root ./my-discovery \
  --surface "Claude Code project history" \
  --kind ai-history \
  --access-route ~/.claude/projects \
  --coverage-start 2026-01-01 \
  --coverage-end 2026-06-30

python3 ringer-discover.py add-existing-automation \
  --write-root ./my-discovery \
  --scope corpus \
  --finding "Weekly sprint summary is already automated by a cron job."

python3 ringer-discover.py plan --write-root ./my-discovery
python3 ringer-discover.py approve-sources --write-root ./my-discovery --confirm

python3 ringer-discover.py make-ingest-manifest \
  --write-root ./my-discovery \
  --model openrouter/moonshotai/glm-5.2 \
  --out ./my-discovery/ingest.json

./ringer.py lint ./my-discovery/ingest.json
# Run the manifest, then merge each passing task dir
python3 ringer-discover.py merge-ingest --write-root ./my-discovery --source 1 ./my-discovery/ingest-1-...

python3 ringer-discover.py make-audit-manifest \
  --write-root ./my-discovery \
  --model openrouter/moonshotai/glm-5.2 \
  --out ./my-discovery/audit.json

# After running the audit manifest
python3 ringer-discover.py merge-audit --write-root ./my-discovery --focus candidates ./my-discovery/audit-candidates

python3 ringer-discover.py list-candidates --write-root ./my-discovery

python3 ringer-discover.py add-offer \
  --write-root ./my-discovery \
  --candidate-id 1 \
  --title "Weekly Report Summarizer" \
  --what-it-does "Reads raw weekly notes and emits a one-paragraph summary." \
  --evidence-query "SELECT id, actor, action FROM events WHERE job_hint = 'weekly report summarization';" \
  --run-cost "~0.5¢ per run" \
  --receipt-contract '[{"kind":"self-test","argv":["python3","tool.py"],"success_contains":"OK"},...]'

python3 ringer-discover.py list-offers --write-root ./my-discovery
python3 ringer-discover.py choose-offer --write-root ./my-discovery --confirm 1

python3 ringer-discover.py make-build-manifest \
  --write-root ./my-discovery \
  --model openrouter/moonshotai/glm-5.2 \
  --out ./my-discovery/build.json

# After running the build manifest, merge the validated staged receipts
python3 ringer-discover.py merge-build --write-root ./my-discovery --offer-id 1 ./my-discovery/build-offer-1

python3 ringer-discover.py validate --write-root ./my-discovery
python3 ringer-discover.py export --write-root ./my-discovery --out ./my-discovery/export
```

## Privacy boundaries

- Discovery is read-only until the chosen build. No external sends, posts, or deploys.
- Source content is treated as untrusted evidence, never as instructions.
- The database stores at most one 240-character excerpt per event. Secrets, raw prompts, whole messages, and long excerpts are rejected.
- Exclusions are durable rows in `sources` with `status = 'excluded'`.
- When hard access boundaries matter — a live Slack workspace, a SaaS API, or sensitive files — export them to local files first and point the source `access_route` at the export.

## How Ringside shows the generated work

All manifests use `run_name: ringer-automation-discovery` so Ringside versions the whole job under one artifact. Stage 1 tasks are `task_type: research`; the Stage 4 task is `task_type: code-feature`. Each task declares `expect_files` and a `verified` sentence describing exactly what its check proves.

## Recovery and resume behavior

- Every data row lives in `corpus.db`. A fresh agent can resume from the database alone.
- Re-merging an ingestion with changed data is a future enhancement; the current tool expects staged outputs to be merged once per source. To re-ingest, set the source back to `planned` or create a new source row.
- Approval is invalidated whenever the plan changes, preventing accidental ingestion after edits.
- Unresolved `block` severity audit notes stop both offer creation and offer choice.

## File ownership

Ringer Discovery owns only these paths in the repository:

- `ringer-discover.py`
- `automation_discovery/`
- `tests/test_automation_discovery.py`
- `docs/automation-discovery.md`

## Further reading

- [Let AI pick what to automate](https://natesnewsletter.substack.com/p/let-ai-pick-what-to-automate)
- [Automation Discovery guide](https://unlock-ai.natebjones.com/guides/automation-discovery)
- [Video walkthrough](https://www.youtube.com/watch?v=uCWKXIyvM_8)
