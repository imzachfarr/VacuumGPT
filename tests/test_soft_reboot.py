import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import control_layer as c
import direct_control
import write_approval as a
import supervised_command as w
import soft_reboot as r
import recovery_diagnostics as diag

class SoftRebootTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/tmp');self.root=Path(self.tmp.name)
        self.stack=contextlib.ExitStack()
        for m in (c,a):self.stack.enter_context(patch.object(m,'ROOT',self.root))
        self.stack.enter_context(patch.object(a,'PENDING',self.root/'.write-approval.json'))
        (self.root/'activation-result.json').write_text(json.dumps({'writes_enabled':True,'write_actions':list(c.ACTIONS)}))
        c.database().close()
        self.mission={'phase':'stuck','error':2,'cycle':'none','notReady':0,'condNotReady':[],
            'missionId':'mission','nMssn':39,'mssnStrtTm':100}
        self.reported={'cleanMissionStatus':self.mission,'batPct':90}
        self.snapshot={'target_sku':c.TARGET,'state_stale':False,'connection':{'data':{'status':'connected'}},'state':{'data':self.reported}}
    def tearDown(self):self.stack.close();self.tmp.cleanup()
    def test_only_stopped_exact_ready_fault_and_one_use_scope(self):
        for changes in ({'phase':'run'},{'error':3},{'cycle':'clean'},{'notReady':1},{'condNotReady':[22]},{'missionId':None}):
            with self.subTest(changes=changes),self.assertRaises(c.ControlError):r.validate({**self.reported,'cleanMissionStatus':{**self.mission,**changes}})
        for changes in ({'target_sku':'other'},{'state_stale':True},{'connection':{}}):
            with self.assertRaises(c.ControlError):r.prepare({**self.snapshot,**changes})
        with self.assertRaises(c.ControlError):r.validate({**self.reported,'batPct':20})
        with self.assertRaises(c.ControlError):c.prepare('soft_reboot',self.snapshot)
        plan=r.prepare(self.snapshot)
        a.grant_explicit_request(plan,'owner-granted chat request:ordinary')
        with self.assertRaises(PermissionError):w.load_approved(plan['plan_id'])
        a.grant_explicit_request(plan,r.OWNER_PREFIX+'direct human request')
        self.assertEqual(w.load_approved(plan['plan_id'])['action'],'soft_reboot')
        with self.assertRaises(PermissionError):w.load_approved(plan['plan_id'])
    def test_only_exact_non_wipe_wire_command(self):
        plan=r.prepare(self.snapshot);body={'command':'reset','initiator':'rmtApp','time':int(time.time())}
        self.assertTrue(c.payload_matches(plan,body,'fixture'))
        for changes in ({'command':'wipe'},{'command':'resume'},{'initiator':'localApp'},{'send_wipe':True},{'command':'REMOVEEXCEPTION'}):
            with self.subTest(changes=changes):
                self.assertFalse(c.payload_matches(plan,{**body,**changes},'fixture'))
    def test_uncertain_claim_never_repeats(self):
        def unknown(plan_id):
            c.record(plan_id,'dispatch_uncertain',{'outcome':'unknown','may_have_executed':True})
            return c.result(plan_id)
        with patch.object(c,'read_local',return_value=self.snapshot),patch.object(r,'preserve',return_value='private-backup'),patch.object(c,'launch_writer',side_effect=unknown) as send:
            first=r.run('reboot-test-001','fresh human request')
            second=r.run('reboot-test-001','fresh human request')
            self.assertEqual(first['plan_id'],second['plan_id']);self.assertEqual(send.call_count,1)
            with self.assertRaisesRegex(c.ControlError,'AlreadyClaimed'):r.run('reboot-test-002','fresh human request')
            self.assertEqual(send.call_count,1)
    def test_live_state_changed_never_sends(self):
        plan=r.prepare(self.snapshot);plan['approval_expires_at']=time.time()+120
        publish=AsyncMock();mqtt=SimpleNamespace(_publish=publish,cmd_topic=lambda x:'cmd')
        robot=SimpleNamespace(_mqtt=mqtt,_irbt_topic_prefix='fixture',send_simple_command=AsyncMock(),get_named_shadow=AsyncMock(return_value=SimpleNamespace(payload={'state':{'reported':{**self.reported,'cleanMissionStatus':{**self.mission,'error':1}}}})))
        with self.assertRaises(c.ControlError):asyncio.run(w.run_on_robot(plan,robot,'fixture',publish))
        publish.assert_not_called();robot.send_simple_command.assert_not_called()
    def test_fresh_zero_required_and_no_motion_chain(self):
        plan=r.prepare(self.snapshot);plan['approval_expires_at']=time.time()+120
        publish=AsyncMock();mqtt=SimpleNamespace(_publish=publish,cmd_topic=lambda x:'cmd')
        calls=0
        before={'state':{'reported':self.reported},'metadata':{'reported':{'cleanMissionStatus':{'error':{'timestamp':1}}}}}
        async def shadow(name):
            nonlocal calls
            if name=='ro-currentstate':
                calls+=1
                if calls==1:return SimpleNamespace(payload=before)
                # A cached zero does not establish clearance; require a fresh report.
                m={**self.mission,'error':0,'phase':'idle','missionId':'new'}
                stamp=1 if calls==2 else int(time.time())
                return SimpleNamespace(payload={'state':{'reported':{'cleanMissionStatus':m,'batPct':90}},'metadata':{'reported':{'cleanMissionStatus':{'error':{'timestamp':stamp}}}}})
            return SimpleNamespace(payload={'state':{'reported':{'connected':calls>=2,'lastCommand':{'command':'reset','time':int(time.time()),'password':'secret'}}}})
        async def send(action,initiator):
            await mqtt._publish('cmd',json.dumps({'command':action,'initiator':initiator,'time':int(time.time())}));return True
        robot=SimpleNamespace(_mqtt=mqtt,_irbt_topic_prefix='fixture',get_named_shadow=shadow,send_simple_command=send)
        with patch.object(asyncio,'sleep',AsyncMock()):answer=asyncio.run(w.run_on_robot(plan,robot,'fixture',publish))
        self.assertEqual(publish.await_count,1);self.assertEqual(calls,3)
        self.assertTrue(answer['fault_cleared_observed']);self.assertTrue(answer['device_reconnected_observed']);self.assertTrue(answer['robot_action_confirmed']);self.assertFalse(answer['same_mission'])
        self.assertFalse(answer['same_mission_resume_verified']);self.assertFalse(answer['automatic_followup_command_sent'])
        self.assertNotIn('secret',json.dumps(answer))
        self.assertEqual(json.loads(publish.await_args.args[1])['command'],'reset')

if __name__=='__main__':unittest.main()
