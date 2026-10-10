import contextlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import control_layer as c
import recovery_store as store


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(c, 'ROOT', self.root))
        self.job = {'mission_id': 'mission', 'pause_incident': 'incident',
                    'automatic_resume': True, 'held': False, 'finished': False,
                    'resume_count': 0, 'hold_generation': 0}
        store.transaction('mission', lambda job, db: (dict(self.job), None))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def reserve(self, incident='incident'):
        def update(job, db):
            request = store.reserve(db, job, incident, time.time())
            return job, request
        return store.transaction('mission', update)

    def plan(self, request, plan_id='fixture-plan'):
        with contextlib.closing(c.database()) as db, db:
            db.execute('INSERT INTO plans VALUES(?,?,?)',
                       (plan_id, time.time(), json.dumps({'mission_id': request['mission_id']})))
        return plan_id

    def test_unique_claim_consumes_budget_once(self):
        request = self.reserve()
        self.assertIsNone(self.reserve())
        self.assertEqual(c.job('mission')['resume_count'], 1)
        self.assertEqual(len(store.pending()), 1)
        self.assertEqual(store.claim(request['attempt_id'])['status'], 'validating')
        self.assertIsNone(store.claim(request['attempt_id']))

    def test_concurrent_reservations_produce_one_attempt_and_one_budget_charge(self):
        barrier = threading.Barrier(2)
        outcomes = []
        failures = []
        def reserve():
            try:
                barrier.wait(2)
                outcomes.append(self.reserve())
            except Exception as exc:
                failures.append(exc)
        workers = [threading.Thread(target=reserve) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertEqual(c.job('mission')['resume_count'], 1)
        self.assertEqual(len(store.pending()), 1)

    def test_atomic_owner_fence_serializes_with_evaluation_and_cancels_queue(self):
        request = self.reserve()
        inside = threading.Event()
        release = threading.Event()
        outcome = []
        def update(job, db):
            inside.set()
            if not release.wait(2):
                raise RuntimeError('fixture synchronization timeout')
            job['last_phase'] = 'pause'
            return job, None
        def evaluate():
            try: store.transaction('mission', update)
            except Exception as exc: outcome.append(exc)
        def owner():
            try: store.fence('mission', 'explicit_pause')
            except Exception as exc: outcome.append(exc)
        evaluation = threading.Thread(target=evaluate)
        evaluation.start()
        self.assertTrue(inside.wait(2))
        fence = threading.Thread(target=owner)
        fence.start()
        release.set()
        evaluation.join(3)
        fence.join(3)
        self.assertFalse(evaluation.is_alive())
        self.assertFalse(fence.is_alive())
        self.assertEqual(outcome, [])
        self.assertTrue(c.job('mission')['held'])
        self.assertEqual(c.job('mission')['hold_generation'], 1)
        self.assertEqual(store.get(request['attempt_id'])['status'], 'cancelled')
        self.assertIsNone(store.claim(request['attempt_id']))

    def test_initial_job_revision_and_failed_transaction_roll_back_together(self):
        self.assertEqual(c.job('mission')['revision'], 1)
        def fail(job, db):
            store.reserve(db, job, 'incident', time.time())
            raise RuntimeError('fixture failure')
        with self.assertRaises(RuntimeError): store.transaction('mission', fail)
        self.assertEqual(c.job('mission')['resume_count'], 0)
        self.assertEqual(c.job('mission')['revision'], 1)
        self.assertEqual(store.pending(), [])
        result = store.transaction('new', lambda job, db: ({'mission_id': 'new'}, 'created'))
        self.assertEqual(result, 'created')
        self.assertEqual(c.job('new')['revision'], 1)

    def test_claim_without_plan_can_revalidate_after_restart_without_new_budget(self):
        request = self.reserve()
        store.claim(request['attempt_id'])
        changes = store.reconcile()
        self.assertEqual(changes[0]['status'], 'queued')
        self.assertEqual(store.pending()[0]['attempt_id'], request['attempt_id'])
        self.assertEqual(c.job('mission')['resume_count'], 1)
        self.assertIsNotNone(store.claim(request['attempt_id']))

    def test_linked_pre_dispatch_plan_needs_attention_and_cannot_replay(self):
        request = self.reserve()
        store.claim(request['attempt_id'])
        plan_id = self.plan(request)
        store.link_plan(request['attempt_id'], plan_id)
        c.record(plan_id, 'validating', {'may_have_executed': False})
        self.assertEqual(store.reconcile()[0]['status'], 'needs_attention')
        self.assertEqual(store.pending(), [])
        self.assertIsNone(store.claim(request['attempt_id']))

    def test_unknown_dispatch_never_requeues_even_after_owner_fence(self):
        request = self.reserve()
        store.claim(request['attempt_id'])
        plan_id = self.plan(request)
        store.link_plan(request['attempt_id'], plan_id)
        c.record(plan_id, 'dispatch_uncertain', {'may_have_executed': True, 'outcome': 'unknown'})
        store.fence('mission', 'explicit_stop')
        self.assertEqual(store.reconcile()[0]['status'], 'unknown')
        self.assertEqual(store.reconcile(), [])
        self.assertEqual(store.pending(), [])
        self.assertIsNone(store.claim(request['attempt_id']))

    def test_completed_observed_dispatch_reconciles_complete(self):
        request = self.reserve()
        store.claim(request['attempt_id'])
        plan_id = self.plan(request)
        store.link_plan(request['attempt_id'], plan_id)
        c.record(plan_id, 'complete', {'may_have_executed': True, 'robot_action_confirmed': True})
        self.assertEqual(store.reconcile()[0]['status'], 'complete')
        self.assertEqual(store.pending(), [])

    def test_generation_changed_during_validation_blocks_linking(self):
        request = self.reserve()
        store.claim(request['attempt_id'])
        plan_id = self.plan(request)
        store.fence('mission', 'explicit_dock')
        self.assertIsNone(store.link_plan(request['attempt_id'], plan_id))
        self.assertEqual(store.get(request['attempt_id'])['status'], 'cancelled')

    def test_plan_link_cannot_change_and_terminal_result_cannot_reopen(self):
        request = self.reserve()
        store.claim(request['attempt_id'])
        plan_id = self.plan(request)
        store.link_plan(request['attempt_id'], plan_id)
        with self.assertRaisesRegex(c.ControlError, 'PlanMismatch'):
            store.link_plan(request['attempt_id'], 'different-plan')
        store.finish(request['attempt_id'], 'unknown', {'outcome': 'unknown'})
        self.assertEqual(store.finish(request['attempt_id'], 'complete')['status'], 'unknown')
        with self.assertRaisesRegex(c.ControlError, 'TerminalStatus'):
            store.finish(request['attempt_id'], 'queued')

    def test_missing_job_owner_fence_is_persisted(self):
        answer = store.fence('new-mission', 'explicit_pause')
        self.assertEqual(answer['generation'], 1)
        self.assertTrue(c.job('new-mission')['held'])
        self.assertFalse(c.job('new-mission')['automatic_resume'])


if __name__ == '__main__':
    unittest.main()
