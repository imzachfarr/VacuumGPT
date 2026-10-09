import contextlib
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import control_layer as c
import local_schedules as schedules
import area_selection

class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.stack=contextlib.ExitStack();self.root=Path(self.temp.name)
        self.stack.enter_context(patch.object(c,'ROOT',self.root));c.database().close()
        (self.root/'observer-data/control-policy.json').write_text(json.dumps({'direct_chat_commands':True}))
        self.snapshot={'target_sku':c.TARGET,'state_stale':False,'connection':{'data':{'status':'connected'}},
            'identity':{'data':{'capabilities':{'suctionLvl':4}}},'maps':[{'id':1}],
            'state':{'data':{'cleanMissionStatus':{'phase':'charge','error':0,'missionId':'old'},'p2maps':[{'p2map_id':'map','p2mapv_id':'v1'}]}}}
    def tearDown(self):self.stack.close();self.temp.cleanup()
    def test_multiroom_order_and_settings_cannot_widen_payload(self):
        with patch.object(c,'resolve_room',side_effect=lambda name,map_id:{'name':name,'vendor_region_id':{'Kitchen':'13','Office':'12'}[name]}),patch.object(c,'validate_retained_region',return_value=1):
            plan=c.prepare('clean_room',self.snapshot,rooms=['Kitchen','Office'],ordered=True,suction=4,passes=2)
        payload=c.region_command(plan,'fixture').to_json()
        self.assertEqual(payload['ordered'],1);self.assertEqual([r['region_id'] for r in payload['regions']],['13','12'])
        self.assertEqual(payload['regions'][0]['params'],{'operatingMode':2,'suctionLevel':4,'twoPass':True})
        for rooms in ([],['Kitchen','Kitchen']):
            with self.assertRaises(c.ControlError):c.prepare('clean_room',self.snapshot,rooms=rooms)
        for settings in ({'suction':0},{'suction':True},{'passes':3}):
            with self.assertRaises(c.ControlError):c.prepare('clean_room',self.snapshot,rooms=['Kitchen'],**settings)
    def schedule_fixture(self,now):
        local=datetime.fromtimestamp(now,ZoneInfo('America/New_York'))
        data={'rooms':['Kitchen'],'local_time':local.strftime('%H:%M'),'weekdays':[local.weekday()],
            'timezone':'America/New_York','not_before':now-100,'mode':'vacuum','ordered':True,'suction':None,'passes':1,'map_id':'map','map_version':'v1'}
        with contextlib.closing(schedules.db()) as db,db:db.execute('INSERT INTO schedules VALUES(?,1,?)',('fixture',json.dumps(data)))
    def test_schedule_claim_prevents_unknown_delivery_retry(self):
        now=1791651605;self.schedule_fixture(now)
        with patch.object(c,'read_local',return_value=self.snapshot),patch('direct_control.run',return_value={'outcome':'unknown'}) as run:
            self.assertEqual(schedules.tick(now)[0]['outcome'],'unknown')
            self.assertEqual(schedules.tick(now+10),[]);self.assertEqual(run.call_count,1)
    def test_busy_schedule_skips_without_disrupting_existing_mission(self):
        now=1791651605;self.schedule_fixture(now);self.snapshot['state']['data']['cleanMissionStatus']['phase']='run'
        with patch.object(c,'read_local',return_value=self.snapshot),patch('direct_control.run') as run:
            self.assertEqual(schedules.tick(now)[0]['outcome'],'skipped');run.assert_not_called()
        schedules.set_enabled('fixture',False);self.assertFalse(schedules.list_schedules()[0]['enabled'])
        schedules.delete('fixture');self.assertEqual(schedules.list_schedules(),[])
    def test_area_draft_binds_map_and_rejects_outside_coordinates(self):
        doc={'files':{'metadata.json':{'id':'map','version':'v1'},'borders.geojson':{'type':'FeatureCollection','features':[{'geometry':{'type':'Polygon','coordinates':[[[0,0],[100,0],[100,100],[0,100],[0,0]]]}}]}}}
        with patch.object(c,'read_local',side_effect=lambda path:self.snapshot if path=='/api/status' else doc):
            draft=area_selection.save('Table','map','v1',[[10,10],[20,10],[20,20],[10,20]])
            self.assertFalse(draft['dispatch_available']);self.assertEqual(len(area_selection.list_areas()),1)
            for points in ([[200,200],[220,200],[220,220]],[[float('nan'),0],[20,10],[20,20]]):
                with self.assertRaises(c.ControlError):area_selection.save('Bad','map','v1',points)
            with self.assertRaises(c.ControlError):area_selection.save('Old map','map','v2',[[10,10],[20,10],[20,20]])

    def test_area_draft_uses_retained_version_after_active_map_advances(self):
        self.snapshot['state_stale']=True
        self.snapshot['state']['data']['p2maps']=[{'p2map_id':'map','p2mapv_id':'v2'}]
        doc={'files':{'metadata.json':{'id':'map','version':'v1'},'borders.geojson':{'type':'FeatureCollection','features':[{'geometry':{'type':'Polygon','coordinates':[[[0,0],[100,0],[100,100],[0,100],[0,0]]]}}]}}}
        with patch.object(c,'read_local',side_effect=lambda path:self.snapshot if path=='/api/status' else doc),patch('direct_control.run') as run:
            draft=area_selection.save('Drawn snapshot','map','v1',[[10,10],[20,10],[20,20]])
            self.assertEqual(draft['map_version'],'v1')
            self.assertFalse(draft['dispatch_available'])
            self.assertEqual(draft['robot_commands_sent'],0)
            with self.assertRaisesRegex(c.ControlError,'RetainedMapVersionRequired'):
                area_selection.save('Unretained','map','v2',[[10,10],[20,10],[20,20]])
            run.assert_not_called()

    def test_area_draft_accepts_real_multipolygon_feature_and_small_areas(self):
        boundary={'type':'Feature','geometry':{'type':'MultiPolygon','coordinates':[[[[0,0],[10,0],[10,10],[0,10],[0,0]],[[4,4],[6,4],[6,6],[4,6],[4,4]]]]}}
        doc={'files':{'metadata.json':{'id':'map','version':'v1'},'borders.geojson':boundary}}
        with patch.object(c,'read_local',side_effect=lambda path:self.snapshot if path=='/api/status' else doc):
            draft=area_selection.save('Small area','map','v1',[[1,1],[1.5,1],[1.5,1.5],[1,1.5]])
            self.assertFalse(draft['dispatch_available'])
            for points in ([[4.5,4.5],[5.5,4.5],[5.5,5.5]],[[11,11],[12,11],[12,12]],[[1,1],[2,2],[3,3]]):
                with self.assertRaises(c.ControlError):area_selection.save('Invalid','map','v1',points)

if __name__=='__main__':unittest.main()
