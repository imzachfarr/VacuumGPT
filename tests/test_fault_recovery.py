import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import control_layer as c
import direct_control as d
import write_approval as a
import supervised_command as w

class FaultRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.stack=contextlib.ExitStack()
        for module in (c,a):self.stack.enter_context(patch.object(module,'ROOT',self.root))
        self.stack.enter_context(patch.object(a,'PENDING',self.root/'.write-approval.json'))
        (self.root/'activation-result.json').write_text(json.dumps({'write_actions':list(c.ACTIONS)}))
        c.database().close()
        self.mission={'phase':'stuck','error':1010,'missionId':'mission-1','notReady':0,'condNotReady':[]}
        self.snapshot={'target_sku':c.TARGET,'state_stale':False,'connection':{'data':{'status':'connected'}},'state':{'data':{'cleanMissionStatus':self.mission}}}
        self.stack.enter_context(patch.object(c,'policy',return_value={'direct_chat_commands':True}))
        self.stack.enter_context(patch.object(c,'read_local',return_value=self.snapshot))
    def tearDown(self):self.stack.close();self.tmp.cleanup()
    def test_only_complete_navigation_fault_allowed(self):
        c.preconditions('dock',self.mission,True)
        for change in ({'error':0},{'error':5},{'error':1025},{'phase':'run'},{'missionId':None},{'notReady':1},{'condNotReady':[7]}):
            with self.subTest(change=change),self.assertRaises(c.ControlError):c.preconditions('dock',{**self.mission,**change},True)
        for key in ('notReady','condNotReady'):
            state=dict(self.mission);state.pop(key)
            with self.assertRaises(c.ControlError):c.preconditions('dock',state,True)
        for action in ('start','resume','clean_room','clean_area'):
            with self.assertRaises(c.ControlError):c.preconditions(action,self.mission,True)
        with self.assertRaises(c.ControlError):c.preconditions('dock',self.mission)
    def test_duplicate_unknown_never_resends_and_mission_bounded(self):
        def unknown(plan_id):
            c.record(plan_id,'dispatch_uncertain',{'outcome':'unknown','may_have_executed':True})
            return c.result(plan_id)
        with patch.object(c,'launch_writer',side_effect=unknown) as send:
            first=d.run('dock','fault-request-001',owner_navigation_retry=True)
            second=d.run('dock','fault-request-001',owner_navigation_retry=True)
            self.assertEqual(first,second);self.assertEqual(send.call_count,1)
            with patch('time.time',return_value=2000000000):
                with self.assertRaisesRegex(c.ControlError,'AlreadyClaimedForMission'):d.run('dock','fault-request-002',owner_navigation_retry=True)
            self.assertEqual(send.call_count,1)
        plan=c.get_plan(first['plan_id']);self.assertEqual(plan['expected_error'],1010)
    def test_cooldown_global_limit_and_stale_device(self):
        with patch.object(c,'launch_writer',return_value={'outcome':'unknown'}) as send:
            with patch('time.time',return_value=1000000):d.run('dock','fault-request-101',owner_navigation_retry=True)
            self.mission['missionId']='mission-2'
            with patch('time.time',return_value=1000010),self.assertRaisesRegex(c.ControlError,'Cooldown'):d.run('dock','fault-request-102',owner_navigation_retry=True)
            with patch('time.time',return_value=1000181):d.run('dock','fault-request-103',owner_navigation_retry=True)
            self.mission['missionId']='mission-3'
            with patch('time.time',return_value=1000400),self.assertRaisesRegex(c.ControlError,'TwoInSixHours'):d.run('dock','fault-request-104',owner_navigation_retry=True)
            self.assertEqual(send.call_count,2)
            self.snapshot['state_stale']=True
            with self.assertRaisesRegex(c.ControlError,'FreshExactDevice'):d.run('dock','fault-request-105',owner_navigation_retry=True)
    def test_fresh_fault_change_blocks_publish(self):
        import asyncio,time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        plan=c.prepare('dock',owner_navigation_retry=True);plan['approval_expires_at']=time.time()+120
        publish=AsyncMock();mqtt=SimpleNamespace(_publish=publish,cmd_topic=lambda prefix:'fixture-command')
        robot=SimpleNamespace(_mqtt=mqtt,_irbt_topic_prefix='fixture',get_named_shadow=AsyncMock(return_value=SimpleNamespace(payload={'state':{'reported':{'cleanMissionStatus':{**self.mission,'error':5}}}})))
        with self.assertRaisesRegex(c.ControlError,'FaultChangedSinceOwnerRequest'):asyncio.run(w.run_on_robot(plan,robot,'fixture',publish))
        publish.assert_not_called()

if __name__=='__main__':unittest.main()
