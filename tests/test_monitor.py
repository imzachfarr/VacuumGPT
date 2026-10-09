import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import control_layer as c
import mission_monitor as monitor
import direct_control
import write_approval

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.stack=contextlib.ExitStack()
        for module in (c,write_approval):self.stack.enter_context(patch.object(module,'ROOT',self.root))
        self.stack.enter_context(patch.object(write_approval,'PENDING',self.root/'.write-approval.json'))
        c.database().close()
        (self.root/'observer-data/control-policy.json').write_text(json.dumps({'automatic_resume':True,'direct_chat_commands':True}))
        self.job={'mission_id':'mission','room':'Kitchen','map_id':'map','started_at':500,'last_phase':'run','held':False,'automatic_resume':True,'resume_count':0,'finished':False}
        self.current={'batPct':80,'p2maps':[{'p2map_id':'map'}],'cleanMissionStatus':{'missionId':'mission','phase':'pause','error':0,'notReady':0,'condNotReady':[],'cycle':'clean','initiator':'localApp'}}
        c.save_job('mission',self.job)
    def tearDown(self):self.stack.close();self.temp.cleanup()
    def test_pause_debounce_durable_claim_and_no_duplicate_resume(self):
        self.assertIsNone(monitor.evaluate(self.current,1000))
        request=monitor.evaluate(self.current,1046)
        self.assertEqual(request['mission_id'],'mission')
        self.assertIsNone(monitor.evaluate(self.current,1047))
        self.assertEqual(c.job('mission')['resume_count'],1)
        self.assertEqual(len(c.notices()['events']),1)
    def test_fault_readiness_battery_map_origin_and_manual_hold_block_recovery(self):
        baseline={**self.job,'pause_since':800}
        for change in ({'error':48},{'notReady':1},{'condNotReady':[22]},{'initiator':'rmtApp'},{'missionId':'other'}):
            current=json.loads(json.dumps(self.current));current['cleanMissionStatus'].update(change)
            self.assertFalse(monitor.recovery_allowed(current,baseline,c.policy(),1000)[0])
        for change in ({'batPct':10},{'p2maps':[]}):
            self.assertFalse(monitor.recovery_allowed({**self.current,**change},baseline,c.policy(),1000)[0])
        self.assertFalse(monitor.recovery_allowed(self.current,{**baseline,'held':True},c.policy(),1000)[0])
    def test_completion_never_restarts_and_notices_survive_reopen(self):
        current=json.loads(json.dumps(self.current));current['cleanMissionStatus'].update(phase='charge',cycle='none')
        self.assertIsNone(monitor.evaluate(current,1000));self.assertTrue(c.job('mission')['finished'])
        self.assertIsNone(monitor.evaluate(self.current,2000))
        notice=c.notices()['events'][0];self.assertEqual(notice['kind'],'mission_ended')
        self.assertEqual(c.notices(notice['id'])['events'],[])
    def test_request_id_repetition_and_intent_collision_do_not_send(self):
        plan={'plan_id':'fixture-plan','action':'clean_room'}
        with patch.object(c,'prepare',return_value=plan),patch.object(c,'launch_writer',return_value={'outcome':'unknown'}) as writer,patch.object(write_approval,'grant_explicit_request'):
            first=direct_control.run('clean_room','request-123','Kitchen')
            second=direct_control.run('clean_room','request-123','Kitchen')
            self.assertEqual(first['outcome'],'unknown');self.assertEqual(writer.call_count,1)
            self.assertFalse(second['retry_permitted'])
            with self.assertRaisesRegex(c.ControlError,'IntentMismatch'):direct_control.run('clean_room','request-123','Office')
    def test_policy_revocation_blocks_direct_control(self):
        (self.root/'observer-data/control-policy.json').write_text('{}')
        with patch.object(c,'launch_writer') as writer:
            with self.assertRaisesRegex(c.ControlError,'PersistentRoomControlGrant'):direct_control.run('clean_room','request-123','Kitchen')
            writer.assert_not_called()

    def test_existing_mission_enrollment_requires_confirmed_exact_receipt(self):
        self.job.update(plan_id='confirmed-plan',automatic_resume=False)
        c.save_job('mission',self.job)
        (self.root/'observer-data/control-policy.json').write_text(json.dumps({'owner_approved':True,
            'automatic_resume':True,'authorized_existing_mission':'mission'}))
        with patch.object(c,'result',return_value={'robot_action_confirmed':True,'mission_id':'other'}):
            monitor.evaluate(self.current,1000)
        self.assertFalse(c.job('mission')['automatic_resume'])
        with patch.object(c,'result',return_value={'robot_action_confirmed':True,'mission_id':'mission'}):
            request=monitor.evaluate(self.current,1046)
        self.assertTrue(c.job('mission')['automatic_resume']);self.assertEqual(request['mission_id'],'mission')

if __name__=='__main__':unittest.main()
