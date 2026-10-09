import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch
import control_layer as c
import write_approval as a
import supervised_command as w

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.stack=contextlib.ExitStack()
        for module in (c,a):self.stack.enter_context(patch.object(module,'ROOT',self.root))
        self.stack.enter_context(patch.object(a,'PENDING',self.root/'.write-approval.json'))
        (self.root/'activation-result.json').write_text(json.dumps({'writes_enabled':True,'write_actions':list(c.ACTIONS)}))
        self.snapshot={'target_sku':c.TARGET,'state_stale':False,'connection':{'data':{'status':'connected'}},'state':{'data':{'cleanMissionStatus':{'phase':'charge','error':0,'missionId':'mission'}}}}
    def tearDown(self):self.stack.close();self.temp.cleanup()
    def ticket(self,plan,**overrides):
        path=a.PENDING;path.write_text(json.dumps({**plan,'approved_at':time.time(),'expires_at':time.time()+120,**overrides}));path.chmod(0o600)
    def test_missing_approval_never_reads_credentials(self):
        plan=c.prepare('start',self.snapshot)
        with patch.object(w,'credentials') as creds:
            with self.assertRaises(PermissionError):w.execute(plan['plan_id'])
            creds.assert_not_called()
    def test_expiry_mismatch_and_file_permissions_fail_closed(self):
        plan=c.prepare('start',self.snapshot)
        for override in ({'expires_at':0},{'intent_hash':'other'},{'fingerprint':'other'}):
            self.ticket(plan,**override)
            with self.assertRaises(PermissionError):a.consume('start',plan)
        self.ticket(plan);a.PENDING.chmod(0o644)
        with self.assertRaises(PermissionError):a.consume('start',plan)
    def test_duplicate_and_interrupted_dispatch_never_resend(self):
        plan=c.prepare('find',self.snapshot)
        c.record(plan['plan_id'],'dispatching',{'may_have_executed':True,'outcome':'unknown'})
        with patch.object(w,'command') as dispatch:
            answer=w.execute(plan['plan_id']);dispatch.assert_not_called()
        self.assertTrue(answer['duplicate_request']);self.assertFalse(answer['retry_permitted'])
    def test_approval_consumed_once_and_audit_before_execution(self):
        plan=c.prepare('find',self.snapshot);self.ticket(plan)
        async def fake(plan):
            self.assertEqual(c.result(plan['plan_id'])['stage'],'validating')
            c.record(plan['plan_id'],'complete',{'broker_acknowledged':True,'robot_action_confirmed':False})
            return c.result(plan['plan_id'])
        with patch.object(w,'command',fake):answer=w.execute(plan['plan_id'])
        self.assertFalse(a.PENDING.exists());self.assertFalse(answer['robot_action_confirmed'])
    def test_changed_state_fault_and_unknown_phase_refused(self):
        for mission in ({'phase':'run','error':0},{'phase':'charge','error':42},{'phase':'mystery','error':0}):
            with self.assertRaises(c.ControlError):c.preconditions('start',mission)
        self.assertFalse(w.observed('find',{'phase':'charge'},{'phase':'charge','error':0}))
        self.assertFalse(w.observed('dock',{'phase':'charge'},{'phase':'charge','error':0}))
        self.assertTrue(w.observed('pause',{'phase':'run'},{'phase':'pause','error':0}))
    def test_writer_excludes_concurrent_command(self):
        c.database().close()
        with c.exclusive_writer():
            with self.assertRaises(c.ControlError):
                with c.exclusive_writer():pass

    def test_cloud_writer_identity_mission_and_one_publish(self):
        from types import SimpleNamespace
        from roombapy_prime import auth,prime_factory
        import hashlib
        blid='fixture-device'
        fingerprint=hashlib.sha256(blid.encode()).hexdigest()[:16]
        plan=c.prepare('start',self.snapshot);plan['approval_expires_at']=time.time()+120
        publish=AsyncMock()
        mqtt=SimpleNamespace(_publish=publish,cmd_topic=lambda prefix:'fixture-command')
        states=[{'phase':'charge','error':0,'missionId':'mission'},{'phase':'run','error':0,'missionId':'new'}]
        async def shadow(name):return SimpleNamespace(payload={'state':{'reported':{'cleanMissionStatus':states.pop(0)}}})
        async def send(action):
            await mqtt._publish('fixture-command',json.dumps({'command':action,'initiator':'localApp','time':int(time.time())}));return True
        robot=SimpleNamespace(_mqtt=mqtt,_irbt_topic_prefix='fixture',_rest=SimpleNamespace(_request=AsyncMock()),connect=AsyncMock(),disconnect=AsyncMock(),get_named_shadow=shadow,send_simple_command=send)
        login_result=SimpleNamespace(robots={blid:SimpleNamespace(sku=c.TARGET)})
        with patch.object(w,'credentials',return_value=('fixture','fixture','US')),patch.object(w,'FINGERPRINT',fingerprint),patch.object(auth,'login',AsyncMock(return_value=login_result)),patch.object(prime_factory.PrimeFactory,'create_prime_robot',AsyncMock(return_value=robot)),patch.object(asyncio,'sleep',AsyncMock()):
            answer=asyncio.run(w.command(plan))
        self.assertTrue(answer['robot_action_confirmed']);self.assertEqual(publish.await_count,1)
        self.assertTrue(answer['broker_acknowledged'])

    def test_exact_identity_failure_never_creates_writer_robot(self):
        from types import SimpleNamespace
        from roombapy_prime import auth,prime_factory
        plan=c.prepare('start',self.snapshot);plan['approval_expires_at']=time.time()+120
        login_result=SimpleNamespace(robots={'unrelated':SimpleNamespace(sku='another-sku')})
        with patch.object(w,'credentials',return_value=('fixture','fixture','US')),patch.object(auth,'login',AsyncMock(return_value=login_result)),patch.object(prime_factory.PrimeFactory,'create_prime_robot',AsyncMock()) as factory:
            with self.assertRaisesRegex(c.ControlError,'ExactDeviceNotFound'):asyncio.run(w.command(plan))
            factory.assert_not_called()

    def test_room_payload_requires_active_map_exact_geometry_and_one_region(self):
        snapshot=json.loads(json.dumps(self.snapshot))
        snapshot['state']['data']['p2maps']=[{'p2map_id':'map','p2mapv_id':'version'}]
        snapshot['maps']=[{'id':1,'layers':[{'type':'rooms','filepath':'rooms.geojson','retained':True}]}]
        doc={'files':{'metadata.json':{'id':'map','version':'version'},'rooms.geojson':{'features':[{'id':'13','geometry':{'type':'Polygon'}}]}}}
        with patch.object(c,'resolve_room',return_value={'name':'Kitchen','vendor_region_id':'13'}),patch.object(c,'read_local',return_value=doc):
            plan=c.prepare('clean_room',snapshot,'Kitchen')
        payload=c.region_command(plan,'fixture-device').to_json();payload['initiator']='localApp';payload['time']=int(time.time())
        self.assertTrue(c.payload_matches(plan,payload,'fixture-device'))
        self.assertEqual(payload['command'],'start');self.assertEqual(payload['p2map_id'],'map')
        self.assertEqual(payload['regions'],[{'region_id':'13','type':'rid','params':{'operatingMode':2}}])
        for altered in ({**payload,'regions':[]},{**payload,'p2map_id':None},{**payload,'select_all':True},{**payload,'regions':[{'region_id':'14','type':'rid'}]}):
            self.assertFalse(c.payload_matches(plan,altered,'fixture-device'))
        with self.assertRaisesRegex(c.ControlError,'RoomRequired'):c.prepare('clean_room',snapshot)
        doc['files']['metadata.json']['version']='changed'
        with patch.object(c,'resolve_room',return_value={'name':'Kitchen','vendor_region_id':'13'}),patch.object(c,'read_local',return_value=doc):
            with self.assertRaisesRegex(c.ControlError,'VerifiedActiveMap'):c.prepare('clean_room',snapshot,'Kitchen')

    def test_two_independent_tickets_and_append_only_audit(self):
        first=c.prepare('find',self.snapshot);second=c.prepare('find',self.snapshot)
        a.grant_explicit_request(first,'human request one');a.grant_explicit_request(second,'human request two')
        self.assertEqual(a.consume('find',first)['plan_id'],first['plan_id'])
        self.assertEqual(a.consume('find',second)['plan_id'],second['plan_id'])
        with self.assertRaises(PermissionError):a.consume('find',first)
        c.record(first['plan_id'],'dispatching',{'may_have_executed':True})
        c.record(first['plan_id'],'complete',{'outcome':'unknown'})
        self.assertEqual([r['stage'] for r in c.audit(first['plan_id'])],['dispatching','complete'])

if __name__=='__main__':unittest.main()
