"""Offline HTTP boundary and durable dispatch tests; never moves VacuumGPT."""
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
import control_layer as c
import dashboard_commands as d
import roomba_observer as o

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(c,'ROOT',Path(self.temp.name)))
        self.stack.enter_context(patch.object(c,'policy',return_value={'direct_chat_commands':True}))
        c.database().close()
    def tearDown(self):
        for _ in range(100):
            if not d._active:break
            time.sleep(.01)
        self.stack.close();self.temp.cleanup()
    def wait(self,rid):
        for _ in range(200):
            r=d.result(rid)
            if r['state']=='finished':return r
            time.sleep(.005)
        self.fail('request did not finish')
    def test_double_click_and_reload_dispatch_once(self):
        item={'request_id':'dashboard-duplicate','operation':'command','action':'pause'}
        started=threading.Event();release=threading.Event()
        def run(**kw):started.set();release.wait(1);return {'outcome':'state_transition_observed','robot_action_confirmed':True}
        with patch('direct_control.run',side_effect=run) as run:
            d.submit(item);self.assertTrue(started.wait(1));d.submit(item)
            self.assertEqual(run.call_count,1)
            with self.assertRaisesRegex(c.ControlError,'RequestIdIntentMismatch'):d.submit({**item,'action':'stop'})
            release.set();self.assertTrue(self.wait(item['request_id'])['result']['robot_action_confirmed'])
            d.submit(item);self.assertEqual(run.call_count,1)
    def test_interrupted_claim_is_never_replayed(self):
        item={'request_id':'interrupted-request','operation':'command','action':'start'}
        with contextlib.closing(d.database()) as db,db:db.execute('INSERT INTO dashboard_requests VALUES(?,?,?,NULL,?)',(item['request_id'],json.dumps({k:v for k,v in item.items() if k!='request_id'},sort_keys=True),'running',time.time()))
        with patch('direct_control.run') as run:
            self.assertEqual(d.submit(item)['result']['outcome'],'interrupted');run.assert_not_called()
    def test_schedule_create_is_idempotent(self):
        item={'request_id':'schedule-duplicate','operation':'schedule','rooms':['Kitchen'],'local_time':'08:45','weekdays':[0,2]}
        with patch('local_schedules.create',return_value={'schedule_id':'new','robot_commands_sent':0}) as create:
            d.submit(item);self.wait(item['request_id']);d.submit(item);self.assertEqual(create.call_count,1)
    def test_fault_and_grant_refusals_do_not_dispatch(self):
        with patch('direct_control.run',side_effect=c.ControlError('RobotFaultRequiresOwner')):
            d.submit({'request_id':'faulted-request','operation':'command','action':'start'})
            self.assertEqual(self.wait('faulted-request')['result']['error_code'],'RobotFaultRequiresOwner')
        with patch.object(c,'policy',return_value={'direct_chat_commands':False}),patch('direct_control.run') as run:
            with self.assertRaises(c.ControlError):d.submit({'request_id':'grant-refused','operation':'command','action':'find'})
            run.assert_not_called()
    def test_fields_are_allowlisted(self):
        for item in ({'operation':'command','request_id':'valid-request','action':'start','rooms':['Kitchen']},{'operation':'command','request_id':'valid-request','action':'find','secret':'x'},{'operation':'command','request_id':'bad','action':'find'}):
            with self.assertRaises(c.ControlError):d.submit(item)
    def test_http_origin_host_and_content_type_prevent_dispatch(self):
        server=o.ThreadingHTTPServer(('127.0.0.1',0),o.Handler)
        self.stack.enter_context(patch.object(o,'PORT',server.server_port))
        t=threading.Thread(target=server.serve_forever);t.start()
        base='http://127.0.0.1:'+str(server.server_port)
        body=json.dumps({'operation':'command','request_id':'http-request-valid','action':'find'}).encode()
        try:
            with patch.object(d,'submit',return_value={'state':'queued'}) as submit:
                for headers in ({'Content-Type':'application/json'}, {'Content-Type':'application/json','Origin':'https://evil.example'}, {'Content-Type':'text/plain','Origin':base}, {'Content-Type':'application/json','Origin':base,'Host':'evil.example'}):
                    with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(urllib.request.Request(base+'/api/requests',body,headers))
                submit.assert_not_called()
                result=json.load(urllib.request.urlopen(urllib.request.Request(base+'/api/requests',body,{'Content-Type':'application/json','Origin':base})))
                self.assertEqual(result['state'],'queued');self.assertEqual(submit.call_count,1)
        finally:server.shutdown();t.join();server.server_close()

if __name__=='__main__':unittest.main()
