import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import recovery_diagnostics as d


class BrushDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.answer={'shadows':{
            'ro-currentstate':{'mission':{'phase':'stuck','cycle':'clean','error':2,
                'notReady':0,'condNotReady':[],'missionId':'retained','mssnStrtTm':800,
                'nMssn':41,'operatingMode':6},'battery':76,'phase_reported_at':990,'error_reported_at':990,
                'shadow_timestamp':1000},
            'rw-constatus':{'connected':True},
            'ro-stats':{'unmapped_fault_field_present':True,'unmapped_fault_present':False}}}

    def test_retained_clean_fault_is_only_candidate_and_does_not_grant(self):
        result=d.assessment(self.answer,now=1000)
        self.assertTrue(result['standard_inspected_resume_candidate'])
        self.assertTrue(result['physical_inspection_required_for_retry'])
        self.assertFalse(result['claim_budget_checked'])
        self.assertFalse(result['fault_override_supported'])
        self.assertFalse(result['unfinished_only_fault_recovery_verified'])

    def test_ended_fault_and_retained_id_cannot_become_resume_candidate(self):
        self.answer['shadows']['ro-currentstate']['mission']['cycle']='none'
        result=d.assessment(self.answer,now=1000)
        self.assertFalse(result['standard_inspected_resume_candidate'])
        self.assertEqual(result['reported_mission_context'],'ended_or_retention_unverified')
        self.assertIn('explicit new target',result['next_step'])

    def test_fresh_cloud_get_does_not_refresh_robot_fault_evidence(self):
        self.answer['shadows']['ro-currentstate'].update(phase_reported_at=800,error_reported_at=800,shadow_timestamp=1000)
        result=d.assessment(self.answer,now=1000)
        self.assertFalse(result['standard_inspected_resume_candidate'])
        self.assertEqual(result['candidate_reason'],'RobotFaultTelemetryStale')

    def test_notready_low_missing_invalid_battery_or_connection_block(self):
        for value in (None,True,float('nan'),float('inf'),24,101):
            with self.subTest(battery=value):
                answer=copy.deepcopy(self.answer);answer['shadows']['ro-currentstate']['battery']=value
                self.assertFalse(d.assessment(answer,now=1000)['standard_inspected_resume_candidate'])
        for key,value in (('notReady',1),('condNotReady',[1]),('mssnStrtTm',0),('nMssn',None)):
            with self.subTest(key=key):
                answer=copy.deepcopy(self.answer);answer['shadows']['ro-currentstate']['mission'][key]=value
                self.assertFalse(d.assessment(answer,now=1000)['standard_inspected_resume_candidate'])
        self.answer['shadows']['rw-constatus']['connected']=False
        self.assertFalse(d.assessment(self.answer,now=1000)['standard_inspected_resume_candidate'])

    def test_running_new_manual_mission_needs_no_recovery_command(self):
        self.answer['shadows']['ro-currentstate']['mission'].update(phase='run',error=0,initiator='manual')
        result=d.assessment(self.answer,now=1000)
        self.assertTrue(result['running_fault_free'])
        self.assertFalse(result['standard_inspected_resume_candidate'])
        self.assertIn('no recovery command needed',result['next_step'])

    def test_missing_stats_or_stale_robot_state_cannot_claim_fault_free_running(self):
        self.answer['shadows']['ro-currentstate']['mission'].update(phase='run',error=0)
        for stats in ({},{'error_type':'TimeoutError'},{'unmapped_fault_present':False}):
            answer=copy.deepcopy(self.answer);answer['shadows']['ro-stats']=stats
            result=d.assessment(answer,now=1000)
            self.assertTrue(result['running_without_standard_error'])
            self.assertFalse(result['running_fault_free'])
            self.assertFalse(result['native_report']['cleared_or_active_state_verified'])
        self.answer['shadows']['ro-currentstate']['phase_reported_at']=800
        self.assertFalse(d.assessment(self.answer,now=1000)['running_fault_free'])

    def test_stats_export_numeric_allowlist_and_no_raw_unknown_strings(self):
        payload={'state':{'reported':{'unprocessedError':'picea unknown fault code:2105',
            'bbrun':{'nMBStll':3,'nLBStll':True,'nOvertemps':-1,'private':'secret'},'password':'secret'}},
            'metadata':{'reported':{'unprocessedError':{'timestamp':900}}}}
        filtered=d.filtered('ro-stats',payload)
        self.assertEqual(filtered['unmapped_fault_code'],2105)
        self.assertEqual(filtered['declared_fault_counters'],{'nMBStll':3})
        self.assertFalse(filtered['counter_semantics_verified'])
        self.assertNotIn('secret',json.dumps(filtered))
        payload['state']['reported']['unprocessedError']='secret token details'
        filtered=d.filtered('ro-stats',payload)
        self.assertTrue(filtered['unmapped_fault_present'])
        self.assertIsNone(filtered['unmapped_fault_code'])
        self.assertNotIn('secret',json.dumps(filtered))

    def test_missing_known_brush_subcode_remains_unknown(self):
        stats=d.filtered('ro-stats',{'state':{'reported':{'unprocessedError':None}}})
        self.assertFalse(stats['unmapped_fault_present'])
        self.assertIsNone(stats['unmapped_fault_code'])
        self.assertEqual(stats['declared_fault_counters'],{})
        result=d.assessment(self.answer,now=1000)
        self.assertFalse(result['recognized_native_brush_fault_distinguishable'])
        self.assertFalse(result['brush_current_available'])
        self.assertFalse(result['brush_speed_available'])


    def test_native2102_is_retained_as_state_without_relabeling_brush_error(self):
        self.answer['shadows']['rw-software']={'software_version':'p25-105+8.4.0+I3.7.118'}
        self.answer['shadows']['ro-stats']={'unmapped_fault_present':True,'unmapped_fault_code':2102,'unmapped_fault_reported_at':995}
        result=d.assessment(self.answer,now=1000)
        self.assertEqual(result['native_report']['classification'],'pause_or_broken_clean_state')
        self.assertEqual(result['native_report']['type'],3)
        self.assertFalse(result['native_report']['code2102_is_main_brush_error2'])
        self.assertFalse(result['standard_inspected_resume_candidate'])
        self.assertFalse(result['native_report']['cleared_or_active_state_verified'])

    def test_standard_error_zero_does_not_hide_unclassified_native_report(self):
        self.answer['shadows']['ro-currentstate']['mission'].update(phase='run',error=0)
        self.answer['shadows']['ro-stats']={'unmapped_fault_present':True,'unmapped_fault_code':2104,'unmapped_fault_reported_at':995}
        result=d.assessment(self.answer,now=1000)
        self.assertTrue(result['running_without_standard_error'])
        self.assertFalse(result['running_fault_free'])
        self.assertIsNone(result['native_report']['type'])
        self.assertIn('unclassified native report',result['next_step'])
        self.assertNotIn('native 2102',result['next_step'])
        self.answer['shadows']['rw-software']={'software_version':'p25-105+8.4.0+I3.7.118'}
        self.assertEqual(d.assessment(self.answer,now=1000)['native_report']['classification'],'unclassified_type3_state_report')


class FixedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_reads_failure_never_returns_secret_or_writes(self):
        robot=SimpleNamespace(get_named_shadow=AsyncMock(side_effect=RuntimeError('secret credentials')))
        answer=await d.read(robot)
        self.assertEqual([c.args[0] for c in robot.get_named_shadow.await_args_list],list(d.SHADOWS))
        self.assertNotIn('secret',json.dumps(answer))
        self.assertTrue(answer['read_only'])
        self.assertGreaterEqual(answer['read_completed_at'],answer['read_started_at'])


if __name__=='__main__':unittest.main()
