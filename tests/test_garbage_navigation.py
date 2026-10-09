import asyncio
import copy
import json
import time
import unittest
from unittest.mock import patch
import garbage_navigation as g
import control_layer as c

class NavigationTests(unittest.TestCase):
    def test_arrival_requires_fresh_same_mission_and_map(self):
        plan={'map_id':'house','destination':[3.,-2.]};s={'pose_stale':False,'pose':{'at':101,'age_seconds':1,'mission':'trip','data':{'map_id':'house','points':[{'x':3.1,'y':-2.}]}}}
        self.assertAlmostEqual(g.distance(s,plan,'trip',100),.1)
        for mutate in (lambda x:x.update(pose_stale=True),lambda x:x['pose'].update(mission='old'),lambda x:x['pose'].update(at=99),lambda x:x['pose']['data'].update(map_id='other'),lambda x:x['pose'].update(age_seconds=20),lambda x:x['pose']['data']['points'][0].update(x=float('nan'))):
            q=copy.deepcopy(s);mutate(q);self.assertIsNone(g.distance(q,plan,'trip',100))

    def test_exact_point_payload_refuses_broader_or_changed_target(self):
        plan={'action':'drive_garbage','destination':[3.,-2.],'map_id':'house','map_version':'v1'}
        body=c.region_command(plan,'robot').to_json();body.update(initiator='localApp',time=int(time.time()))
        self.assertTrue(c.payload_matches(plan,body,'robot'))
        for change in ({'command':'start'},{'geom':{'center':[0,0]}},{'select_all':True},{'p2map_id':'other'},{'params':{'operatingMode':32}}):
            self.assertFalse(c.payload_matches(plan,{**body,**change},'robot'))

    def run_trip(self,cancel=False,pause_confirm=True):
        plan={'action':'drive_garbage','plan_id':'test','mission_id':'old','phase':'charge','map_id':'house','map_version':'v1','destination':[3.,-2.], 'location_revision':1,'approval_expires_at':time.time()+120,'area_points':[[2.8,-2.2],[3.2,-2.2],[3.2,-1.8],[2.8,-2.2]],'temporary_region_id':'161','operating_mode':2}
        sent=[];records=[]
        class MQTT:
            _publish=None
            def cmd_topic(self,prefix):return 'cmd'
        class Robot:
            _mqtt=MQTT();_irbt_topic_prefix='prefix';phase='charge'
            async def get_named_shadow(self,name):
                return type('Reply',(),{'payload':{'state':{'reported':{'cleanMissionStatus':{'missionId':'old' if self.phase=='charge' else 'trip','phase':self.phase,'error':0,'notReady':0,'condNotReady':[]},'p2maps':[{'p2map_id':'house','p2mapv_id':'v1'}]}}}})()
            async def send_routine_command_via_cmd_topic(self,cmd):
                body=cmd.to_json();body.update(initiator='localApp',time=int(time.time()));await self._mqtt._publish('cmd',json.dumps(body));self.phase='run';return True
            async def send_simple_command(self,action):
                await self._mqtt._publish('cmd',json.dumps({'command':action,'initiator':'localApp','time':int(time.time())}));self.phase='pause' if pause_confirm else 'run';return True
        async def publish(topic,payload,timeout):sent.append(json.loads(payload));return True
        async def no_wait(n):pass
        status={'pose_stale':False,'pose':{'at':time.time()+1,'age_seconds':0,'mission':'trip','data':{'map_id':'house','points':[{'x':3.1,'y':-2.}]}}}
        with patch('named_locations.get',return_value={'location':{'revision':1,'point':[3.,-2.]}}),patch.object(c,'read_local',return_value=status),patch.object(c,'record',side_effect=lambda p,s,d:records.append(copy.deepcopy(d))),patch.object(c,'result',side_effect=lambda p:records[-1]),patch.object(c,'save_job'),patch.object(c,'hold_mission'),patch.object(g,'cancellation',return_value=cancel),patch.object(g.asyncio,'sleep',no_wait):
            answer=asyncio.run(g.run(plan,Robot(),'robot',publish))
        self.assertEqual([s['command'] for s in sent],['point_clean','pause']);self.assertFalse(sent[0].get('select_all'));self.assertEqual(sent[0]['geom'],{'center':[3.,-2.]});self.assertNotIn('regions',sent[0])
        return answer
    def test_arrived_only_after_pause_confirmed(self):
        self.assertTrue(self.run_trip()['arrival_confirmed'])
        self.assertFalse(self.run_trip(pause_confirm=False)['arrival_confirmed'])
    def test_owner_pause_does_not_claim_arrival(self):
        r=self.run_trip(cancel=True);self.assertEqual(r['outcome'],'owner_paused');self.assertFalse(r['arrival_confirmed'])
