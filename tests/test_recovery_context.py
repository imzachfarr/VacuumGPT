import copy
import unittest
from unittest.mock import patch
import recovery_store
import control_layer as c
import recovery_context as rc
import map_recovery as mr

class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.anchor = {'mission_id': 'm', 'mission_start': 500, 'n_mission': 1, 'cycle': 'clean', 'operating_mode': 32}
        self.map = {'map_id': 'map', 'version': 'v1', 'topology_hash': 'h', 'region_ids': ['12']}
        self.job = {'verified_native_pause_origins': ['robot'], 'mission_id': 'm', 'native_anchor': self.anchor, 'recovery_map_context': self.map}
        self.current = {'batPct': 80, 'p2maps': [{'p2map_id': 'map', 'p2mapv_id': 'v2'}], 'cleanMissionStatus':
            {'missionId': 'm', 'mssnStrtTm': 500, 'nMssn': 1, 'cycle': 'clean', 'operatingMode': 32,
             'phase': 'pause', 'error': 0, 'notReady': 0, 'condNotReady': [], 'initiator': 'localApp'},
            '_recovery': {'connected': True, 'phase_reported_at': 1000, 'map_context': {**self.map, 'version': 'v2'},
                          'last_command': {'command': 'pause', 'initiator': 'robot', 'time': 1000}}}
    def test_confirmed_resume_consumes_the_one_pause_authorization(self):
        job = {**self.job, 'held': False, 'finished': False, 'hold_generation': 4,
               'authorized_pause': {'command_time': 1000, 'generation': 4}}
        plan = {'action': 'resume', 'mission_id': 'm', 'expected_hold_generation': 4}
        result = {'robot_action_confirmed': True, 'native_anchor': self.anchor}
        saved = []
        def transaction(mid, callback):
            updated, value = callback(job, None);saved.append(updated);return value
        with patch.object(recovery_store, 'transaction', side_effect=transaction):
            c.track_result(plan, result)
        self.assertIsNone(saved[0]['authorized_pause'])
    def test_exact_stop_clean_pause_requires_echo_and_retained_context(self):
        current = copy.deepcopy(self.current);current['cleanMissionStatus']['phase'] = 'stop'
        self.assertTrue(rc.is_paused(current));self.assertTrue(rc.validate(current, self.job, 1046)[0])
        current['_recovery']['last_command']['command'] = 'stop'
        self.assertFalse(rc.is_paused(current))
        current['_recovery']['last_command']['command'] = 'pause'
        current['cleanMissionStatus']['cycle'] = 'none'
        self.assertFalse(rc.is_paused(current));self.assertFalse(rc.validate(current, self.job, 1046)[0])
    def test_nonfinite_battery_and_native_start_are_rejected(self):
        for value in (float('nan'), float('inf')):
            current = copy.deepcopy(self.current);current['batPct'] = value
            self.assertFalse(rc.validate(current, self.job, 1046)[0])
            job = copy.deepcopy(self.job);job['native_anchor']['mission_start'] = value
            self.assertFalse(rc.validate(self.current, job, 1046)[0])
    def test_unverified_native_pause_origin_stays_capability_disabled(self):
        job = dict(self.job);job.pop('verified_native_pause_origins')
        self.assertEqual(rc.validate(self.current, job, 1046)[1], 'PauseOriginUnverified')
    def test_version_upload_changes_do_not_block_identical_target_geometry(self):
        self.assertEqual(rc.validate(self.current, self.job, 1046), (True, 'SameNativeMissionPausedAndReady'))
    def test_cached_get_never_substitutes_for_fresh_robot_report(self):
        current = copy.deepcopy(self.current);current['_recovery'].update(checked_at=2000)
        self.assertEqual(rc.validate(current, self.job, 2000)[1], 'RobotPauseTelemetryStale')
    def test_start_initiator_does_not_authorize_app_pause(self):
        current = copy.deepcopy(self.current);current['_recovery']['last_command']['initiator'] = 'rmtApp'
        self.assertEqual(rc.validate(current, self.job, 1046)[1], 'PauseOriginUnverified')
    def test_missing_readiness_cycle_and_changed_native_anchor_block(self):
        for field in ('condNotReady', 'cycle', 'mssnStrtTm', 'nMssn', 'operatingMode'):
            current = copy.deepcopy(self.current);current['cleanMissionStatus'].pop(field)
            self.assertFalse(rc.validate(current, self.job, 1046)[0], field)
        current = copy.deepcopy(self.current);current['cleanMissionStatus']['mssnStrtTm'] = 0
        self.assertFalse(rc.validate(current, self.job, 1046)[0])
    def test_real_room_or_keepout_edit_changes_digest_but_coverage_does_not(self):
        ring = [[0,0],[1,0],[1,1],[0,1],[0,0]]
        doc = {'files': {'metadata.json': {'id': 'map', 'version': 'v1', 'sourceFormat': 'picea'},
            'rooms.geojson': {'features': [{'id': '12', 'geometry': {'type': 'Polygon', 'coordinates': [ring]}}]},
            'policyZones.geojson': {'features': []}, 'coverage.geojson': {'features': []}}}
        first = mr.context(doc, ['12']);changed = copy.deepcopy(doc)
        changed['files']['metadata.json'].update(version='v2', sourceMetadata={'mapUploadTime': 10})
        changed['files']['coverage.geojson']['features'] = [{'arbitrary': True}]
        self.assertEqual(first['topology_hash'], mr.context(changed, ['12'])['topology_hash'])
        changed['files']['rooms.geojson']['features'][0]['geometry']['coordinates'][0][1][0] = 2
        self.assertNotEqual(first['topology_hash'], mr.context(changed, ['12'])['topology_hash'])
    def test_map_proof_must_match_current_device_version(self):
        current = copy.deepcopy(self.current);current['_recovery']['map_context']['version'] = 'v3'
        self.assertEqual(rc.validate(current, self.job, 1046)[1], 'RecoveryMapEvidenceNotCurrent')
    def test_linear_virtual_wall_is_hashed_and_changes_block_recovery(self):
        doc = {'files': {'metadata.json': {'id': 'map', 'version': 'v1'},
            'rooms.geojson': {'features': [{'id': '12', 'geometry': {'type': 'Polygon', 'coordinates': [[[0,0],[1,0],[1,1],[0,0]]]}}]},
            'policyZones.geojson': {'features': [{'id': 'wall', 'geometry': {'type': 'LineString', 'coordinates': [[0,0],[0,1]]}, 'properties': {'type': 'virtualWall'}}]}}}
        first = mr.context(doc, ['12'])
        doc['files']['policyZones.geojson']['features'][0]['geometry']['coordinates'][1][1] = 2
        self.assertNotEqual(first['topology_hash'], mr.context(doc, ['12'])['topology_hash'])
    def _test_pause(self):
        current = copy.deepcopy(self.current)
        current['_recovery']['last_command']['initiator'] = 'localApp'
        job = {**self.job, 'held': True, 'hold_reason': 'explicit_pause',
               'automatic_resume': True, 'finished': False, 'hold_generation': 0}
        plan = {'action': 'pause', 'mission_id': 'm', 'created_at': 990, 'expires_at': 1600}
        receipt = {'robot_action_confirmed': True, 'mission_id': 'm', 'hold_generation': 0, 'command_time': 1000}
        def transaction(mid, callback):
            updated, result = callback(job, None)
            self.assertEqual(updated['authorized_pause']['command_time'], 1000)
            self.assertFalse(updated['held'])
            return result
        return current, job, plan, receipt, transaction
    def test_controlled_pause_authorizes_only_exact_confirmed_command(self):
        current, job, plan, receipt, transaction = self._test_pause()
        with patch.object(c, 'get_plan', return_value=plan), patch.object(c, 'result', return_value=receipt), patch.object(rc.time, 'time', return_value=1046), patch.object(recovery_store, 'transaction', side_effect=transaction):
            result = rc.authorize_test_pause('fixture', {'state': {'data': current}})
        self.assertTrue(result['one_pause_authorized']);self.assertFalse(result['command_sent'])
    def test_old_test_receipt_cannot_authorize_later_external_pause(self):
        current, job, plan, receipt, transaction = self._test_pause()
        current['_recovery']['last_command']['time'] = 1100
        current['_recovery']['phase_reported_at'] = 1100
        with patch.object(c, 'get_plan', return_value=plan), patch.object(c, 'result', return_value=receipt), patch.object(recovery_store, 'transaction') as update:
            with self.assertRaisesRegex(c.ControlError, 'EchoUnverified'):
                rc.authorize_test_pause('fixture', {'state': {'data': current}})
            update.assert_not_called()
    def test_native_anchor_rejects_boolean_or_incomplete_counter_and_mode(self):
        for key, value in (('n_mission', True), ('n_mission', None), ('operating_mode', None), ('operating_mode', False)):
            job = copy.deepcopy(self.job);job['native_anchor'][key] = value
            self.assertFalse(rc.validate(self.current, job, 1046)[0])
        current = copy.deepcopy(self.current);current['cleanMissionStatus']['notReady'] = False
        self.assertFalse(rc.validate(current, self.job, 1046)[0])

if __name__ == '__main__': unittest.main()
