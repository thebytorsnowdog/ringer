"""Distinct real-job routing evidence, including JSONL/SQLite/HTML agreement."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ringer


def rows(n=30, *, family='code-feature', first=1., final=1.):
    result=[]
    for i in range(n):
        base=dict(job_id=f'job-{i}', run_name=f'job-{i}', run_id=f'round-{i}', task_key='t',
                  worker_engine='codex', model='gpt-5.6-sol', effective_requested_model='gpt-5.6-sol',
                  task_type=family, evidence_kind='job', worker_tokens=120,
                  logged_at=f'2026-09-03T10:{i:02}:00+00:00', attempt_index=1, retry=False)
        result.append(dict(base, verdict='PASS' if i < n*first else 'FAIL'))
        if i >= n*first and i < n*final:
            result.append(dict(base, retry=True, verdict='PASS', attempt_index=2,
                               logged_at=f'2026-09-03T11:{i:02}:00+00:00'))
    return result


class RoutingEvidenceTests(unittest.TestCase):
    def tiers(self, records):
        groups = ringer.aggregate_model_log_rows(records)
        rollup = ringer.aggregate_model_scoreboard_rows(records)
        return groups, rollup

    def test_29_vs_30_jobs_and_both_rate_thresholds(self):
        for count, first, final, tier in [(29,1,1,'probation'),(30,1,1,'proven'),
                (40,.75,.85,'proven'),(40,.725,.9,'probation'),(40,.75,.825,'probation')]:
            with self.subTest(count=count, first=first, final=final):
                groups, rollup = self.tiers(rows(count, first=first, final=final))
                self.assertEqual(tier, groups[0]['tier'])
                self.assertEqual(tier, rollup[0]['tier'])
                self.assertEqual(tier=='proven', ringer.proven_model_group(groups[0]))

    def test_mixed_families_do_not_add_to_floor(self):
        groups, rollup = self.tiers(rows(20)+rows(20, family='research'))
        self.assertEqual(['probation', 'probation'], [g['tier'] for g in groups])
        self.assertEqual('probation', rollup[0]['tier'])
        self.assertEqual([20,20], [r['distinct_jobs'] for r in rollup[0]['routing_evidence']])

    def test_one_qualified_family_can_establish_model_tier(self):
        _, rollup = self.tiers(rows(30)+rows(10, family='research', first=0, final=0))
        self.assertEqual('proven', rollup[0]['tier'])
        self.assertEqual(30, rollup[0]['routing_evidence'][0]['distinct_jobs'])

    def test_repeated_rounds_tasks_and_copies_do_not_create_jobs(self):
        records=[]
        for round_number in range(3):
            for row in rows(10):
                records.append(dict(row, run_id=f"round-{round_number}-{row['job_id']}"))
                records.append(dict(row, task_key='another-task', run_id=f"round-{round_number}-{row['job_id']}"))
        _, rollup = self.tiers(records)
        self.assertEqual('probation', rollup[0]['tier'])
        self.assertEqual(10, rollup[0]['routing_evidence'][0]['distinct_jobs'])

    def test_first_failure_in_earlier_round_is_not_erased(self):
        records=rows(30, first=0, final=0)
        records += [dict(r, run_id=r['run_id']+'-later', logged_at='2026-09-04T10:00:00Z') for r in rows(30)]
        _, rollup = self.tiers(records)
        self.assertEqual('probation', rollup[0]['tier'])
        self.assertEqual(0, rollup[0]['routing_evidence'][0]['first_try_pass_rate'])
        self.assertEqual(1, rollup[0]['routing_evidence'][0]['pass_rate'])

    def test_benchmark_unnamed_and_identity_mismatch_excluded(self):
        for mutate in (lambda r: dict(r, evidence_kind='benchmark'),
                       lambda r: {k:v for k,v in r.items() if k not in {'job_id','run_name'}},
                       lambda r: dict(r, reported_model='other', expected_model='gpt-5.6-sol'),
                       lambda r: dict(r, model='', effective_requested_model=None)):
            _, rollup=self.tiers([mutate(r) for r in rows()])
            self.assertNotEqual('proven', rollup[0]['tier'])
        groups, _ = self.tiers([dict(r, task_type='') for r in rows()])
        self.assertEqual('probation', groups[0]['tier'])

    def test_sqlite_and_jsonl_and_html_have_same_rule(self):
        records=rows(30)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); log=root/'runs.jsonl'; db=root/'ringer.db'
            log.write_text(''.join(json.dumps(r)+'\n' for r in records))
            ringer.rebuild_read_model_db(db, log, catalog_path=root/'missing.json', registry_path=root/'missing.toml')
            read, _=ringer.db_attempt_rows(db)
            groups, rollup=self.tiers(records)
            db_groups, db_rollup=self.tiers(read)
            self.assertEqual(groups, db_groups)
            self.assertEqual(rollup, db_rollup)
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                ringer.print_model_explore(log_path=log, rows_read=30, skipped=0, groups=groups,
                                          catalog_path=root/'catalog', catalog_models=[])
            self.assertIn('proven', output.getvalue())
            page = ringer.render_model_scoreboard_html(rows=rollup, log_path=log, rows_read=30,
                skipped=0, catalog_path=root/'missing.json', catalog_models=[],
                notes_path=root/'missing.md', notes_sections={})
            self.assertIn('30 distinct jobs in one named task family', page)
            self.assertIn('tier-badge proven', page)
            api = ringer.build_models_api_payload(log_path=log, db_path=db, catalog_path=root/'missing.json',
                registry_path=root/'missing.toml', notes_path=root/'missing.md')
            self.assertEqual('proven', api['rollup'][0]['tier'])
            # An old derived DB must reload job identities from the unchanged log.
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA user_version=3")
                conn.execute("UPDATE attempts SET routing_metadata=NULL")
            synced = ringer.sync_read_model_db(db, log, catalog_path=root/'missing.json', registry_path=root/'missing.toml')
            self.assertTrue(synced.rebuilt)
            reread, _ = ringer.db_attempt_rows(db)
            self.assertEqual(rollup, self.tiers(reread)[1])

    def test_price_unknown_does_not_rank_as_zero_or_included(self):
        row=dict(median_tokens=1000)
        catalog=dict(prompt_per_m=1, completion_per_m=10)
        self.assertIsNone(ringer.estimated_task_cost(row, catalog))
        self.assertIsNone(ringer.estimated_task_cost({}, dict(free=True)))
        self.assertEqual(float('inf'), ringer.model_sort_cost({}, catalog))
        self.assertEqual('unknown', ringer.model_task_cost_label({}, None))
        self.assertEqual(.12, ringer.estimated_task_cost(dict(cost_coverage='provider_actual', median_actual_cost_usd='.12'), None))

if __name__=='__main__': unittest.main()
