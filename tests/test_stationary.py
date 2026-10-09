import contextlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import control_layer as c
import stationary_review as review
from chair_keepout import same_wall,walls_from

class ReviewTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.stack=contextlib.ExitStack();self.root=Path(self.temp.name);self.stack.enter_context(patch.object(c,'ROOT',self.root));c.database().close();(self.root/'observer-data/control-policy.json').write_text(json.dumps({'direct_chat_commands':True}))
 def tearDown(self):self.stack.close();self.temp.cleanup()
 def sample(self,t,x=1,y=2):
  return {'state_stale':False,'pose_stale':False,'state':{'data':{'cleanMissionStatus':{'missionId':'m','phase':'run','cycle':'clean','error':0}}},'pose':{'mission':'m','at':t,'data':{'timestamp':t,'map_id':'map','map_version':'v','points':[{'x':x,'y':y}]}}}
 def incident(self):
  for t in (100,110,120):self.assertIsNone(review.observe(self.sample(t)))
  return review.observe(self.sample(121))
 def test_twenty_seconds_requires_fresh_repeated_positions_and_deduplicates(self):
  incident=self.incident();self.assertEqual(incident['stationary_seconds'],21)
  self.assertIsNone(review.observe(self.sample(125)));self.assertEqual(len(review.reviews()),1)
  self.assertEqual(c.notices()['events'][0]['kind'],'stuck_review')
 def test_movement_gap_stale_and_intentional_hold_do_not_trigger(self):
  for t,x in ((100,0),(110,.3),(120,.6),(131,.9)):self.assertIsNone(review.observe(self.sample(t,x)))
  stale=self.sample(151,.9);stale['pose_stale']=True;self.assertIsNone(review.observe(stale))
  c.save_job('m',{'held':True});self.assertIsNone(review.observe(self.sample(152,.9)))
 def test_temporary_review_never_writes_map(self):
  incident=self.incident()
  with patch('chair_keepout.ban_point') as ban:
   result=review.classify(incident['incident_id'],'temporary');ban.assert_not_called()
  self.assertEqual(result['classification'],'temporary')
 def test_permanent_review_dispatches_once_and_unknown_is_not_retried(self):
  incident=self.incident()
  with patch('chair_keepout.ban_point',return_value={'keepout_verified':False,'outcome':'unknown'}) as ban:
   result=review.classify(incident['incident_id'],'permanent');review.classify(incident['incident_id'],'permanent')
  self.assertEqual(ban.call_count,1);self.assertEqual(result['state'],'ban_unverified')
 def test_rounding_tolerance_preserves_exact_boundary_id_and_type(self):
  self.assertTrue(same_wall(['1',1,-.997244,2.018057],['1',1,-.9972,2.0181]))
  self.assertFalse(same_wall(['1',1,0,0],['2',1,0,0]));self.assertFalse(same_wall(['1',1,0,0],['1',1,.1,0]))
  with self.assertRaises(c.ControlError):walls_from({}, {'geojson_details':{'zones':[{'id':'1'}]}})

if __name__=='__main__':unittest.main()
