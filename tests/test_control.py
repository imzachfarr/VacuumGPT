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
        self.temp=tempfile.TemporaryDirectory(prefix='vacuumgpt-control-',dir='/tmp');self.root=Path(self.temp.name)
        self.stack=contextlib.ExitStack()
        for module in (c,a):self.stack.enter_context(patch.object(module,'ROOT',self.root))
        for module in (c,a,w):self.stack.enter_context(patch.object(module,'TARGET','fixture-sku'))
        for module in (c,w):self.stack.enter_context(patch.object(module,'FINGERPRINT','fixture-fingerprint'))
        self.stack.enter_context(patch.object(a,'PENDING',self.root/'.write-approval.json'))
        (self.root/'activation-result.json').write_text(json.dumps({'writes_enabled':True,'write_actions':list(c.ACTIONS)}))
        self.snapshot={'target_sku':c.TARGET,'state_stale':False,'connection':{'data':{'status':'connected'}},'state':{'data':{'cleanMissionStatus':{'phase':'charge','error':0,'missionId':'mission'}}}}
    def tearDown(self):self.stack.close();self.temp.cleanup()
    def ticket(self,plan,**overrides):
        path=a.PENDING;path.write_text(json.dumps({**plan,'approved_at':time.time(),'expires_at':time.time()+120,**overrides}));path.chmod(0o600)
    def paused_snapshot(self,phase='pause'):
        snapshot=json.loads(json.dumps(self.snapshot))
        snapshot['state']['data']['cleanMissionStatus'].update(
            phase=phase,cycle='clean',mssnStrtTm=1000,nMssn=0,
            operatingMode=0,error=0,notReady=0,condNotReady=[])
        return snapshot

    def test_ordinary_resume_accepts_complete_clean_anchor_and_zero_counters(self):
        for phase in ('pause','paused'):
            for started in (1000,1000.5):
                snapshot=self.paused_snapshot(phase)
                snapshot['state']['data']['cleanMissionStatus']['mssnStrtTm']=started
                with self.subTest(phase=phase,start=started):
                    plan=c.prepare('resume',snapshot)
                    self.assertEqual(plan['action'],'resume')
                    self.assertEqual(plan['mission_id'],'mission')
                    self.assertEqual(plan['phase'],phase)
                    expected={'mission_id':'mission','mission_start':started,'n_mission':0,
                              'cycle':'clean','operating_mode':0}
                    self.assertEqual(plan['expected_native_anchor'],expected)
                    self.assertEqual(c.get_plan(plan['plan_id'])['expected_native_anchor'],expected)

    def test_ordinary_resume_refuses_ended_or_nonclean_mission(self):
        for phase in ('pause','paused'):
            for cycle in ('none','train','evac','',None):
                snapshot=self.paused_snapshot(phase)
                snapshot['state']['data']['cleanMissionStatus']['cycle']=cycle
                with self.subTest(phase=phase,cycle=cycle),self.assertRaisesRegex(c.ControlError,'^ResumeRequiresActiveNativeMission$'):
                    c.prepare('resume',snapshot)

    def test_ordinary_resume_refuses_missing_native_anchor_fields(self):
        for field in ('missionId','cycle','mssnStrtTm','nMssn','operatingMode'):
            snapshot=self.paused_snapshot()
            snapshot['state']['data']['cleanMissionStatus'].pop(field)
            expected='ResumeRequiresExistingMissionIdentity' if field=='missionId' else 'ResumeRequiresActiveNativeMission'
            with self.subTest(field=field),self.assertRaisesRegex(c.ControlError,'^'+expected+'$'):
                c.prepare('resume',snapshot)

    def test_ordinary_resume_refuses_invalid_and_boolean_native_anchors(self):
        invalid={
            'missionId':('',None,True,False,0,0.0,1,[],{},['mission']),
            'mssnStrtTm':(0,-1,None,True,False,'1000',float('nan'),float('inf'),float('-inf')),
            'nMssn':(-1,None,True,False,0.0,1.5,'0',float('nan'),float('inf')),
            'operatingMode':(-1,None,True,False,0.0,1.5,'0',float('nan'),float('inf')),
        }
        for field,values in invalid.items():
            for value in values:
                snapshot=self.paused_snapshot()
                snapshot['state']['data']['cleanMissionStatus'][field]=value
                expected='ResumeRequiresExistingMissionIdentity' if field=='missionId' and not value else 'ResumeRequiresActiveNativeMission'
                with self.subTest(field=field,value=value),self.assertRaisesRegex(c.ControlError,'^'+expected+'$'):
                    c.prepare('resume',snapshot)

    def test_complete_native_anchor_does_not_relax_current_fault_and_readiness_gates(self):
        for change in ({'error':2},{'error':5},{'error':True},{'error':1.0},{'error':'0'},
                       {'notReady':1},{'notReady':True},{'notReady':'0'},
                       {'condNotReady':[1]},{'condNotReady':True},{'condNotReady':'blocked'}):
            snapshot=self.paused_snapshot()
            snapshot['state']['data']['cleanMissionStatus'].update(change)
            with self.subTest(change=change),self.assertRaisesRegex(c.ControlError,'^RobotFaultRequiresOwner$'):
                c.prepare('resume',snapshot)

    def test_ordinary_resume_refuses_missing_readiness_evidence(self):
        for field in ('error','notReady','condNotReady'):
            snapshot=self.paused_snapshot()
            snapshot['state']['data']['cleanMissionStatus'].pop(field)
            expected='IncompleteRobotState' if field=='error' else 'ResumeRequiresReadyRobot'
            with self.subTest(field=field),self.assertRaisesRegex(c.ControlError,'^'+expected+'$'):
                c.prepare('resume',snapshot)

    def test_ordinary_resume_refuses_falsey_malformed_readiness(self):
        invalid={
            'error':(None,False,0.0,[],{},'',()),
            'notReady':(None,False,0.0,[],{},'',()),
            'condNotReady':(None,False,0,0.0,'',(),{}),
        }
        for field,values in invalid.items():
            for value in values:
                snapshot=self.paused_snapshot()
                snapshot['state']['data']['cleanMissionStatus'][field]=value
                with self.subTest(field=field,value=value),self.assertRaisesRegex(c.ControlError,'^ResumeRequiresReadyRobot$'):
                    c.prepare('resume',snapshot)

    def test_inspected_brush_anchor_and_fault_errors_remain_specific(self):
        snapshot=self.paused_snapshot('stuck')
        snapshot['state']['data']['batPct']=95
        snapshot['state']['data']['cleanMissionStatus']['error']=2
        self.assertTrue(c.prepare('resume',snapshot,owner_brush_retry=True)['owner_brush_retry'])
        for change in ({'cycle':'none'},{'nMssn':True},{'mssnStrtTm':float('nan')}):
            mission={**snapshot['state']['data']['cleanMissionStatus'],**change}
            with self.subTest(change=change),self.assertRaisesRegex(c.ControlError,'^BrushRecoveryRequiresActiveNativeMission$'):
                c.preconditions('resume',mission,owner_brush_retry=True)
        for change in ({'error':5},{'notReady':1},{'phase':'pause'}):
            mission={**snapshot['state']['data']['cleanMissionStatus'],**change}
            with self.subTest(change=change),self.assertRaisesRegex(c.ControlError,'^InspectedBrushRetryRequiresExactReadyError2$'):
                c.preconditions('resume',mission,owner_brush_retry=True)

    def test_retained_stop_echo_cannot_bypass_missing_or_boolean_counters(self):
        import recovery_context as rc
        for field in ('nMssn','operatingMode'):
            for value in ('missing',True,False,0.0,None):
                snapshot=self.retained_stop_snapshot()
                mission=snapshot['state']['data']['cleanMissionStatus']
                if value=='missing':mission.pop(field)
                else:mission[field]=value
                self.assertTrue(rc.is_paused(snapshot['state']['data']))
                with self.subTest(field=field,value=value),self.assertRaisesRegex(c.ControlError,'^ResumeRequiresActiveNativeMission$'):
                    c.prepare('resume',snapshot)
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

    def test_recovery_context_is_hashed_copied_and_matches_mission(self):
        c.hold_mission('mission','pause')
        snapshot=self.paused_snapshot()
        context={'mission_id':'mission','generation':1,'incident':'pause-1','attempt_id':'attempt-1'}
        plan=c.prepare('resume',snapshot,recovery_context=context)
        self.assertEqual(plan['expected_hold_generation'],1)
        self.assertEqual(plan['recovery_context'],context)
        import hashlib
        unsigned={key:value for key,value in plan.items() if key!='intent_hash'}
        self.assertEqual(plan['intent_hash'],hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest())
        context['generation']=99
        self.assertEqual(c.get_plan(plan['plan_id'])['recovery_context']['generation'],1)
        with self.assertRaisesRegex(c.ControlError,'ContextMissionMismatch'):
            c.prepare('resume',snapshot,recovery_context={'mission_id':'other'})
        with self.assertRaisesRegex(c.ControlError,'ContextMustBeObject'):
            c.prepare('resume',snapshot,recovery_context=[])

    def test_confirmed_room_job_retains_full_intent_native_anchor_and_map_context(self):
        import sys
        from types import ModuleType
        from unittest.mock import Mock
        snapshot=json.loads(json.dumps(self.snapshot))
        snapshot['state']['data']['p2maps']=[{'p2map_id':'map','p2mapv_id':'version'}]
        snapshot['identity']={'data':{'capabilities':{'suctionLvl':4}}}
        map_context={'map_id':'map','version':'version','topology_hash':'topology','region_ids':['13','14']}
        helper=ModuleType('map_recovery');helper.from_plan=Mock(return_value=map_context)
        def room(name,map_id):return {'name':name,'vendor_region_id':'13' if name=='Kitchen' else '14'}
        with patch.dict(sys.modules,{'map_recovery':helper}),patch.object(c,'resolve_room',side_effect=room),patch.object(c,'validate_retained_region',return_value=7):
            plan=c.prepare('clean_room',snapshot,rooms=['Kitchen','Office'],mode='vacuum_and_mop',ordered=True,suction=3,passes=2)
        helper.from_plan.assert_called_once()
        self.assertEqual(plan['recovery_map_context'],map_context)
        self.assertEqual(c.get_plan(plan['plan_id'])['recovery_map_context'],map_context)
        anchor={'mission_id':'new','mission_start':123,'n_mission':7,'cycle':'clean','operating_mode':32}
        with patch.object(c,'policy',return_value={'automatic_resume':True}):
            c.track_result(plan,{'robot_action_confirmed':True,'mission_id':'new','native_anchor':anchor})
        tracked=c.job('new')
        self.assertEqual(tracked['native_anchor'],anchor)
        self.assertEqual(tracked['recovery_map_context'],map_context)
        for field,value in {'rooms':['Kitchen','Office'],'region_ids':['13','14'],'ordered':True,'mode':'vacuum_and_mop','suction':3,'passes':2,'operating_mode':32}.items():
            self.assertEqual(tracked[field],value)
            self.assertEqual(tracked['intent'][field],value)
        self.assertTrue(tracked['automatic_resume'])
        anchor['mission_start']=999
        map_context['topology_hash']='changed'
        self.assertEqual(c.job('new')['native_anchor']['mission_start'],123)
        self.assertEqual(c.job('new')['recovery_map_context']['topology_hash'],'topology')

    def test_confirmed_start_preserves_owner_fence_and_existing_recovery_budget(self):
        from recovery_store import transaction
        plan=c.prepare('start',self.snapshot)
        c.hold_mission('new','stop')
        transaction('new',lambda current,db:({**current,'resume_count':2,'finished':True},None))
        c.track_result(plan,{'robot_action_confirmed':True,'mission_id':'new'})
        tracked=c.job('new')
        self.assertTrue(tracked['held'])
        self.assertEqual(tracked['hold_reason'],'explicit_stop')
        self.assertEqual(tracked['hold_generation'],1)
        self.assertEqual(tracked['resume_count'],2)
        self.assertTrue(tracked['finished'])
        self.assertIn('started_at',tracked)

    def test_new_mission_does_not_inherit_old_mission_hold(self):
        c.hold_mission('mission','stop')
        plan=c.prepare('start',self.snapshot)
        c.track_result(plan,{'robot_action_confirmed':True,'mission_id':'new'})
        self.assertTrue(c.job('mission')['held'])
        self.assertFalse(c.job('new')['held'])
        self.assertEqual(c.job('new')['hold_generation'],0)
        self.assertIsNone(c.job('new')['native_anchor'])
        self.assertIsNone(c.job('new')['recovery_map_context'])

    def test_confirmed_resume_cannot_clear_owner_fence_after_prepare(self):
        snapshot=self.paused_snapshot()
        c.hold_mission('mission','pause')
        plan=c.prepare('resume',snapshot)
        c.hold_mission('mission','stop')
        c.track_result(plan,{'robot_action_confirmed':True})
        tracked=c.job('mission')
        self.assertTrue(tracked['held'])
        self.assertEqual(tracked['hold_generation'],2)
        self.assertEqual(tracked['hold_reason'],'explicit_stop')

    def test_confirmed_explicit_resume_releases_only_captured_old_hold(self):
        snapshot=self.paused_snapshot()
        c.hold_mission('mission','pause')
        plan=c.prepare('resume',snapshot)
        c.track_result(plan,{'robot_action_confirmed':True})
        self.assertFalse(c.job('mission')['held'])
        self.assertIsNone(c.job('mission')['hold_reason'])
        self.assertEqual(c.job('mission')['hold_generation'],1)

    def test_automatic_resume_result_requires_its_same_incident_and_claim(self):
        from recovery_store import transaction
        snapshot=self.paused_snapshot()
        c.hold_mission('mission','pause')
        transaction('mission',lambda current,db:({**current,'pause_incident':'incident','recovery_attempt_id':'current-attempt'},None))
        context={'mission_id':'mission','generation':1,'incident':'incident','attempt_id':'older-attempt'}
        plan=c.prepare('resume',snapshot,recovery_context=context)
        c.track_result(plan,{'robot_action_confirmed':True})
        self.assertTrue(c.job('mission')['held'])
        context['attempt_id']='current-attempt'
        plan=c.prepare('resume',snapshot,recovery_context=context)
        c.track_result(plan,{'robot_action_confirmed':True})
        self.assertFalse(c.job('mission')['held'])

    def test_unconfirmed_result_never_releases_hold_or_creates_recovery_job(self):
        snapshot=self.paused_snapshot()
        c.hold_mission('mission','pause')
        plan=c.prepare('resume',snapshot)
        c.track_result(plan,{'robot_action_confirmed':False})
        self.assertTrue(c.job('mission')['held'])
        start=c.prepare('start',self.snapshot)
        with patch.object(c,'notify') as notification:
            c.track_result(start,{'robot_action_confirmed':False,'mission_id':'new','outcome':'unknown'})
        self.assertIsNone(c.job('new'))
        notification.assert_called_once()

    def test_whole_house_start_retains_active_map_without_regional_payload(self):
        import map_recovery
        snapshot=json.loads(json.dumps(self.snapshot))
        snapshot['state']['data']['p2maps']=[{'p2map_id':'map','p2mapv_id':'version'}]
        context={'map_id':'map','version':'version','topology_hash':'all-rooms','region_ids':['13','14']}
        with patch.object(map_recovery,'from_plan',return_value=context) as retained:
            plan=c.prepare('start',snapshot)
        retained.assert_called_once()
        self.assertEqual(plan['map_id'],'map')
        self.assertEqual(plan['map_version'],'version')
        self.assertEqual(plan['recovery_map_context'],context)
        self.assertNotIn('region_ids',plan)
        payload={'command':'start','initiator':'localApp','time':int(time.time())}
        self.assertTrue(c.payload_matches(plan,payload,'fixture-device'))
        c.track_result(plan,{'robot_action_confirmed':True,'mission_id':'new'})
        self.assertEqual(c.job('new')['recovery_map_context'],context)
        self.assertEqual(c.job('new')['map_id'],'map')
        self.assertIsNone(c.job('new')['region_ids'])

    def test_whole_house_start_does_not_require_recovery_geometry(self):
        import map_recovery
        for maps in (None,[],[{'p2map_id':'missing-version'}],[{'p2map_id':'one','p2mapv_id':'v'},{'p2map_id':'two','p2mapv_id':'v'}]):
            snapshot=json.loads(json.dumps(self.snapshot))
            snapshot['state']['data']['p2maps']=maps
            with patch.object(map_recovery,'from_plan',return_value=None):
                plan=c.prepare('start',snapshot)
            self.assertIsNone(plan['recovery_map_context'])
            self.assertNotIn('map_id',plan)
        snapshot=json.loads(json.dumps(self.snapshot))
        snapshot['state']['data']['p2maps']=[{'p2map_id':'map','p2mapv_id':'version'}]
        with patch.object(map_recovery,'from_plan',side_effect=c.ControlError('RecoveryMapTargetIncomplete')):
            plan=c.prepare('start',snapshot)
        self.assertIsNone(plan['recovery_map_context'])
        self.assertEqual(plan['map_id'],'map')

    def retained_stop_snapshot(self):
        snapshot=json.loads(json.dumps(self.snapshot))
        now=time.time()
        snapshot['state']['data']['cleanMissionStatus'].update(
            phase='stop',cycle='clean',mssnStrtTm=now-600,nMssn=40,
            operatingMode=6,error=0,notReady=0,condNotReady=[])
        snapshot['state']['data']['_recovery']={
            'connected':True,'checked_at':now,'phase_reported_at':now,
            'last_command':{'command':'pause','initiator':'localApp','time':now-1}}
        return snapshot

    def test_retained_stop_pause_requires_positive_echo_and_native_cycle(self):
        import recovery_context as rc
        snapshot=self.retained_stop_snapshot()
        self.assertTrue(rc.is_paused(snapshot['state']['data']))
        pause=c.prepare('pause',snapshot)
        self.assertTrue(pause['retained_stop_pause'])
        self.assertTrue(pause['no_op'])
        resume=c.prepare('resume',snapshot)
        self.assertTrue(resume['retained_stop_pause'])
        self.assertEqual(resume['expected_native_anchor'],{
            'mission_id':'mission','mission_start':snapshot['state']['data']['cleanMissionStatus']['mssnStrtTm'],
            'n_mission':40,'cycle':'clean','operating_mode':6})
        self.assertNotIn('no_op',resume)
        self.assertEqual(c.get_plan(resume['plan_id'])['retained_stop_pause'],True)
        payload={'command':'resume','initiator':'localApp','time':int(time.time())}
        self.assertTrue(c.payload_matches(resume,payload,'fixture-device'))

    def test_retained_stop_without_pause_echo_is_not_pause_or_resume(self):
        import recovery_context as rc
        for evidence in ({},{'phase_reported_at':time.time(),'last_command':{'command':'stop','time':time.time()}},
                         {'phase_reported_at':time.time(),'last_command':{'command':'pause','time':time.time()-90}}):
            snapshot=self.retained_stop_snapshot()
            snapshot['state']['data']['_recovery']=evidence
            self.assertFalse(rc.is_paused(snapshot['state']['data']))
            for action in ('pause','resume'):
                with self.assertRaises(c.ControlError):c.prepare(action,snapshot)

    def test_ended_or_rebooted_mission_cannot_be_resumed_as_retained_pause(self):
        import recovery_context as rc
        for changes in ({'cycle':'none'},{'mssnStrtTm':0},{'mssnStrtTm':float('nan')},
                        {'phase':'stopped','cycle':'none','mssnStrtTm':0}):
            snapshot=self.retained_stop_snapshot()
            snapshot['state']['data']['cleanMissionStatus'].update(changes)
            self.assertFalse(rc.is_paused(snapshot['state']['data']))
            for action in ('pause','resume'):
                with self.assertRaises(c.ControlError):c.prepare(action,snapshot)

    def test_retained_pause_flag_cannot_bypass_fault_or_readiness_checks(self):
        healthy=self.retained_stop_snapshot()['state']['data']['cleanMissionStatus']
        for changes in ({'error':2},{'notReady':1},{'condNotReady':['blocked']},{'cycle':'none'},
                        {'mssnStrtTm':0},{'mssnStrtTm':float('inf')},{'mssnStrtTm':True},
                        {'missionId':None},{'phase':'stuck'}):
            mission={**healthy,**changes}
            for action in ('pause','resume'):
                with self.assertRaisesRegex(c.ControlError,'RetainedPauseRequires'):
                    c.preconditions(action,mission,retained_stop_pause=True)
        with self.assertRaisesRegex(c.ControlError,'ProofMustBeBoolean'):
            c.preconditions('resume',healthy,retained_stop_pause=1)
        with self.assertRaises(c.ControlError):c.preconditions('resume',healthy)
        c.preconditions('resume',healthy,retained_stop_pause=True)

    def test_active_cycle_cannot_be_replaced_by_any_new_cleaning_start(self):
        # Exercise the mission guard after explicitly opting into geometry.
        with patch.object(c,'policy',return_value={'experimental_geometry_commands':True}):
            for phase in ('stop','charge','idle'):
                snapshot=self.retained_stop_snapshot()
                snapshot['state']['data']['cleanMissionStatus']['phase']=phase
                # The active cycle alone protects the mission; a pause echo is
                # required for resume but is not required to reject replacement.
                snapshot['state']['data']['_recovery']={}
                for action in ('start','clean_room','clean_area'):
                    with self.assertRaisesRegex(c.ControlError,'ActiveMissionRequiresResumeOrStop'):
                        c.prepare(action,snapshot,room='Office' if action=='clean_room' else None)

    def test_ended_stop_and_charge_cycles_allow_basic_start(self):
        for phase in ('stop','charge'):
            snapshot=self.retained_stop_snapshot()
            mission=snapshot['state']['data']['cleanMissionStatus']
            mission.update(phase=phase,cycle='none',mssnStrtTm=0)
            for action in ('start','clean_room','clean_area'):
                c.preconditions(action,mission)
            plan=c.prepare('start',snapshot)
            self.assertEqual(plan['action'],'start')
            self.assertNotIn('retained_stop_pause',plan)

    def test_missing_legacy_cycle_does_not_change_existing_start_preconditions(self):
        mission={'phase':'charge','error':0,'missionId':'fixture'}
        for action in ('start','clean_room','clean_area'):
            c.preconditions(action,mission)

if __name__=='__main__':unittest.main()
