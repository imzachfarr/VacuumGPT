"""Offline fault retry guards, native mission proof and durable dispatch tests."""
import asyncio
import contextlib
import copy
import hashlib
import itertools
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import control_layer as c
import direct_control as d
import recovery_context
import supervised_command as w
import write_approval as a


def brush_mission():
    return {'phase': 'stuck', 'error': 2, 'missionId': 'mission-1',
            'cycle': 'clean', 'mssnStrtTm': 1000, 'nMssn': 41,
            'operatingMode': 2, 'notReady': 0, 'condNotReady': []}


class FaultRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='roo-fault-tests-', dir='/tmp')
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        for module in (c, a):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.stack.enter_context(patch.object(a, 'PENDING', self.root / '.write-approval.json'))
        (self.root / 'activation-result.json').write_text(json.dumps({
            'write_actions': list(c.ACTIONS)}))
        c.database().close()
        self.mission = {'phase': 'stuck', 'error': 1010, 'missionId': 'mission-1',
                        'notReady': 0, 'condNotReady': []}
        self.snapshot = {'target_sku': c.TARGET, 'state_stale': False,
            'connection': {'data': {'status': 'connected'}},
            'state': {'data': {'cleanMissionStatus': self.mission}}}
        self.stack.enter_context(patch.object(c, 'policy', return_value={'direct_chat_commands': True}))
        self.stack.enter_context(patch.object(c, 'read_local', return_value=self.snapshot))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def use_brush(self):
        self.mission.clear()
        self.mission.update(brush_mission())
        self.snapshot['state']['data']['batPct'] = 95

    def test_only_complete_navigation_fault_allowed(self):
        c.preconditions('dock', self.mission, True)
        for change in ({'error': 0}, {'error': 5}, {'error': 1025}, {'phase': 'run'},
                       {'missionId': None}, {'notReady': 1}, {'condNotReady': [7]}):
            with self.subTest(change=change), self.assertRaises(c.ControlError):
                c.preconditions('dock', {**self.mission, **change}, True)
        for key in ('notReady', 'condNotReady'):
            state = dict(self.mission)
            state.pop(key)
            with self.assertRaises(c.ControlError):
                c.preconditions('dock', state, True)
        for action in ('start', 'resume', 'clean_room', 'clean_area'):
            with self.assertRaises(c.ControlError):
                c.preconditions(action, self.mission, True)
        with self.assertRaises(c.ControlError):
            c.preconditions('dock', self.mission)

    def test_duplicate_unknown_never_resends_and_mission_bounded(self):
        def unknown(plan_id):
            c.record(plan_id, 'dispatch_uncertain', {'outcome': 'unknown', 'may_have_executed': True})
            return c.result(plan_id)
        with patch.object(c, 'launch_writer', side_effect=unknown) as send:
            first = d.run('dock', 'fault-request-001', owner_navigation_retry=True)
            self.assertEqual(first, d.run('dock', 'fault-request-001', owner_navigation_retry=True))
            self.assertEqual(send.call_count, 1)
            with patch('time.time', return_value=2000000000):
                with self.assertRaisesRegex(c.ControlError, 'AlreadyClaimedForMission'):
                    d.run('dock', 'fault-request-002', owner_navigation_retry=True)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(c.get_plan(first['plan_id'])['expected_error'], 1010)

    def test_cooldown_global_limit_and_stale_device(self):
        with patch.object(c, 'launch_writer', return_value={'outcome': 'unknown'}) as send:
            with patch('time.time', return_value=1000000):
                d.run('dock', 'fault-request-101', owner_navigation_retry=True)
            self.mission['missionId'] = 'mission-2'
            with patch('time.time', return_value=1000010), self.assertRaisesRegex(c.ControlError, 'Cooldown'):
                d.run('dock', 'fault-request-102', owner_navigation_retry=True)
            with patch('time.time', return_value=1000181):
                d.run('dock', 'fault-request-103', owner_navigation_retry=True)
            self.mission['missionId'] = 'mission-3'
            with patch('time.time', return_value=1000400), self.assertRaisesRegex(c.ControlError, 'TwoInSixHours'):
                d.run('dock', 'fault-request-104', owner_navigation_retry=True)
            self.assertEqual(send.call_count, 2)
            self.snapshot['state_stale'] = True
            with self.assertRaisesRegex(c.ControlError, 'FreshExactDevice'):
                d.run('dock', 'fault-request-105', owner_navigation_retry=True)

    def test_inspected_brush_resume_is_narrow_and_claimed_once(self):
        self.use_brush()
        c.preconditions('resume', self.mission, owner_brush_retry=True)
        for change in ({'error': 5}, {'phase': 'run'}, {'notReady': 1}, {'condNotReady': [1]}):
            with self.subTest(change=change), self.assertRaises(c.ControlError):
                c.preconditions('resume', {**self.mission, **change}, owner_brush_retry=True)
        def unknown(plan_id):
            c.record(plan_id, 'dispatch_uncertain', {'outcome': 'unknown', 'may_have_executed': True})
            return c.result(plan_id)
        with patch.object(c, 'launch_writer', side_effect=unknown) as send:
            first = d.run('resume', 'brush-request-001', owner_brush_retry=True)
            self.assertEqual(first, d.run('resume', 'brush-request-001', owner_brush_retry=True))
            with self.assertRaisesRegex(c.ControlError, 'AlreadyClaimed'):
                d.run('resume', 'brush-request-002', owner_brush_retry=True)
            self.assertEqual(send.call_count, 1)
        plan = c.get_plan(first['plan_id'])
        self.assertEqual(plan['expected_error'], 2)
        self.assertEqual(plan['expected_cycle'], 'clean')
        self.assertEqual(plan['expected_native_anchor'], recovery_context.native_anchor(self.snapshot['state']['data']))

    def test_ended_or_incomplete_brush_mission_is_refused(self):
        self.use_brush()
        invalid = [{'cycle': 'none'}, {'cycle': ''}, {'cycle': None}, {'cycle': 'train'},
            {'mssnStrtTm': 0}, {'mssnStrtTm': -1}, {'mssnStrtTm': float('nan')},
            {'mssnStrtTm': float('inf')}, {'missionId': None}, {'missionId': ''}]
        for key in ('missionId', 'cycle', 'mssnStrtTm', 'nMssn', 'operatingMode'):
            missing = dict(self.mission)
            missing.pop(key)
            with self.subTest(missing=key), self.assertRaises(c.ControlError):
                c.preconditions('resume', missing, owner_brush_retry=True)
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(c.ControlError):
                c.preconditions('resume', {**self.mission, **change}, owner_brush_retry=True)

    def test_boolean_and_noninteger_brush_proof_is_refused(self):
        self.use_brush()
        for key in ('missionId', 'mssnStrtTm', 'nMssn', 'operatingMode', 'notReady', 'error'):
            for value in (True, False):
                with self.subTest(key=key, value=value), self.assertRaises(c.ControlError):
                    c.preconditions('resume', {**self.mission, key: value}, owner_brush_retry=True)
        for key in ('nMssn', 'operatingMode'):
            for value in (-1, 1.5, '2', None):
                with self.subTest(key=key, value=value), self.assertRaises(c.ControlError):
                    c.preconditions('resume', {**self.mission, key: value}, owner_brush_retry=True)

    def test_brush_battery_requires_finite_numeric_bounded_value(self):
        self.use_brush()
        for value in (True, False, None, '95', float('nan'), float('inf'), -1, 24.99, 100.01):
            self.snapshot['state']['data']['batPct'] = value
            with self.subTest(battery=value), self.assertRaises(c.ControlError):
                c.prepare('resume', owner_brush_retry=True)
        self.snapshot['state']['data'].pop('batPct')
        with self.assertRaises(c.ControlError):
            c.prepare('resume', owner_brush_retry=True)
        for value in (25, 25.5, 95, 100):
            self.snapshot['state']['data']['batPct'] = value
            with self.subTest(battery=value):
                self.assertEqual(c.prepare('resume', owner_brush_retry=True)['expected_cycle'], 'clean')

    def test_fresh_navigation_fault_change_blocks_publish(self):
        plan = c.prepare('dock', owner_navigation_retry=True)
        plan['approval_expires_at'] = time.time() + 120
        publish = AsyncMock()
        mqtt = SimpleNamespace(_publish=publish, cmd_topic=lambda prefix: 'fixture-command')
        robot = SimpleNamespace(_mqtt=mqtt, _irbt_topic_prefix='fixture',
            get_named_shadow=AsyncMock(return_value=SimpleNamespace(payload={
                'state': {'reported': {'cleanMissionStatus': {**self.mission, 'error': 5}}}})))
        with self.assertRaisesRegex(c.ControlError, 'FaultChangedSinceOwnerRequest'):
            asyncio.run(w.run_on_robot(plan, robot, 'fixture', publish))
        publish.assert_not_called()


class BrushRecoveryWriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='roo-brush-writer-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        for module in (c, a):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.stack.enter_context(patch.object(a, 'PENDING', self.root / '.write-approval.json'))
        self.blid = 'offline-brush-device'
        fingerprint = hashlib.sha256(self.blid.encode()).hexdigest()[:16]
        for module in (c, w):
            self.stack.enter_context(patch.object(module, 'FINGERPRINT', fingerprint))
        c.database().close()
        (self.root / 'activation-result.json').write_text(json.dumps({
            'writes_enabled': True, 'write_actions': list(c.ACTIONS)}))
        self.stack.enter_context(patch.object(c, 'policy', return_value={'direct_chat_commands': True}))
        self.stack.enter_context(patch.object(asyncio, 'sleep', AsyncMock()))
        self.reset_cloud()
        self.shadow_reads = []
        self.before_publish = None
        self.double_publish = False
        self.publish_error = None
        self.raw_publish = AsyncMock(side_effect=self.publish)
        mqtt = SimpleNamespace(_publish=AsyncMock(), _connected=True,
            cmd_topic=lambda prefix: 'fixture-command')
        self.robot = SimpleNamespace(_mqtt=mqtt, _irbt_topic_prefix='fixture',
            get_named_shadow=self.shadow, send_simple_command=self.send)
        self.initial_publish = mqtt._publish

    async def asyncTearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def reset_cloud(self):
        self.reported = {'batPct': 95, 'cleanMissionStatus': brush_mission()}
        self.state_version = 100
        self.after_publish = None
        stamp = int(time.time())
        self.metadata = {'reported': {'cleanMissionStatus': {
            'phase': {'timestamp': stamp}, 'error': {'timestamp': stamp}}}}
        self.software = {'state': {'reported': {'lastCommand': {
            'command': 'resume', 'time': stamp, 'initiator': 'localApp'}}}}
        self.connection = {'state': {'reported': {'connected': True}}}

    async def shadow(self, name):
        self.assertIn(name, ('ro-currentstate', 'rw-software', 'rw-constatus'))
        self.shadow_reads.append(name)
        if name == 'rw-software':
            payload = self.software
        elif name == 'rw-constatus':
            payload = self.connection
        else:
            payload = {'state': {'reported': self.reported}, 'metadata': self.metadata}
            if self.state_version is not None:
                payload['version'] = self.state_version
        return SimpleNamespace(payload=copy.deepcopy(payload))

    async def send(self, action):
        self.assertEqual(action, 'resume')
        if self.before_publish:
            self.before_publish()
        payload = json.dumps({'command': action, 'initiator': 'localApp', 'time': int(time.time())})
        await self.robot._mqtt._publish('fixture-command', payload)
        if self.double_publish:
            await self.robot._mqtt._publish('fixture-command', payload)
        return True

    async def publish(self, topic, payload, timeout=5.0):
        self.assertEqual(topic, 'fixture-command')
        body = json.loads(payload)
        self.assertEqual(body['command'], 'resume')
        if self.publish_error:
            raise self.publish_error
        self.reported['cleanMissionStatus'].update(phase='run', error=0)
        self.state_version = self.state_version + 1 if type(self.state_version) is int else 101
        self.metadata['reported']['cleanMissionStatus']['phase']['timestamp'] = body['time']
        self.metadata['reported']['cleanMissionStatus']['error']['timestamp'] = body['time']
        self.software['state']['reported']['lastCommand'] = dict(body)
        if self.after_publish:
            self.after_publish(body)

    def snapshot(self):
        return {'target_sku': c.TARGET, 'state_stale': False,
            'connection': {'data': {'status': 'connected'}},
            'state': {'data': copy.deepcopy(self.reported)}}

    def owner_plan(self):
        plan = c.prepare('resume', self.snapshot(), owner_brush_retry=True)
        a.grant_explicit_request(plan, 'owner-requested inspected-brush resume retry:offline test')
        return plan

    async def execute(self, plan):
        # One observation pass; do not replace the process/asyncio clock.
        ticks = itertools.count(0, 10)
        clock = SimpleNamespace(time=time.time, monotonic=lambda: next(ticks))
        with patch.object(w, 'time', clock):
            return await w.execute_on_service(plan['plan_id'], self.robot, self.blid, self.raw_publish)

    async def refused(self, plan):
        answer = await self.execute(plan)
        self.assertEqual(answer['stage'], 'failed')
        self.assertEqual(answer['outcome'], 'refused')
        self.assertFalse(answer['may_have_executed'])
        self.assertFalse(answer['retry_permitted'])
        self.raw_publish.assert_not_awaited()
        self.assertIs(self.robot._mqtt._publish, self.initial_publish)
        return answer

    async def test_valid_inspected_brush_retry_gets_only_fixed_shadows_and_one_wire_publish(self):
        plan = self.owner_plan()
        answer = await self.execute(plan)
        self.assertTrue(answer['same_mission_resume_verified'])
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertTrue(answer['fault_cleared_observed'])
        self.assertTrue(answer['fresh_resume_report_verified'])
        self.assertFalse(answer['fault_clear_command_sent'])
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertEqual(self.shadow_reads.count('rw-software'), 3)
        self.assertEqual(self.shadow_reads.count('rw-constatus'), 3)
        self.assertEqual(self.shadow_reads.count('ro-currentstate'), 3)
        self.assertIs(self.robot._mqtt._publish, self.initial_publish)
        reads = list(self.shadow_reads)
        duplicate = await self.execute(plan)
        self.assertTrue(duplicate['duplicate_request'])
        self.assertEqual(self.shadow_reads, reads)
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_changed_native_mission_blocks_initial_and_final_validation(self):
        changes = ({'missionId': 'different'}, {'cycle': 'none'}, {'mssnStrtTm': 0},
            {'mssnStrtTm': 1001}, {'nMssn': 42}, {'operatingMode': 32}, {'phase': 'run'},
            {'error': 5}, {'notReady': 1}, {'condNotReady': [1]}, {'nMssn': True},
            {'operatingMode': False}, {'mssnStrtTm': float('nan')})
        for stage in ('initial', 'final'):
            for change in changes:
                with self.subTest(stage=stage, change=change):
                    self.reset_cloud()
                    plan = self.owner_plan()
                    mutate = lambda: self.reported['cleanMissionStatus'].update(change)
                    self.before_publish = mutate if stage == 'final' else None
                    if stage == 'initial':
                        mutate()
                    await self.refused(plan)

    async def test_bad_battery_blocks_initial_and_final_validation(self):
        for stage in ('initial', 'final'):
            for value in (24, True, '95', None, float('nan'), float('inf'), 101):
                with self.subTest(stage=stage, battery=value):
                    self.reset_cloud()
                    plan = self.owner_plan()
                    mutate = lambda: self.reported.update(batPct=value)
                    self.before_publish = mutate if stage == 'final' else None
                    if stage == 'initial':
                        mutate()
                    await self.refused(plan)

    async def test_stale_or_missing_phase_and_error_proof_blocks_initial_and_final_validation(self):
        for stage in ('initial', 'final'):
            for field in ('phase', 'error'):
                for age in (121, None):
                    with self.subTest(stage=stage, field=field, age=age):
                        self.reset_cloud()
                        plan = self.owner_plan()
                        def mutate():
                            metadata = self.metadata['reported']['cleanMissionStatus']
                            if age is None:
                                metadata.pop(field)
                            else:
                                metadata[field]['timestamp'] = int(time.time()) - age
                        self.before_publish = mutate if stage == 'final' else None
                        if stage == 'initial':
                            mutate()
                        await self.refused(plan)

    async def test_disconnected_or_nonboolean_connection_proof_blocks_initial_and_final_validation(self):
        for stage in ('initial', 'final'):
            for connected in (False, 1, 'true', None):
                with self.subTest(stage=stage, connected=connected):
                    self.reset_cloud()
                    plan = self.owner_plan()
                    mutate = lambda: self.connection['state']['reported'].update(connected=connected)
                    self.before_publish = mutate if stage == 'final' else None
                    if stage == 'initial':
                        mutate()
                    await self.refused(plan)

    async def test_owner_hold_before_initial_or_during_sdk_yield_blocks_publish(self):
        for stage in ('initial', 'final'):
            with self.subTest(stage=stage):
                self.reset_cloud()
                c.save_job('mission-1', {'mission_id': 'mission-1', 'held': False, 'hold_generation': 0})
                plan = self.owner_plan()
                mutate = lambda: c.hold_mission('mission-1', 'stop')
                self.before_publish = mutate if stage == 'final' else None
                if stage == 'initial':
                    mutate()
                await self.refused(plan)
                self.assertTrue(c.job('mission-1')['held'])
                self.assertEqual(c.job('mission-1')['hold_generation'], 1)

    async def test_new_protective_app_command_blocks_initial_and_final_validation(self):
        for stage in ('initial', 'final'):
            for command in ('pause', 'stop', 'dock', 'reset', 'wipe'):
                for encoded in (False, True):
                    with self.subTest(stage=stage, command=command, encoded=encoded):
                        self.reset_cloud()
                        plan = self.owner_plan()
                        def mutate():
                            value = {'command': command, 'initiator': 'rmtApp',
                                     'time': int(plan['created_at'])}
                            self.software['state']['reported']['lastCommand'] = json.dumps(value) if encoded else value
                        self.before_publish = mutate if stage == 'final' else None
                        if stage == 'initial':
                            mutate()
                        await self.refused(plan)

    async def test_owner_hold_after_last_get_is_checked_in_dispatch_transaction(self):
        plan = self.owner_plan()
        real_dispatch = w.record_brush_dispatch
        def fence_then_dispatch(candidate, data):
            c.hold_mission('mission-1', 'stop')
            return real_dispatch(candidate, data)
        with patch.object(w, 'record_brush_dispatch', side_effect=fence_then_dispatch):
            await self.refused(plan)
        self.assertTrue(c.job('mission-1')['held'])
        self.assertEqual(self.shadow_reads.count('rw-software'), 2)

    async def test_old_protective_app_command_does_not_block_current_owner_retry(self):
        plan = self.owner_plan()
        self.software['state']['reported']['lastCommand'] = {
            'command': 'stop', 'initiator': 'rmtApp', 'time': int(plan['created_at']) - 1}
        answer = await self.execute(plan)
        self.assertTrue(answer['same_mission_resume_verified'])
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_protective_command_with_unknown_time_refuses_before_initial_and_final_publish(self):
        for stage in ('initial', 'final'):
            for command in ('pause', 'stop', 'dock', 'reset', 'wipe'):
                for value in ('missing', None, True, False, float('nan'), float('inf'), float('-inf')):
                    with self.subTest(stage=stage, command=command, time=value):
                        self.reset_cloud()
                        plan = self.owner_plan()
                        def mutate():
                            echo = {'command': command, 'initiator': 'rmtApp'}
                            if value != 'missing':
                                echo['time'] = value
                            self.software['state']['reported']['lastCommand'] = echo
                        self.before_publish = mutate if stage == 'final' else None
                        if stage == 'initial':
                            mutate()
                        answer = await self.refused(plan)
                        self.assertIn('BrushRecoveryOwnerCommandUnverified', answer['error_code'])

    def held_plan(self):
        c.save_job('mission-1', {'mission_id': 'mission-1', 'held': True,
            'hold_reason': 'explicit_pause', 'hold_generation': 7, 'finished': False})
        return self.owner_plan()

    async def unconfirmed_with_hold(self, mutation):
        self.reset_cloud()
        self.before_publish = None
        self.raw_publish.reset_mock()
        plan = self.held_plan()
        self.after_publish = mutation
        answer = await self.execute(plan)
        self.assertFalse(answer['robot_action_confirmed'])
        self.assertFalse(answer['same_mission_resume_verified'])
        self.assertFalse(answer['fresh_resume_report_verified'])
        self.assertFalse(answer['fault_cleared_observed'])
        self.assertTrue(answer['may_have_executed'])
        self.assertFalse(answer['retry_permitted'])
        self.assertTrue(c.job('mission-1')['held'])
        self.assertEqual(c.job('mission-1')['hold_generation'], 7)
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertIs(self.robot._mqtt._publish, self.initial_publish)
        return answer

    async def test_cached_run_and_zero_error_cannot_confirm_or_release_hold(self):
        def cached(body):
            self.state_version = 100
            for field in ('phase', 'error'):
                self.metadata['reported']['cleanMissionStatus'][field]['timestamp'] = body['time'] - 10
            self.software['state']['reported']['lastCommand']['time'] = body['time'] - 10
        await self.unconfirmed_with_hold(cached)

    async def test_bad_or_missing_confirmation_stamps_keep_hold(self):
        for field in ('phase', 'error'):
            for value in ('missing', None, True, False, 'timestamp', float('nan'), float('inf'), float('-inf'), 'before'):
                with self.subTest(field=field, stamp=value):
                    def corrupt(body):
                        metadata = self.metadata['reported']['cleanMissionStatus']
                        if value == 'missing':
                            metadata.pop(field)
                        else:
                            metadata[field]['timestamp'] = body['time'] - 1 if value == 'before' else value
                    await self.unconfirmed_with_hold(corrupt)

    async def test_missing_unchanged_older_or_noninteger_confirmation_version_keeps_hold(self):
        for value in (None, 100, 99, True, False, 101.0, '101', float('nan')):
            with self.subTest(version=value):
                await self.unconfirmed_with_hold(lambda body: setattr(self, 'state_version', value))

    async def test_confirmation_version_must_exceed_final_fault_snapshot_not_initial(self):
        self.reset_cloud()
        plan = self.held_plan()
        self.before_publish = lambda: setattr(self, 'state_version', 200)
        self.after_publish = lambda body: setattr(self, 'state_version', 150)
        answer = await self.execute(plan)
        self.assertFalse(answer['robot_action_confirmed'])
        self.assertFalse(answer['same_mission_resume_verified'])
        self.assertTrue(c.job('mission-1')['held'])
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_missing_or_invalid_final_fault_version_cannot_confirm_newer_report(self):
        for value in (None, True, False, 100.0, '100', float('nan')):
            with self.subTest(final_version=value):
                self.reset_cloud()
                self.raw_publish.reset_mock()
                plan = self.held_plan()
                self.before_publish = lambda: setattr(self, 'state_version', value)
                answer = await self.execute(plan)
                self.assertFalse(answer['robot_action_confirmed'])
                self.assertFalse(answer['fresh_resume_report_verified'])
                self.assertTrue(c.job('mission-1')['held'])
                self.assertEqual(self.raw_publish.await_count, 1)

    async def test_confirmation_stamps_cannot_predate_final_fault_snapshot(self):
        for field in ('phase', 'error'):
            with self.subTest(field=field):
                self.reset_cloud()
                self.raw_publish.reset_mock()
                plan = self.held_plan()
                def later_fault_stamp():
                    self.metadata['reported']['cleanMissionStatus'][field]['timestamp'] = int(time.time()) + 3
                self.before_publish = later_fault_stamp
                answer = await self.execute(plan)
                self.assertFalse(answer['robot_action_confirmed'])
                self.assertFalse(answer['fresh_resume_report_verified'])
                self.assertTrue(c.job('mission-1')['held'])
                self.assertEqual(self.raw_publish.await_count, 1)

    async def test_wrong_or_missing_resume_echo_keeps_hold(self):
        cases = ({'command': 'start'}, {'command': 'pause'}, {'initiator': 'rmtApp'},
            {'time': None}, {'time': True}, {'time': float('nan')}, {'time': 'wrong'},
            {'time_offset': -1}, {'time_offset': 1}, {'remove': 'command'},
            {'remove': 'initiator'}, {'remove': 'time'}, {'remove_all': True})
        for change in cases:
            with self.subTest(echo=change):
                def corrupt(body):
                    echo = self.software['state']['reported']['lastCommand']
                    if change.get('remove_all'):
                        self.software['state']['reported'].pop('lastCommand')
                    elif 'remove' in change:
                        echo.pop(change['remove'])
                    elif 'time_offset' in change:
                        echo['time'] = body['time'] + change['time_offset']
                    else:
                        echo.update(change)
                await self.unconfirmed_with_hold(corrupt)

    async def test_not_ready_or_incomplete_running_state_keeps_hold(self):
        cases = ({'error': False}, {'error': 0.0}, {'notReady': True}, {'notReady': False}, {'notReady': 0.0}, {'notReady': 1},
            {'condNotReady': [1]}, {'condNotReady': None}, {'remove': 'error'},
            {'remove': 'notReady'}, {'remove': 'condNotReady'})
        for change in cases:
            with self.subTest(mission=change):
                def corrupt(body):
                    mission = self.reported['cleanMissionStatus']
                    if 'remove' in change:
                        mission.pop(change['remove'])
                    else:
                        mission.update(change)
                await self.unconfirmed_with_hold(corrupt)

    async def test_changed_or_incomplete_postpublish_native_anchor_keeps_hold(self):
        cases = ({'missionId': 'other'}, {'mssnStrtTm': 1001}, {'nMssn': 42},
            {'operatingMode': 32}, {'cycle': 'none'}, {'mssnStrtTm': float('nan')},
            {'nMssn': True}, {'remove': 'operatingMode'})
        for change in cases:
            with self.subTest(anchor=change):
                def corrupt(body):
                    mission = self.reported['cleanMissionStatus']
                    if 'remove' in change:
                        mission.pop(change['remove'])
                    else:
                        mission.update(change)
                await self.unconfirmed_with_hold(corrupt)

    async def test_disconnected_postpublish_proof_keeps_hold(self):
        for value in (False, None, 1, 'true'):
            with self.subTest(connected=value):
                await self.unconfirmed_with_hold(lambda body: self.connection['state']['reported'].update(connected=value))

    async def test_fresh_exact_confirmation_releases_only_matching_old_hold(self):
        plan = self.held_plan()
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertTrue(answer['same_mission_resume_verified'])
        self.assertFalse(c.job('mission-1')['held'])
        self.assertEqual(c.job('mission-1')['hold_generation'], 7)
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_fresh_confirmation_cannot_release_later_owner_hold(self):
        plan = self.held_plan()
        self.after_publish = lambda body: c.hold_mission('mission-1', 'stop')
        answer = await self.execute(plan)
        self.assertTrue(answer['robot_action_confirmed'])
        self.assertTrue(answer['same_mission_resume_verified'])
        self.assertTrue(c.job('mission-1')['held'])
        self.assertEqual(c.job('mission-1')['hold_generation'], 8)
        self.assertEqual(self.raw_publish.await_count, 1)

    async def test_uncertain_publish_is_durable_and_duplicate_cannot_resend(self):
        plan = self.owner_plan()
        self.publish_error = TimeoutError('offline uncertain receipt')
        first = await self.execute(plan)
        self.assertEqual(first['outcome'], 'unknown')
        self.assertTrue(first['may_have_executed'])
        self.assertFalse(first['retry_permitted'])
        self.assertEqual(self.raw_publish.await_count, 1)
        reads = list(self.shadow_reads)
        self.publish_error = None
        duplicate = await self.execute(plan)
        self.assertTrue(duplicate['duplicate_request'])
        self.assertEqual(duplicate['outcome'], 'unknown')
        self.assertTrue(duplicate['may_have_executed'])
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertEqual(self.shadow_reads, reads)
        self.assertIs(self.robot._mqtt._publish, self.initial_publish)

    async def test_sdk_attempted_second_publish_never_reaches_wire(self):
        plan = self.owner_plan()
        self.double_publish = True
        answer = await self.execute(plan)
        self.assertEqual(answer['stage'], 'failed')
        self.assertIn('UnapprovedPublishBlocked', answer['error_code'])
        self.assertTrue(answer['may_have_executed'])
        self.assertEqual(answer['outcome'], 'unknown')
        self.assertFalse(answer['retry_permitted'])
        self.assertEqual(self.raw_publish.await_count, 1)
        self.assertIs(self.robot._mqtt._publish, self.initial_publish)


if __name__ == '__main__':
    unittest.main()
