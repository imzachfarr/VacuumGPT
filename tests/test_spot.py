import asyncio
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import control_layer as c
import direct_control
import dashboard_commands
import supervised_command as w
import write_approval as approval
import spot_control as spot


def feature(geometry):return {'type':'Feature','geometry':geometry}
def box(x=0,y=0,w=10):return {'type':'Polygon','coordinates':[[[x,y],[x+w,y],[x+w,y+w],[x,y+w],[x,y]]]}

class SpotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='vacuumgpt-spot-',dir='/tmp');self.stack=contextlib.ExitStack();self.root=Path(self.temp.name)
        self.stack.enter_context(patch.object(c,'ROOT',self.root));self.stack.enter_context(patch.object(approval,'ROOT',self.root))
        self.stack.enter_context(patch.object(approval,'PENDING',self.root/'.write-approval.json'))
        for module in (c,approval,w):self.stack.enter_context(patch.object(module,'TARGET','fixture-sku'))
        for module in (c,w):self.stack.enter_context(patch.object(module,'FINGERPRINT','fixture-fingerprint'))
        self.stack.enter_context(patch.object(c,'policy',return_value={'direct_chat_commands':True,'automatic_resume':False,'experimental_geometry_commands':True}))
        c.database().close();(self.root/'activation-result.json').write_text(json.dumps({'writes_enabled':True,'write_actions':['start']}))
        self.snapshot={'target_sku':c.TARGET,'state_stale':False,'connection':{'data':{'status':'connected'}},'maps':[{'id':1}],
            'state':{'data':{'cleanMissionStatus':{'phase':'charge','error':0,'missionId':'before'},'p2maps':[{'p2map_id':'map','p2mapv_id':'v1'}]}}}
        self.doc={'files':{'metadata.json':{'id':'map','version':'v1'},'borders.geojson':feature(box()),'policyZones.geojson':{'type':'FeatureCollection','features':[]}}}
        self.points=[[1,1],[3,1],[3,2],[2,2],[2,3],[1,3]]
        self.stack.enter_context(patch.object(c,'read_local',side_effect=lambda path:self.snapshot if path=='/api/status' else self.doc))
    def tearDown(self):self.stack.close();self.temp.cleanup()
    def prepare(self):return c.prepare('clean_area',self.snapshot,map_id='map',map_version='v1',snapshot_id=1,area_points=self.points)
    def test_failed_targeting_blocks_new_and_preexisting_plans(self):
        plan=self.prepare()
        with patch.object(c,'policy',return_value={'direct_chat_commands':True,'experimental_geometry_commands':True,'spot_cleaning_blocked':True}):
            with self.assertRaisesRegex(c.ControlError,'SpotTargetingFailedRequiresCommandFix'):self.prepare()
            with self.assertRaisesRegex(c.ControlError,'SpotTargetingFailedRequiresCommandFix'):w.load_approved(plan['plan_id'])
    def test_exact_polygon_wire_and_no_global_or_room_fallback(self):
        p=self.prepare();body=c.region_command(p,'fixture').to_json()
        self.assertEqual(body['regions'][0]['type'],'tid');self.assertEqual(body['regions'][0]['region_id'],'161')
        self.assertEqual(body['id_multipolys'][0]['id'],'161');self.assertEqual(body['id_multipolys'][0]['poly'],self.points+[self.points[0]])
        body.update(initiator='localApp',time=int(time.time()));self.assertTrue(c.payload_matches(p,body,'fixture'))
        for altered in ({**body,'regions':[]},{**body,'id_multipolys':[]},{**body,'regions':[{'region_id':'10','type':'rid'}]},{**body,'select_all':True}):self.assertFalse(c.payload_matches(p,altered,'fixture'))
    def test_dense_freehand_trace_is_preserved_without_simplification(self):
        import math
        points=[[2+math.cos(i*2*math.pi/320)*.5,2+math.sin(i*2*math.pi/320)*.5] for i in range(320)]
        ring=spot.validate_document(self.doc,points,'vacuum')
        self.assertEqual(ring,points+[points[0]])
        with self.assertRaisesRegex(c.ControlError,'SpotRequires3To4096Points'):
            spot.polygon(points*13)

    def test_self_crossing_and_nonfinite_refused(self):
        for pts in ([[1,1],[4,4],[1,4],[4,1]],[[1,1],[2,2],[3,3]],[[float('nan'),1],[2,1],[2,2]]):
            with self.assertRaises(c.ControlError):spot.validate_document(self.doc,pts,'vacuum')
    def test_edges_and_enclosed_holes_are_not_only_vertex_checked(self):
        doc=copy.deepcopy(self.doc);doc['files']['borders.geojson']=feature({'type':'Polygon','coordinates':[[[0,0],[10,0],[10,10],[6,10],[6,4],[4,4],[4,10],[0,10],[0,0]]]})
        with self.assertRaisesRegex(c.ControlError,'SelectionOutsideHouseBoundary'):spot.validate_document(doc,[[2,2],[8,2],[8,8],[2,8]],'vacuum')
        doc['files']['borders.geojson']=feature(box());doc['files']['borders.geojson']['geometry']['coordinates'].append(box(4,4,2)['coordinates'][0])
        with self.assertRaisesRegex(c.ControlError,'SelectionOutsideHouseBoundary'):spot.validate_document(doc,[[2,2],[8,2],[8,8],[2,8]],'vacuum')
    def test_keepout_overlap_and_missing_policy_geometry_refused(self):
        doc=copy.deepcopy(self.doc);doc['files']['policyZones.geojson']['features']=[feature(box(1.5,1.5,.5))]
        with self.assertRaisesRegex(c.ControlError,'SpotOverlapsKeepOut'):spot.validate_document(doc,self.points,'vacuum')
        del doc['files']['policyZones.geojson']
        with self.assertRaisesRegex(c.ControlError,'SpotKeepOutGeometryRequired'):spot.validate_document(doc,self.points,'vacuum')
    def test_stale_fault_busy_and_wrong_map_cannot_create_plan(self):
        for modifier in ('stale','fault','busy','map'):
            s=copy.deepcopy(self.snapshot)
            if modifier=='stale':s['state_stale']=True
            elif modifier=='fault':s['state']['data']['cleanMissionStatus']['error']=1010
            elif modifier=='busy':s['state']['data']['cleanMissionStatus']['phase']='run'
            else:s['state']['data']['p2maps'][0]['p2map_id']='other'
            with self.assertRaises(c.ControlError):c.prepare('clean_area',s,map_id='map',map_version='v1',snapshot_id=1,area_points=self.points)
    def test_current_geometry_rechecked_when_version_advances(self):
        self.snapshot['state']['data']['p2maps'][0]['p2mapv_id']='v2';self.snapshot['maps']=[{'id':2}]
        current=copy.deepcopy(self.doc);current['files']['metadata.json']['version']='v2'
        with patch.object(c,'read_local',side_effect=lambda path:self.doc if path.endswith('/1') else current):
            p=self.prepare();self.assertEqual(p['map_version'],'v2');self.assertEqual(p['drawn_map_version'],'v1')
        current['files']['policyZones.geojson']['features']=[feature(box(1.5,1.5,.5))]
        with patch.object(c,'read_local',side_effect=lambda path:self.doc if path.endswith('/1') else current):
            with self.assertRaisesRegex(c.ControlError,'SpotOverlapsKeepOut'):self.prepare()
    def test_idempotency_binds_polygon_and_unknown_never_replays(self):
        kwargs={'map_id':'map','map_version':'v1','snapshot_id':1,'area_points':self.points}
        with patch.object(c,'launch_writer',return_value={'outcome':'unknown'}) as writer:
            direct_control.run('clean_area','spot-idempotent',**kwargs);direct_control.run('clean_area','spot-idempotent',**kwargs)
            self.assertEqual(writer.call_count,1)
            with self.assertRaisesRegex(c.ControlError,'RequestIdIntentMismatch'):direct_control.run('clean_area','spot-idempotent',**{**kwargs,'area_points':[[1,1],[4,1],[4,4]]})
        item={'operation':'command','request_id':'spot-http-valid','action':'clean_area',**kwargs};dashboard_commands.validate(item)
        with self.assertRaises(c.ControlError):dashboard_commands.validate({**item,'rooms':['Kitchen']})
    def test_actor_one_polygon_publish_and_no_boundary_confirmation_claim(self):
        p=self.prepare();p['approval_expires_at']=time.time()+120;published=AsyncMock()
        mqtt=SimpleNamespace(_publish=published,cmd_topic=lambda prefix:'fixture-command')
        before={'phase':'charge','error':0,'missionId':'before'};after={'phase':'run','error':0,'missionId':'after'};states=[before,after]
        async def shadow(name):return SimpleNamespace(payload={'state':{'reported':{'cleanMissionStatus':states.pop(0),'p2maps':[{'p2map_id':'map','p2mapv_id':'v1'}]}}})
        async def send(command):
            payload=command.to_json();payload.update(initiator='localApp',time=int(time.time()));await mqtt._publish('fixture-command',json.dumps(payload));return True
        robot=SimpleNamespace(_mqtt=mqtt,_irbt_topic_prefix='fixture',get_named_shadow=shadow,send_routine_command_via_cmd_topic=send,send_simple_command=AsyncMock())
        with patch.object(asyncio,'sleep',AsyncMock()):answer=asyncio.run(w.run_on_robot(p,robot,'fixture',published))
        self.assertEqual(published.await_count,1);robot.send_simple_command.assert_not_called()
        self.assertTrue(answer['robot_action_confirmed']);self.assertFalse(answer['boundary_verified'])
