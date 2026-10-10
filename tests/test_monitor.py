import contextlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import control_layer as c
import direct_control
import mission_monitor as monitor
import recovery_store as store
import supervised_command as writer
import write_approval


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        for module in (c, write_approval):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.stack.enter_context(patch.object(write_approval, 'PENDING', self.root / '.write-approval.json'))
        c.database().close()
        self.policy = {'automatic_resume': True, 'direct_chat_commands': True}
        self.set_policy(self.policy)
        self.job = {'verified_native_pause_origins': ['robot'], 'mission_id': 'mission', 'room': 'Kitchen', 'map_id': 'map',
                    'started_at': 500, 'last_phase': 'run', 'held': False,
                    'automatic_resume': True, 'resume_count': 0, 'finished': False,
                    'native_anchor': {'mission_id': 'mission', 'mission_start': 500,
                        'n_mission': 1, 'cycle': 'clean', 'operating_mode': 32},
                    'recovery_map_context': {'map_id': 'map', 'version': 'v1',
                        'topology_hash': 'same-geometry', 'region_ids': ['13']}}
        self.current = {'batPct': 80, 'p2maps': [{'p2map_id': 'map', 'p2mapv_id': 'v2'}],
                        'cleanMissionStatus': {'missionId': 'mission', 'phase': 'pause',
                            'error': 0, 'notReady': 0, 'condNotReady': [],
                            'cycle': 'clean', 'initiator': 'localApp',
                            'mssnStrtTm': 500, 'nMssn': 1, 'operatingMode': 32},
                        '_recovery': {'connected': True, 'phase_reported_at': 1000,
                            'last_command': {'command': 'pause', 'initiator': 'robot', 'time': 1000},
                            'map_context': {'map_id': 'map', 'version': 'v2',
                                'topology_hash': 'same-geometry', 'region_ids': ['13']}}}
        c.save_job('mission', self.job)

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def set_policy(self, value):
        (self.root / 'observer-data/control-policy.json').write_text(json.dumps(value))

    def test_pause_debounce_durable_claim_and_no_duplicate_resume(self):
        self.assertIsNone(monitor.evaluate(self.current, 1000))
        request = monitor.evaluate(self.current, 1046)
        self.assertEqual(request['mission_id'], 'mission')
        self.assertEqual(store.get(request['attempt_id'])['status'], 'queued')
        self.assertIsNone(monitor.evaluate(self.current, 1047))
        self.assertEqual(c.job('mission')['resume_count'], 1)
        self.assertEqual(len(c.notices()['events']), 1)
        self.assertEqual(len(store.pending()), 1)

    def test_fault_incomplete_readiness_ended_cycle_and_manual_hold_block(self):
        baseline = {**self.job, 'pause_since': 800}
        for change in ({'error': 48}, {'error': None}, {'notReady': 1},
                       {'notReady': None}, {'condNotReady': [22]},
                       {'condNotReady': None}, {'cycle': 'none'}, {'cycle': None},
                       {'missionId': 'other'}):
            current = json.loads(json.dumps(self.current))
            current['cleanMissionStatus'].update(change)
            self.assertFalse(monitor.recovery_allowed(current, baseline, c.policy(), 1000)[0])
        self.assertFalse(monitor.recovery_allowed(self.current, {**baseline, 'held': True}, c.policy(), 1000)[0])
        current = {**self.current, 'batPct': 10}
        self.assertEqual(monitor.recovery_allowed(current, baseline, c.policy(), 1000), (False, 'BatteryTooLow'))

    def test_version_rotation_with_identical_geometry_does_not_block_native_resume(self):
        baseline = {**self.job, 'pause_since': 800}
        self.assertEqual(monitor.recovery_allowed(self.current, baseline, c.policy(), 1000),
                         (True, 'SameMissionPausedAndReady'))
        changed = json.loads(json.dumps(self.current))
        changed['_recovery']['map_context']['topology_hash'] = 'edited-geometry'
        self.assertEqual(monitor.recovery_allowed(changed, baseline, c.policy(), 1000),
                         (False, 'MissionMapGeometryChanged'))

    def test_local_mission_starter_does_not_authorize_external_app_pause(self):
        current = json.loads(json.dumps(self.current))
        current['_recovery']['last_command']['initiator'] = 'rmtApp'
        baseline = {**self.job, 'pause_since': 800}
        self.assertEqual(monitor.recovery_allowed(current, baseline, c.policy(), 1000),
                         (False, 'PauseOriginUnverified'))

    def test_explicitly_authorized_pause_start_without_incident_gets_durable_incident(self):
        job = {**self.job, 'pause_since': 1000, 'pause_incident': None}
        c.save_job('mission', job)
        self.assertIsNone(monitor.evaluate(self.current, 1001))
        self.assertEqual(c.job('mission')['pause_since'], 1000)
        self.assertTrue(c.job('mission')['pause_incident'])
        self.assertIsNotNone(monitor.evaluate(self.current, 1046))

    def test_brush_fault_reports_specific_reason_and_never_resumes(self):
        current = json.loads(json.dumps(self.current))
        current['cleanMissionStatus'].update(phase='stuck', cycle='none', error=2)
        self.assertIsNone(monitor.evaluate(current, 1000))
        self.assertEqual(c.job('mission')['recovery_decision'], 'ActiveRobotFault:2')
        notice = c.notices()['events'][0]
        self.assertEqual(notice['fault_title'], 'Main Brush stuck')
        self.assertTrue(notice['automatic_resume_blocked'])
        self.assertEqual(c.job('mission')['resume_count'], 0)
        current['cleanMissionStatus'].update(phase='pause', error=0)
        allowed, reason = monitor.recovery_allowed(current, {**self.job, 'pause_since': 500}, c.policy(), 1000)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'MissionCycleEndedNoPlannerReplay')

    def test_fault_clearance_starts_new_debounce_and_cancels_old_queued_claim(self):
        monitor.evaluate(self.current, 1000)
        old = monitor.evaluate(self.current, 1046)
        fault = json.loads(json.dumps(self.current))
        fault['cleanMissionStatus'].update(error=2, phase='stuck')
        monitor.evaluate(fault, 1047)
        self.assertEqual(store.get(old['attempt_id'])['status'], 'cancelled')
        self.assertIsNone(c.job('mission')['pause_since'])
        self.current['_recovery']['phase_reported_at'] = 1250
        self.current['_recovery']['last_command']['time'] = 1250
        self.assertIsNone(monitor.evaluate(self.current, 1250))
        self.assertEqual(c.job('mission')['pause_since'], 1250)
        self.assertIsNone(monitor.evaluate(self.current, 1294))
        new = monitor.evaluate(self.current, 1296)
        self.assertNotEqual(old['incident'], new['incident'])
        self.assertEqual(c.job('mission')['resume_count'], 2)

    def test_recharge_breaks_pause_incident_without_marking_native_mission_complete(self):
        monitor.evaluate(self.current, 1000)
        incident = c.job('mission')['pause_incident']
        charging = json.loads(json.dumps(self.current))
        charging['cleanMissionStatus']['phase'] = 'recharge'
        monitor.evaluate(charging, 1010)
        self.assertFalse(c.job('mission')['finished'])
        self.assertIsNone(c.job('mission')['pause_since'])
        self.current['_recovery']['phase_reported_at'] = 1020
        self.current['_recovery']['last_command']['time'] = 1020
        monitor.evaluate(self.current, 1020)
        self.assertNotEqual(c.job('mission')['pause_incident'], incident)
        self.assertIsNone(monitor.evaluate(self.current, 1064))
        self.assertIsNotNone(monitor.evaluate(self.current, 1066))

    def test_completion_never_restarts_and_notices_survive_reopen(self):
        current = json.loads(json.dumps(self.current))
        current['cleanMissionStatus'].update(phase='charge', cycle='none')
        self.assertIsNone(monitor.evaluate(current, 1000))
        self.assertTrue(c.job('mission')['finished'])
        self.assertIsNone(monitor.evaluate(self.current, 2000))
        notice = c.notices()['events'][0]
        self.assertEqual(notice['kind'], 'mission_ended')
        self.assertEqual(c.notices(notice['id'])['events'], [])

    def test_owner_fence_during_evaluation_is_not_overwritten(self):
        entered = threading.Event()
        release = threading.Event()
        failures = []
        def validate(current, job, now):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('fixture synchronization timeout')
            return True, 'NativeMissionValid'
        def evaluate():
            try: monitor.evaluate(self.current, 1000)
            except Exception as exc: failures.append(exc)
        def hold():
            try: store.fence('mission', 'explicit_pause')
            except Exception as exc: failures.append(exc)
        with patch.object(monitor, '_validate', side_effect=validate):
            evaluator = threading.Thread(target=evaluate)
            evaluator.start()
            self.assertTrue(entered.wait(2))
            owner = threading.Thread(target=hold)
            owner.start()
            release.set()
            evaluator.join(3)
            owner.join(3)
        self.assertEqual(failures, [])
        self.assertFalse(evaluator.is_alive())
        self.assertFalse(owner.is_alive())
        self.assertTrue(c.job('mission')['held'])
        self.assertIsNone(monitor.evaluate(self.current, 1046))

    def test_notices_are_emitted_after_transaction_commit(self):
        def notify(key, data):
            store.fence('mission', 'explicit_pause')
            return True
        with patch.object(c, 'notify', side_effect=notify):
            self.assertIsNone(monitor.evaluate(self.current, 1000))
        self.assertTrue(c.job('mission')['held'])

    def test_request_id_repetition_and_intent_collision_do_not_send(self):
        plan = {'plan_id': 'fixture-plan', 'action': 'clean_room'}
        with patch.object(c, 'prepare', return_value=plan), patch.object(c, 'launch_writer', return_value={'outcome': 'unknown'}) as dispatch, patch.object(write_approval, 'grant_explicit_request'):
            first = direct_control.run('clean_room', 'request-123', 'Kitchen')
            second = direct_control.run('clean_room', 'request-123', 'Kitchen')
            self.assertEqual(first['outcome'], 'unknown')
            self.assertEqual(dispatch.call_count, 1)
            self.assertFalse(second['retry_permitted'])
            with self.assertRaisesRegex(c.ControlError, 'IntentMismatch'):
                direct_control.run('clean_room', 'request-123', 'Office')

    def test_policy_revocation_blocks_direct_control(self):
        self.set_policy({})
        with patch.object(c, 'launch_writer') as dispatch:
            with self.assertRaisesRegex(c.ControlError, 'PersistentRoomControlGrant'):
                direct_control.run('clean_room', 'request-123', 'Kitchen')
            dispatch.assert_not_called()

    def test_existing_mission_enrollment_requires_confirmed_exact_receipt(self):
        self.job.update(plan_id='confirmed-plan', automatic_resume=False)
        c.save_job('mission', self.job)
        self.set_policy({'owner_approved': True, 'automatic_resume': True,
                         'authorized_existing_mission': 'mission'})
        c.record('confirmed-plan', 'complete', {'robot_action_confirmed': True, 'mission_id': 'other'})
        monitor.evaluate(self.current, 1000)
        self.assertFalse(c.job('mission')['automatic_resume'])
        c.record('confirmed-plan', 'complete', {'robot_action_confirmed': True, 'mission_id': 'mission'})
        request = monitor.evaluate(self.current, 1046)
        self.assertTrue(c.job('mission')['automatic_resume'])
        self.assertEqual(request['mission_id'], 'mission')

    def _reserved_request(self):
        now = time.time()
        job = {**self.job, 'started_at': now - 120}
        c.save_job('mission', job)
        self.current['_recovery']['phase_reported_at'] = now - 60
        self.current['_recovery']['last_command']['time'] = now - 60
        monitor.evaluate(self.current, now - 60)
        return monitor.evaluate(self.current, now)

    def _snapshot(self):
        return {'target_sku': c.TARGET, 'state_stale': False,
                'connection': {'data': {'status': 'connected'}},
                'state': {'data': self.current}}

    def _prepare(self, action, snapshot, recovery_context):
        plan = {'plan_id': 'fixture-plan', 'action': action, 'mission_id': 'mission',
                'recovery_context': recovery_context}
        with contextlib.closing(c.database()) as db, db:
            db.execute('INSERT INTO plans VALUES(?,?,?)',
                       (plan['plan_id'], time.time(), json.dumps(plan)))
        return plan

    def test_recover_consumes_exact_claim_once_and_links_context_before_dispatch(self):
        request = self._reserved_request()
        def execute(plan_id):
            row = store.get(request['attempt_id'])
            self.assertEqual(row['status'], 'validating')
            self.assertEqual(row['plan_id'], plan_id)
            return {'robot_action_confirmed': True, 'may_have_executed': True, 'outcome': 'state_transition_observed'}
        with patch.object(c, 'read_local', return_value=self._snapshot()), patch.object(c, 'prepare', side_effect=self._prepare) as prepare, patch.object(write_approval, 'grant_explicit_request'), patch.object(writer, 'execute', side_effect=execute) as dispatch:
            monitor.recover(request)
            self.assertEqual(prepare.call_args.kwargs['recovery_context']['attempt_id'], request['attempt_id'])
            self.assertEqual(store.get(request['attempt_id'])['status'], 'complete')
            with self.assertRaisesRegex(c.ControlError, 'AlreadyClaimed'):
                monitor.recover(request)
            self.assertEqual(dispatch.call_count, 1)

    def test_obsolete_incident_and_owner_fence_never_prepare_or_dispatch(self):
        request = self._reserved_request()
        with patch.object(c, 'prepare') as prepare, patch.object(writer, 'execute') as dispatch:
            with self.assertRaisesRegex(c.ControlError, 'IntentMismatch'):
                monitor.recover({**request, 'incident': 'obsolete'})
            store.fence('mission', 'explicit_stop')
            with self.assertRaisesRegex(c.ControlError, 'AlreadyClaimed'):
                monitor.recover(request)
            prepare.assert_not_called()
            dispatch.assert_not_called()

    def test_unconfirmed_execution_is_terminal_unknown_and_not_replayed(self):
        request = self._reserved_request()
        answer = {'robot_action_confirmed': False, 'may_have_executed': True, 'outcome': 'unconfirmed'}
        with patch.object(c, 'read_local', return_value=self._snapshot()), patch.object(c, 'prepare', side_effect=self._prepare), patch.object(write_approval, 'grant_explicit_request'), patch.object(writer, 'execute', return_value=answer) as dispatch:
            monitor.recover(request)
            self.assertEqual(store.get(request['attempt_id'])['status'], 'unknown')
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.reconcile(), [])
            self.assertEqual(dispatch.call_count, 1)

    def test_lost_actor_response_uses_durable_uncertain_journal(self):
        request = self._reserved_request()
        def execute(plan_id):
            c.record(plan_id, 'dispatching', {'may_have_executed': True, 'outcome': 'unknown'})
            raise c.ControlError('CommandActorResponseMissingCheckJournal')
        with patch.object(c, 'read_local', return_value=self._snapshot()), patch.object(c, 'prepare', side_effect=self._prepare), patch.object(write_approval, 'grant_explicit_request'), patch.object(writer, 'execute', side_effect=execute):
            with self.assertRaisesRegex(c.ControlError, 'ResponseMissing'):
                monitor.recover(request)
        self.assertEqual(store.get(request['attempt_id'])['status'], 'unknown')
        self.assertEqual(store.pending(), [])

    def test_changed_native_context_blocks_reserved_claim_without_budget_replay(self):
        request = self._reserved_request()
        self.current['cleanMissionStatus']['nMssn'] = 2
        with patch.object(c, 'read_local', return_value=self._snapshot()), patch.object(c, 'prepare') as prepare, patch.object(writer, 'execute') as dispatch:
            with self.assertRaisesRegex(c.ControlError, 'MissionContextChanged'):
                monitor.recover(request)
            prepare.assert_not_called()
            dispatch.assert_not_called()
        self.assertEqual(store.get(request['attempt_id'])['status'], 'needs_attention')
        self.assertEqual(c.job('mission')['resume_count'], 1)


if __name__ == '__main__':
    unittest.main()
