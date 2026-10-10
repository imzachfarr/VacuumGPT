"""Offline cloud validation, owner fence and exact native resume tests."""
import asyncio
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import control_layer as c
import map_recovery
import recovery_context
import recovery_store as store
import supervised_command as w
import write_approval as a


class RecoverySupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        for module in (c, a):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.stack.enter_context(patch.object(a, 'PENDING', self.root / '.write-approval.json'))
        self.blid = 'offline-recovery-device'
        fingerprint = hashlib.sha256(self.blid.encode()).hexdigest()[:16]
        for module in (c, w):
            self.stack.enter_context(patch.object(module, 'FINGERPRINT', fingerprint))
        c.database().close()
        (self.root / 'activation-result.json').write_text(json.dumps({
            'writes_enabled': True, 'write_actions': ['start', 'stop', 'pause', 'resume', 'dock', 'find']}))
        self.policy = self.root / 'observer-data/control-policy.json'
        self.policy.write_text(json.dumps({'automatic_resume': True, 'direct_chat_commands': True}))
        self.now = time.time()
        self.phase_at = int(self.now - 60)
        self.reported = {'batPct': 80, 'p2maps': [{'p2map_id': 'map', 'p2mapv_id': 'v1'}],
            'cleanMissionStatus': {'missionId': 'mission', 'mssnStrtTm': int(self.now - 120),
                'nMssn': 39, 'cycle': 'clean', 'operatingMode': 32, 'phase': 'pause',
                'error': 0, 'notReady': 0, 'condNotReady': [], 'initiator': 'localApp'}}
        self.metadata = {'reported': {'cleanMissionStatus': {
            'phase': {'timestamp': self.phase_at}, 'error': {'timestamp': self.phase_at}}}}
        self.software = {'state': {'reported': {'lastCommand': {
            'command': 'pause', 'initiator': 'robot', 'time': self.phase_at}}}}
        self.connection = {'state': {'reported': {'connected': True}}}
        self.geometry = {'map_id': 'map', 'version': 'v1', 'topology_hash': 'verified-geometry',
                         'region_ids': ['13']}
        self.map_read = self.stack.enter_context(patch.object(map_recovery, 'current',
            AsyncMock(side_effect=self.read_map)))
        self.stack.enter_context(patch.object(asyncio, 'sleep', AsyncMock()))
        self.published = False
        self.before_publish = None
        self.pause_result_phase = 'pause'
        self.pause_echo_hook = None
        self.after_reports = []
        self.shadow_reads = []
        self.raw_publish = AsyncMock(side_effect=self.publish)
        mqtt = SimpleNamespace(_publish=AsyncMock(), cmd_topic=lambda prefix: 'fixture-command')
        self.robot = SimpleNamespace(_mqtt=mqtt, _irbt_topic_prefix='fixture',
            get_named_shadow=self.shadow, send_simple_command=AsyncMock(side_effect=self.send))
        self.initial_publish = mqtt._publish

    async def asyncTearDown(self):
        self.stack.close()
        self.temp.cleanup()

    async def read_map(self, robot, reported, region_ids):
        self.assertIs(robot, self.robot)
        self.assertEqual(region_ids, ['13'])
        return copy.deepcopy(self.geometry)

    async def shadow(self, name):
        self.shadow_reads.append(name)
        if name == 'rw-software': payload = self.software
        elif name == 'rw-constatus': payload = self.connection
        else:
            reported = self.after_reports.pop(0) if self.published and self.after_reports else self.reported
            payload = {'state': {'reported': reported}, 'metadata': self.metadata}
        return SimpleNamespace(payload=copy.deepcopy(payload))

    async def send(self, action):
        if self.before_publish: self.before_publish()
        await self.robot._mqtt._publish('fixture-command', json.dumps({
            'command': action, 'initiator': 'localApp', 'time': int(time.time())}))
        return True

    async def publish(self, topic, payload, timeout=5.0):
        self.published = True
        body = json.loads(payload)
        action = body['command']
        self.reported['cleanMissionStatus']['phase'] = {'resume': 'run', 'pause': self.pause_result_phase}[action]
        if action == 'pause':
            self.software['state']['reported']['lastCommand'] = dict(body)
            self.metadata['reported']['cleanMissionStatus']['phase']['timestamp'] = body['time']
            if self.pause_echo_hook: self.pause_echo_hook()

    def snapshot(self):
        return {'target_sku': c.TARGET, 'state_stale': False,
            'connection': {'data': {'status': 'connected'}}, 'state': {'data': recovery_context.enrich(
                copy.deepcopy(self.reported), {'metadata': self.metadata}, self.software, self.connection)}}

    def automatic_plan(self):
        incident = str(uuid.uuid4())
        job = {'verified_native_pause_origins': ['robot'], 'mission_id': 'mission', 'automatic_resume': True, 'held': False, 'finished': False,
            'hold_generation': 0, 'pause_incident': incident, 'resume_count': 0,
            'verified_native_pause_origins': ['robot'],
            'native_anchor': recovery_context.native_anchor(self.reported),
            'recovery_map_context': copy.deepcopy(self.geometry), 'started_at': self.now - 120,
            'room': 'Kitchen'}
        store.transaction('mission', lambda previous, db: (job, None))
        request = store.transaction('mission', lambda saved, db:
            (saved, store.reserve(db, saved, incident, time.time())))
        store.claim(request['attempt_id'])
        plan = c.prepare('resume', self.snapshot(), recovery_context=request)
        store.link_plan(request['attempt_id'], plan['plan_id'])
        a.grant_explicit_request(plan, 'owner-granted same-mission recovery:offline test')
        return plan, request

    async def execute(self, plan):
        return await w.execute_on_service(plan['plan_id'], self.robot, self.blid, self.raw_publish)

    async def refused(self, plan, reason):
        answer = await self.execute(plan)
        self.assertEqual(answer['stage'], 'failed')
        self.assertIn(reason, answer['error_code'])
        self.assertFalse(answer['may_have_executed'])
        self.raw_publish.assert_not_awaited()
        self.assertIs(self.robot._mqtt._publish, self.initial_publish)
        return answer

    async def test_valid_recovery_revalidates_at_guard_and_publishes_one_resume(self):
        plan, request = self.automatic_plan()
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertEqual(answer['native_anchor'], c.job('mission')['native_anchor'])
        self.assertEqual(answer['hold_generation'], 0)
        self.assertEqual(answer['command_time'], json.loads(self.raw_publish.await_args.args[1])['time'])
        self.assertEqual(json.loads(self.raw_publish.await_args.args[1])['command'], 'resume')
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertEqual(self.shadow_reads.count('rw-software'), 2)
        self.assertEqual(self.shadow_reads.count('rw-constatus'), 2)
        self.assertEqual(self.map_read.await_count, 2)
        self.assertEqual(store.get(request['attempt_id'])['status'], 'validating')
        self.assertEqual(store.reconcile()[0]['status'], 'complete')

    async def test_fresh_cloud_battery_blocks_approved_recovery(self):
        plan, _ = self.automatic_plan()
        self.reported['batPct'] = 24
        await self.refused(plan, 'BatteryTooLow')

    async def test_cycle_none_with_reused_mission_id_blocks_planner_replay(self):
        plan, _ = self.automatic_plan()
        self.reported['cleanMissionStatus'].update(cycle='none', mssnStrtTm=0)
        await self.refused(plan, 'MissionCycleEndedNoPlannerReplay')

    async def test_cloud_readiness_blocks_recovery(self):
        plan, _ = self.automatic_plan()
        self.reported['cleanMissionStatus']['condNotReady'] = [1]
        await self.refused(plan, 'RobotReadinessIncompleteOrBlocked')

    async def test_missing_readiness_is_not_treated_as_ready(self):
        plan, _ = self.automatic_plan()
        self.reported['cleanMissionStatus'].pop('notReady')
        await self.refused(plan, 'RobotReadinessIncompleteOrBlocked')

    async def test_new_brush_fault_is_never_resumed(self):
        plan, _ = self.automatic_plan()
        self.reported['cleanMissionStatus']['error'] = 2
        await self.refused(plan, 'ActiveRobotFault:2')

    async def test_changed_map_geometry_blocks_recovery(self):
        plan, _ = self.automatic_plan()
        self.geometry['topology_hash'] = 'different-chair-boundary'
        await self.refused(plan, 'MissionMapGeometryChanged')

    async def test_changed_map_evidence_cannot_use_old_retained_version(self):
        plan, _ = self.automatic_plan()
        self.reported['p2maps'][0]['p2mapv_id'] = 'v2'
        await self.refused(plan, 'RecoveryMapEvidenceNotCurrent')

    async def test_new_upload_with_identical_geometry_can_resume(self):
        plan, _ = self.automatic_plan()
        self.reported['p2maps'][0]['p2mapv_id'] = 'v2'
        self.geometry['version'] = 'v2'
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_old_cloud_pause_metadata_blocks_recovery(self):
        plan, _ = self.automatic_plan()
        self.metadata['reported']['cleanMissionStatus']['phase']['timestamp'] = int(self.now - 121)
        await self.refused(plan, 'RobotPauseTelemetryStale')

    async def test_app_pause_origin_blocks_recovery(self):
        plan, _ = self.automatic_plan()
        self.software['state']['reported']['lastCommand']['initiator'] = 'rmtApp'
        await self.refused(plan, 'PauseOriginUnverified')

    async def test_disconnected_constatus_blocks_recovery(self):
        plan, _ = self.automatic_plan()
        self.connection['state']['reported']['connected'] = False
        await self.refused(plan, 'DeviceConnectionUnverified')

    async def test_cloud_change_while_sdk_yields_is_checked_again_at_guard(self):
        plan, _ = self.automatic_plan()
        self.before_publish = lambda: self.reported.update(batPct=10)
        await self.refused(plan, 'BatteryTooLow')
        self.assertEqual(self.shadow_reads.count('rw-software'), 2)

    async def test_owner_hold_while_sdk_yields_blocks_final_guard(self):
        plan, request = self.automatic_plan()
        self.before_publish = lambda: store.fence('mission', 'explicit_pause')
        await self.refused(plan, 'RecoveryPermissionChangedBeforeDispatch')
        self.assertTrue(c.job('mission')['held'])
        self.assertEqual(c.job('mission')['hold_generation'], 1)
        self.assertNotIn('dispatching', [entry['stage'] for entry in c.audit(plan['plan_id'])])
        self.assertEqual(store.reconcile()[0]['status'], 'cancelled')

    async def test_changed_native_anchor_at_cloud_read_blocks_resume(self):
        plan, _ = self.automatic_plan()
        self.reported['cleanMissionStatus']['nMssn'] += 1
        await self.refused(plan, 'NativeMissionContextChanged')

    async def test_reused_id_with_changed_native_anchor_never_confirms_resume(self):
        plan, _ = self.automatic_plan()
        changed = copy.deepcopy(self.reported)
        changed['cleanMissionStatus'].update(phase='run', mssnStrtTm=int(self.now))
        faulted = copy.deepcopy(changed)
        faulted['cleanMissionStatus']['error'] = 5
        self.after_reports = [changed, faulted]
        answer = await self.execute(plan)
        self.assertFalse(answer['robot_action_confirmed'])
        self.assertTrue(answer['may_have_executed'])
        self.assertTrue(answer['same_mission'])
        self.assertNotEqual(answer['native_anchor'], c.job('mission')['native_anchor'])
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_paused_pause_noop_confirms_hold_without_publication(self):
        self.automatic_plan()
        plan = c.prepare('pause', self.snapshot())
        self.assertTrue(plan['no_op'])
        a.grant_explicit_request(plan, 'owner requested pause')
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertFalse(answer['may_have_executed'])
        self.assertFalse(answer['command_sent'])
        self.assertEqual(answer['outcome'], 'already_paused_noop')
        self.assertIsNone(answer['command_time'])
        self.assertEqual(answer['hold_generation'], c.job('mission')['hold_generation'])
        self.assertTrue(c.job('mission')['held'])
        self.raw_publish.assert_not_awaited()

    async def test_pause_receipt_does_not_adopt_newer_owner_hold(self):
        self.automatic_plan()
        self.reported['cleanMissionStatus']['phase'] = 'run'
        plan = c.prepare('pause', self.snapshot())
        a.grant_explicit_request(plan, 'owner requested pause')
        self.before_publish = lambda: store.fence('mission', 'explicit_stop')
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertEqual(answer['hold_generation'], 1)
        self.assertEqual(c.job('mission')['hold_generation'], 2)
        self.assertTrue(c.job('mission')['held'])

    async def test_uncertain_transport_receipt_preserves_exact_command_and_fence(self):
        plan, _ = self.automatic_plan()
        self.raw_publish.side_effect = RuntimeError('private-transport-detail')
        answer = await self.execute(plan)
        self.assertEqual(answer['stage'], 'failed')
        self.assertTrue(answer['may_have_executed'])
        self.assertEqual(answer['outcome'], 'unknown')
        self.assertEqual(answer['hold_generation'], 0)
        self.assertEqual(answer['command_time'], json.loads(self.raw_publish.await_args.args[1])['time'])
        self.assertEqual(answer['native_anchor'], c.job('mission')['native_anchor'])
        self.assertNotIn('private-transport-detail', json.dumps(answer))

    def running_pause_plan(self):
        self.automatic_plan()
        self.reported['cleanMissionStatus']['phase'] = 'run'
        plan = c.prepare('pause', self.snapshot())
        a.grant_explicit_request(plan, 'owner requested pause')
        self.pause_result_phase = 'stop'
        return plan

    async def test_pause_stop_clean_confirms_only_exact_echo_and_native_anchor(self):
        plan = self.running_pause_plan()
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertTrue(answer['pause_echo_verified'])
        self.assertEqual(answer['after_phase'], 'stop')
        self.assertEqual(answer['native_anchor'], c.job('mission')['native_anchor'])
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertEqual(self.shadow_reads.count('rw-software'), 1)
        self.assertEqual(self.shadow_reads.count('rw-constatus'), 1)

    async def test_pause_stop_with_other_command_echo_is_unconfirmed(self):
        plan = self.running_pause_plan()
        retained = copy.deepcopy(self.reported)
        retained['cleanMissionStatus']['phase'] = 'stop'
        faulted = copy.deepcopy(retained)
        faulted['cleanMissionStatus']['error'] = 5
        self.after_reports = [retained, faulted]
        self.pause_echo_hook = lambda: self.software['state']['reported']['lastCommand'].update(initiator='rmtApp')
        answer = await self.execute(plan)
        self.assertFalse(answer['robot_action_confirmed'])
        self.assertFalse(answer['pause_echo_verified'])
        self.assertTrue(answer['may_have_executed'])

    async def test_pause_stop_with_stale_phase_metadata_is_unconfirmed(self):
        plan = self.running_pause_plan()
        retained = copy.deepcopy(self.reported)
        retained['cleanMissionStatus']['phase'] = 'stop'
        faulted = copy.deepcopy(retained)
        faulted['cleanMissionStatus']['error'] = 5
        self.after_reports = [retained, faulted]
        def stale():
            self.metadata['reported']['cleanMissionStatus']['phase']['timestamp'] = (
                self.software['state']['reported']['lastCommand']['time'] - 1)
        self.pause_echo_hook = stale
        answer = await self.execute(plan)
        self.assertFalse(answer['robot_action_confirmed'])
        self.assertFalse(answer['pause_echo_verified'])

    async def test_pause_stop_reused_id_with_changed_native_anchor_is_unconfirmed(self):
        plan = self.running_pause_plan()
        changed = copy.deepcopy(self.reported)
        changed['cleanMissionStatus'].update(phase='stop', nMssn=40)
        faulted = copy.deepcopy(changed)
        faulted['cleanMissionStatus']['error'] = 5
        self.after_reports = [changed, faulted]
        answer = await self.execute(plan)
        self.assertFalse(answer['robot_action_confirmed'])
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_proven_retained_stop_direct_resume_reads_fresh_echo(self):
        self.reported['cleanMissionStatus']['phase'] = 'stop'
        self.software['state']['reported']['lastCommand']['initiator'] = 'localApp'
        plan = c.prepare('resume', self.snapshot())
        a.grant_explicit_request(plan, 'owner requested resume')
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertEqual(self.shadow_reads.count('rw-software'), 2)
        self.assertEqual(self.shadow_reads.count('rw-constatus'), 2)
        self.assertEqual(json.loads(self.raw_publish.await_args.args[1])['command'], 'resume')

    async def test_retained_stop_plan_never_trusts_cached_echo(self):
        self.reported['cleanMissionStatus']['phase'] = 'stop'
        self.software['state']['reported']['lastCommand']['initiator'] = 'localApp'
        plan = c.prepare('resume', self.snapshot())
        a.grant_explicit_request(plan, 'owner requested resume')
        self.software['state']['reported']['lastCommand']['command'] = 'stop'
        await self.refused(plan, 'ResumeRequiresPausedMission')

    async def test_proven_retained_stop_pause_is_noop_and_keeps_hold(self):
        self.automatic_plan()
        self.reported['cleanMissionStatus']['phase'] = 'stop'
        self.software['state']['reported']['lastCommand']['initiator'] = 'localApp'
        plan = c.prepare('pause', self.snapshot())
        self.assertTrue(plan['no_op'])
        a.grant_explicit_request(plan, 'owner requested pause')
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertFalse(answer['may_have_executed'])
        self.assertTrue(c.job('mission')['held'])
        self.raw_publish.assert_not_awaited()

    def test_stop_none_with_pause_echo_is_not_a_resumable_native_mission(self):
        self.reported['cleanMissionStatus'].update(phase='stop', cycle='none', mssnStrtTm=0)
        self.software['state']['reported']['lastCommand']['initiator'] = 'localApp'
        with self.assertRaisesRegex(c.ControlError, 'ResumeRequiresPausedMission'):
            c.prepare('resume', self.snapshot())

    def test_observed_pause_stop_requires_retained_clean_native_anchor(self):
        before = {**self.reported['cleanMissionStatus'], 'phase': 'run'}
        after = {**before, 'phase': 'stop'}
        self.assertTrue(w.observed('pause', before, after))
        self.assertFalse(w.observed('pause', before, {**after, 'cycle': 'none'}))
        self.assertFalse(w.observed('pause', before, {**after, 'mssnStrtTm': 0}))

    def test_stale_automatic_claim_is_refused_before_transport(self):
        for change in ('incident', 'generation', 'grant', 'finished', 'attempt', 'plan', 'status'):
            with self.subTest(change=change):
                plan, request = self.automatic_plan()
                if change in ('plan', 'status'):
                    with contextlib.closing(store._database()) as db, db:
                        field, value = ('plan_id', 'other') if change == 'plan' else ('status', 'queued')
                        db.execute('UPDATE recovery_outbox SET ' + field + '=? WHERE attempt_id=?',
                                   (value, request['attempt_id']))
                else:
                    def mutate(job, db):
                        field, value = {'incident': ('pause_incident', 'later'),
                            'generation': ('hold_generation', 1), 'grant': ('automatic_resume', False),
                            'finished': ('finished', True), 'attempt': ('recovery_attempt_id', 'other')}[change]
                        job[field] = value
                        return job, None
                    store.transaction('mission', mutate)
                with self.assertRaisesRegex(c.ControlError, 'Recovery'):
                    w.load_approved(plan['plan_id'])
        self.raw_publish.assert_not_called()

    def test_automatic_prefix_without_outbox_context_is_refused(self):
        plan = c.prepare('resume', self.snapshot())
        a.grant_explicit_request(plan, 'owner-granted same-mission recovery:invalid fixture')
        with self.assertRaisesRegex(c.ControlError, 'RecoveryContextRequired'):
            w.load_approved(plan['plan_id'])

    def test_dispatch_journal_is_atomic_and_never_replayable(self):
        plan, request = self.automatic_plan()
        w.load_approved(plan['plan_id'])
        store.dispatch(request['attempt_id'], plan['plan_id'], {'action': 'resume', 'outcome': 'unknown'})
        answer = c.result(plan['plan_id'])
        self.assertEqual(answer['stage'], 'dispatching')
        self.assertTrue(answer['may_have_executed'])
        with self.assertRaisesRegex(c.ControlError, 'AlreadyCommitted'):
            store.dispatch(request['attempt_id'], plan['plan_id'], {'action': 'resume'})
        self.assertEqual(sum(row['stage'] == 'dispatching' for row in c.audit(plan['plan_id'])), 1)
        store.fence('mission', 'explicit_stop')
        self.assertEqual(store.reconcile()[0]['status'], 'unknown')
        self.assertEqual(store.pending(), [])

    def test_owner_fence_before_logical_dispatch_preserves_nonexecution_journal(self):
        plan, request = self.automatic_plan()
        w.load_approved(plan['plan_id'])
        store.fence('mission', 'explicit_pause')
        with self.assertRaisesRegex(c.ControlError, 'PermissionChanged'):
            store.dispatch(request['attempt_id'], plan['plan_id'], {'action': 'resume'})
        self.assertEqual(c.result(plan['plan_id'])['stage'], 'validating')
        self.assertFalse(c.result(plan['plan_id'])['may_have_executed'])
        self.assertNotIn('dispatching', [row['stage'] for row in c.audit(plan['plan_id'])])

    def test_observation_requires_every_native_anchor_field(self):
        before = self.reported['cleanMissionStatus']
        after = {**before, 'phase': 'run'}
        self.assertTrue(w.observed('resume', before, after))
        for field in ('missionId', 'mssnStrtTm', 'nMssn', 'cycle', 'operatingMode'):
            with self.subTest(field=field):
                self.assertFalse(w.observed('resume', before, {**after, field: 'changed'}))
                incomplete = {key: value for key, value in before.items() if key != field}
                self.assertFalse(w.observed('resume', incomplete, {**incomplete, 'phase': 'run'}))

    def manual_plan(self):
        plan = c.prepare('resume', self.snapshot())
        a.grant_explicit_request(plan, 'owner requested same-mission resume')
        return plan

    async def test_manual_resume_rejects_ended_and_incomplete_fresh_anchor_before_sdk(self):
        original = copy.deepcopy(self.reported)
        changes = ({'cycle': 'none'}, {'mssnStrtTm': 0}, {'nMssn': True},
                   {'operatingMode': None}, {'mssnStrtTm': float('nan')}, {'cycle': 'evac'})
        for change in changes:
            self.reported = copy.deepcopy(original)
            plan = self.manual_plan()
            self.reported['cleanMissionStatus'].update(change)
            with self.subTest(change=change):
                await self.refused(plan, 'ResumeRequiresActiveNativeMission')
                self.robot.send_simple_command.assert_not_awaited()
        for field in ('mssnStrtTm', 'nMssn', 'cycle', 'operatingMode'):
            self.reported = copy.deepcopy(original)
            plan = self.manual_plan()
            self.reported['cleanMissionStatus'].pop(field)
            with self.subTest(missing=field):
                await self.refused(plan, 'ResumeRequiresActiveNativeMission')
                self.robot.send_simple_command.assert_not_awaited()

    async def test_manual_resume_rejects_reused_id_with_changed_complete_anchor_before_sdk(self):
        original = copy.deepcopy(self.reported)
        for field in ('mssnStrtTm', 'nMssn', 'operatingMode'):
            self.reported = copy.deepcopy(original)
            plan = self.manual_plan()
            self.reported['cleanMissionStatus'][field] += 1
            with self.subTest(field=field):
                await self.refused(plan, 'ResumeNativeMissionChanged')
                self.robot.send_simple_command.assert_not_awaited()

    async def test_manual_resume_rechecks_complete_anchor_after_sdk_yield(self):
        original = copy.deepcopy(self.reported)
        for change, reason in (({'cycle': 'none'}, 'ResumeRequiresActiveNativeMission'),
                              ({'nMssn': None}, 'ResumeRequiresActiveNativeMission'),
                              ({'mssnStrtTm': original['cleanMissionStatus']['mssnStrtTm'] + 1}, 'ResumeNativeMissionChanged'),
                              ({'nMssn': 40}, 'ResumeNativeMissionChanged'),
                              ({'operatingMode': 6}, 'ResumeNativeMissionChanged')):
            self.reported = copy.deepcopy(original)
            self.robot.send_simple_command.reset_mock()
            plan = self.manual_plan()
            self.before_publish = lambda: self.reported['cleanMissionStatus'].update(change)
            with self.subTest(change=change):
                await self.refused(plan, reason)
                self.robot.send_simple_command.assert_awaited_once_with('resume')

    async def test_manual_resume_requires_explicit_typed_fresh_readiness(self):
        original = copy.deepcopy(self.reported)
        for field, value in (('error', False), ('notReady', False), ('notReady', None),
                             ('notReady', float('nan')), ('condNotReady', None)):
            self.reported = copy.deepcopy(original)
            plan = self.manual_plan()
            self.reported['cleanMissionStatus'][field] = value
            with self.subTest(field=field, value=value):
                await self.refused(plan, 'RobotFaultRequiresOwner' if field == 'notReady' and type(value) is float else 'ResumeRequiresReadyRobot')
                self.robot.send_simple_command.assert_not_awaited()
        for field in ('notReady', 'condNotReady'):
            self.reported = copy.deepcopy(original)
            plan = self.manual_plan()
            self.reported['cleanMissionStatus'].pop(field)
            with self.subTest(missing=field):
                await self.refused(plan, 'ResumeRequiresReadyRobot')
                self.robot.send_simple_command.assert_not_awaited()

    async def test_manual_resume_rechecks_readiness_at_final_publish(self):
        plan = self.manual_plan()
        self.before_publish = lambda: self.reported['cleanMissionStatus'].pop('notReady')
        await self.refused(plan, 'ResumeRequiresReadyRobot')
        self.robot.send_simple_command.assert_awaited_once_with('resume')

    async def test_manual_resume_new_owner_hold_before_sdk_never_publishes(self):
        plan = self.manual_plan()
        store.fence('mission', 'explicit_stop')
        await self.refused(plan, 'ResumeOwnerHoldChanged')
        self.robot.send_simple_command.assert_not_awaited()

    async def test_manual_resume_new_owner_hold_during_sdk_yield_never_publishes(self):
        plan = self.manual_plan()
        self.before_publish = lambda: store.fence('mission', 'explicit_dock')
        await self.refused(plan, 'ResumeOwnerHoldChanged')
        self.robot.send_simple_command.assert_awaited_once_with('resume')

    async def test_manual_resume_last_read_hold_race_is_fenced_atomically(self):
        plan = self.manual_plan()
        original = w.record_resume_dispatch
        def fence_then_record(plan, data):
            store.fence('mission', 'explicit_pause')
            original(plan, data)
        with patch.object(w, 'record_resume_dispatch', side_effect=fence_then_record):
            await self.refused(plan, 'ResumeOwnerHoldChanged')
        self.assertNotIn('dispatching', [entry['stage'] for entry in c.audit(plan['plan_id'])])
        self.assertTrue(c.job('mission')['held'])

    async def test_manual_resume_completed_job_cannot_dispatch(self):
        plan = self.manual_plan()
        store.transaction('mission', lambda job, db: ({'mission_id': 'mission', 'hold_generation': 0, 'finished': True}, None))
        await self.refused(plan, 'ResumeMissionCompleted')
        self.robot.send_simple_command.assert_not_awaited()

    async def test_new_explicit_resume_can_release_its_captured_existing_hold(self):
        store.fence('mission', 'explicit_pause')
        plan = self.manual_plan()
        self.assertEqual(plan['expected_hold_generation'], 1)
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertFalse(c.job('mission')['held'])

    async def test_manual_resume_uncertain_receipt_is_not_retried(self):
        plan = self.manual_plan()
        self.raw_publish.side_effect = TimeoutError('offline timeout')
        answer = await self.execute(plan)
        self.assertTrue(answer['may_have_executed'])
        self.assertEqual(answer['outcome'], 'unknown')
        duplicate = await self.execute(plan)
        self.assertTrue(duplicate['duplicate_request'])
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertEqual(sum(row['stage'] == 'dispatching' for row in c.audit(plan['plan_id'])), 1)

    async def test_manual_resume_final_software_protective_command_blocks_publish(self):
        original = copy.deepcopy(self.software)
        for command in ('pause', 'stop', 'dock', 'reset', 'wipe'):
            self.software = copy.deepcopy(original)
            plan = self.manual_plan()
            self.before_publish = lambda: self.software['state']['reported']['lastCommand'].update(
                command=command, initiator='rmtApp', time=int(plan['request_started_at']))
            with self.subTest(command=command):
                await self.refused(plan, 'ResumeInterruptedByOwnerCommand')

    async def test_manual_resume_invalid_protective_command_time_fails_closed(self):
        original = copy.deepcopy(self.software)
        for value in (None, True, False, float('nan'), float('inf'), 'old'):
            self.software = copy.deepcopy(original)
            plan = self.manual_plan()
            self.before_publish = lambda: self.software['state']['reported']['lastCommand'].update(time=value)
            with self.subTest(value=value):
                await self.refused(plan, 'ResumeOwnerCommandUnverified')

    async def test_manual_resume_disconnected_final_read_never_publishes(self):
        plan = self.manual_plan()
        self.before_publish = lambda: self.connection['state']['reported'].update(connected=False)
        await self.refused(plan, 'ResumeConnectionUnverified')

    async def test_app_stop_during_status_read_cannot_be_overtaken_by_resume(self):
        started = self.now - 10
        def status():
            self.software['state']['reported']['lastCommand'].update(
                command='stop', initiator='rmtApp', time=int(started + 5))
            return self.snapshot()
        with patch.object(c, 'read_local', side_effect=lambda route: status()):
            plan = c.prepare('resume', request_started_at=started)
        self.assertLess(plan['request_started_at'], self.software['state']['reported']['lastCommand']['time'])
        self.assertLess(self.software['state']['reported']['lastCommand']['time'], plan['created_at'])
        a.grant_explicit_request(plan, 'owner requested resume')
        await self.refused(plan, 'ResumeInterruptedByOwnerCommand')

    def test_prepare_status_read_cannot_adopt_new_owner_hold(self):
        def stopped(route):
            store.fence('mission', 'explicit_stop')
            return self.snapshot()
        with patch.object(c, 'read_local', side_effect=stopped):
            with self.assertRaisesRegex(c.ControlError, 'ResumeOwnerHoldChanged'):
                c.prepare('resume')
        with contextlib.closing(c.database()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM plans').fetchone()[0], 0)

    def test_legacy_hold_is_resumable_only_if_generation_unchanged_during_prepare(self):
        store.transaction('mission', lambda job, db: ({'mission_id': 'mission', 'held': True,
            'hold_generation': 2, 'finished': False}, None))
        plan = c.prepare('resume', self.snapshot())
        self.assertEqual(plan['expected_hold_generation'], 2)
        def new_legacy_hold(route):
            store.transaction('mission', lambda job, db: ({**job, 'hold_generation': 3}, None))
            return self.snapshot()
        with patch.object(c, 'read_local', side_effect=new_legacy_hold):
            with self.assertRaisesRegex(c.ControlError, 'ResumeOwnerHoldChanged'):
                c.prepare('resume')

    def test_manual_dispatch_atomic_receipt_is_not_replayable(self):
        plan = self.manual_plan()
        w.load_approved(plan['plan_id'])
        w.record_resume_dispatch(plan, {'action': 'resume', 'may_have_executed': True})
        with self.assertRaisesRegex(c.ControlError, 'ResumeAlreadyDispatched'):
            w.record_resume_dispatch(plan, {'action': 'resume'})
        self.assertEqual(sum(row['stage'] == 'dispatching' for row in c.audit(plan['plan_id'])), 1)

    async def test_manual_resume_missing_or_invalid_intent_timestamp_fails_closed(self):
        for value in (None, True, False, 0, -1, float('nan'), float('inf')):
            plan = self.manual_plan()
            plan = w.load_approved(plan['plan_id'])
            plan['request_started_at'] = value
            self.software['state']['reported']['lastCommand'] = {'command': 'resume', 'time': self.phase_at}
            with self.subTest(value=value), self.assertRaisesRegex(c.ControlError, 'ResumeIntentTimeUnverified'):
                await w.run_on_robot(plan, self.robot, self.blid, self.raw_publish)
            self.raw_publish.assert_not_awaited()

    def test_prepare_rejects_boolean_current_owner_generation(self):
        store.transaction('mission', lambda job, db: ({'mission_id': 'mission', 'hold_generation': True,
                                                     'held': True, 'finished': False}, None))
        with self.assertRaisesRegex(c.ControlError, 'ResumeOwnerHoldChanged'):
            c.prepare('resume', self.snapshot())


if __name__ == '__main__':
    unittest.main()
