import asyncio
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import command_actor
import control_layer as c
import supervised_command as w
import write_approval as a

class ActorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/tmp');self.root=Path(self.temp.name)
        self.stack=contextlib.ExitStack()
        for module in (c,a):self.stack.enter_context(patch.object(module,'ROOT',self.root))
        self.stack.enter_context(patch.object(a,'PENDING',self.root/'.write-approval.json'))
        self.blid='fixture-device';fingerprint=hashlib.sha256(self.blid.encode()).hexdigest()[:16]
        for module in (c,w):self.stack.enter_context(patch.object(module,'FINGERPRINT',fingerprint))
        (self.root/'activation-result.json').write_text(json.dumps({'writes_enabled':True,'write_actions':['start','stop','pause','resume','dock','find']}))
        self.snapshot={'target_sku':c.TARGET,'state_stale':False,'connection':{'data':{'status':'connected'}},'state':{'data':{'cleanMissionStatus':{'phase':'charge','error':0,'missionId':'before'}}}}
        c.database().close()
        self.raw_publish=AsyncMock()
        async def read_only(topic,payload,timeout=5):
            if payload not in ('',b''):raise c.ControlError('WriteBlocked')
            return await self.raw_publish(topic,payload,timeout)
        mqtt=SimpleNamespace(_publish=read_only,cmd_topic=lambda prefix:'fixture-command')
        calls=0
        async def shadow(name):
            nonlocal calls
            calls+=1
            return SimpleNamespace(payload={'state':{'reported':{'cleanMissionStatus':{'phase':'charge' if calls==1 else 'run','error':0,'missionId':'before' if calls==1 else 'after'}}}})
        async def send(action):
            body=json.dumps({'command':action,'initiator':'localApp','time':int(time.time())})
            async def unrelated_task():
                with self.assertRaisesRegex(c.ControlError,'UnapprovedPublish'):await mqtt._publish('fixture-command',body)
            await asyncio.create_task(unrelated_task())
            await mqtt._publish('fixture-command',body);return True
        self.robot=SimpleNamespace(_mqtt=mqtt,_irbt_topic_prefix='fixture',get_named_shadow=shadow,send_simple_command=send)
        self.baseline=mqtt._publish
        self.actor=await command_actor.start(self.robot,self.blid,self.raw_publish)
    async def asyncTearDown(self):
        await command_actor.close(*self.actor);self.stack.close();self.temp.cleanup()
    async def request(self,body):
        reader,writer=await asyncio.open_unix_connection(str(self.actor[1]))
        writer.write((json.dumps(body)+'\n').encode());await writer.drain()
        answer=json.loads(await reader.readline());writer.close();await writer.wait_closed();return answer
    async def test_voice_diagnostic_reads_only_fixed_shadows_without_dispatch(self):
        calls=[]
        async def shadow(name):
            calls.append(name)
            return SimpleNamespace(payload={'state':{'reported':{
                'audio':{'volume':3},'langs2':{'sLang':'en-US'},'password':'must-not-return'}}})
        self.robot.get_named_shadow=shadow
        answer=await self.request({'diagnostic':'voice_read'})
        self.assertTrue(answer['read_only'])
        self.assertEqual(calls,list(command_actor.VOICE_SHADOW_FIELDS))
        self.assertEqual(answer['shadows']['rw-settings']['audio'],{'volume':3})
        self.assertNotIn('must-not-return',json.dumps(answer))
        self.raw_publish.assert_not_awaited()
        denied=await self.request({'diagnostic':'voice_read','shadow':'rw-software'})
        self.assertIn('error_code',denied)
        self.assertEqual(len(calls),3)

    async def test_voice_diagnostic_failure_does_not_expose_raw_exception(self):
        async def shadow(name):raise RuntimeError('private token value')
        self.robot.get_named_shadow=shadow
        answer=await self.request({'diagnostic':'voice_read'})
        self.assertEqual(answer['shadows']['rw-settings'],{'error_type':'RuntimeError'})
        self.assertNotIn('private token',json.dumps(answer))
    async def test_shared_connection_once_and_other_task_cannot_publish(self):
        plan=c.prepare('start',self.snapshot);a.grant_explicit_request(plan,'explicit fixture owner request')
        with patch.object(asyncio,'sleep',AsyncMock()),patch.object(w,'credentials') as creds:
            answer=await self.request({'plan_id':plan['plan_id']})
            duplicate=await self.request({'plan_id':plan['plan_id']})
            creds.assert_not_called()
        self.assertTrue(answer['broker_acknowledged']);self.assertTrue(answer['robot_action_confirmed'])
        self.assertEqual(answer['mqtt_owner'],'observer_session');self.assertTrue(duplicate['duplicate_request'])
        self.assertEqual(self.raw_publish.await_count,1)
        self.assertIs(self.robot._mqtt._publish,self.baseline)
        self.assertEqual(self.actor[1].stat().st_mode&0o777,0o600)
    async def test_socket_refuses_unapproved_and_arbitrary_commands(self):
        plan=c.prepare('find',self.snapshot)
        answer=await self.request({'plan_id':plan['plan_id']})
        self.assertEqual(answer['error_code'],'LocalActionApprovalRequired')
        answer=await self.request({'command':'start'})
        self.assertEqual(answer['error_code'],'OnlyPlanIdAccepted')
        self.raw_publish.assert_not_called()

    async def test_exact_room_dispatch_uses_one_region_and_retains_managed_job(self):
        snapshot=json.loads(json.dumps(self.snapshot))
        snapshot['state']['data']['p2maps']=[{'p2map_id':'map','p2mapv_id':'version'}]
        snapshot['maps']=[{'id':1,'layers':[{'type':'rooms','filepath':'rooms.geojson','retained':True}]}]
        doc={'files':{'metadata.json':{'id':'map','version':'version'},'rooms.geojson':{'features':[{'id':'13','geometry':{'type':'Polygon'}}]}}}
        with patch.object(c,'resolve_room',return_value={'name':'Kitchen','vendor_region_id':'13'}),patch.object(c,'read_local',return_value=doc):
            plan=c.prepare('clean_room',snapshot,'Kitchen')
        a.grant_explicit_request(plan,'explicit fixture room request')
        states=[{'phase':'charge','error':0,'missionId':'before'},{'phase':'run','error':0,'missionId':'regional'}]
        async def shadow(name):
            return SimpleNamespace(payload={'state':{'reported':{'cleanMissionStatus':states.pop(0),'p2maps':[{'p2map_id':'map','p2mapv_id':'version'}]}}})
        async def send(command):
            body=command.to_json();body.update(initiator='localApp',time=int(time.time()))
            await self.robot._mqtt._publish('fixture-command',json.dumps(body));return True
        self.robot.get_named_shadow=shadow
        self.robot.send_routine_command_via_cmd_topic=send
        with patch.object(asyncio,'sleep',AsyncMock()):answer=await self.request({'plan_id':plan['plan_id']})
        self.assertTrue(answer['robot_action_confirmed'])
        payload=json.loads(self.raw_publish.await_args.args[1])
        self.assertEqual(payload['regions'],[{'region_id':'13','type':'rid','params':{'operatingMode':2}}])
        self.assertEqual(self.raw_publish.await_count,1)
        self.assertEqual(c.job('regional')['room'],'Kitchen')

    async def test_recovery_through_actor_resumes_same_mission_without_start(self):
        import mission_monitor
        now=time.time()
        current={'batPct':80,'cleanMissionStatus':{'phase':'pause','error':0,'notReady':0,'condNotReady':[],
            'missionId':'same','initiator':'localApp','cycle':'clean'}}
        snapshot={**self.snapshot,'state':{'data':current}}
        (self.root/'observer-data/control-policy.json').write_text(json.dumps({'automatic_resume':True}))
        c.save_job('same',{'mission_id':'same','started_at':now-120,'pause_since':now-60,'held':False,'finished':False,
            'automatic_resume':True,'resume_count':1,'last_resume_at':now,'room':'Kitchen'})
        states=[current['cleanMissionStatus'],{**current['cleanMissionStatus'],'phase':'run'}]
        async def shadow(name):return SimpleNamespace(payload={'state':{'reported':{'cleanMissionStatus':states.pop(0)}}})
        async def send(action):
            self.assertEqual(action,'resume')
            await self.robot._mqtt._publish('fixture-command',json.dumps({'command':action,'initiator':'localApp','time':int(time.time())}));return True
        self.robot.get_named_shadow=shadow;self.robot.send_simple_command=send
        with patch.object(c,'read_local',return_value=snapshot),patch.object(asyncio,'sleep',AsyncMock()):
            answer=await asyncio.to_thread(mission_monitor.recover,{'mission_id':'same','incident':'one','room':'Kitchen'})
        self.assertTrue(answer['robot_action_confirmed']);self.assertTrue(answer['same_mission'])
        self.assertEqual(self.raw_publish.await_count,1)
        self.assertEqual(c.notices()['events'][-1]['kind'],'recovery_result')

if __name__=='__main__':unittest.main()
