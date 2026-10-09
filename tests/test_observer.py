"""Offline tests: retention after abrupt exit, gaps, vendor layer mapping, interface restrictions."""
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from observer_store import Store, sanitize
import roomba_observer as observer

class ObserverTests(unittest.TestCase):
    def test_restart_preserves_committed_segments(self):
        with tempfile.TemporaryDirectory() as folder:
            code = "from observer_store import Store; import os,sys; s=Store(sys.argv[1]); s.set_mission('old'); s.position({'sequence':1,'timestamp':100,'points':[{'x':1,'y':2,'heading_raw':0,'mode':1}]}); os._exit(9)"
            result = subprocess.run([sys.executable,"-c",code,folder],capture_output=True)
            self.assertEqual(result.returncode,9)
            store=Store(folder)
            store.set_mission('new')
            store.position({'sequence':2,'timestamp':200,'points':[{'x':3,'y':4}]})
            rows=store.db.execute("SELECT mission,segment FROM events WHERE kind='position' ORDER BY id").fetchall()
            self.assertEqual([r[0] for r in rows],['old','new'])
            self.assertNotEqual(rows[0][1],rows[1][1])
            store.close()

    def test_gap_and_reset_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);store.set_mission('m')
            for seq,stamp in [(10,100),(15,130),(1,131)]:
                store.position({'sequence':seq,'timestamp':stamp,'points':[{'x':1,'y':2}]})
            reasons=[json.loads(r[0])['reason'] for r in store.db.execute("SELECT data FROM events WHERE kind='coordinate_gap'")]
            self.assertEqual(set(reasons),{'sequence_gap','timestamp_gap','sequence_reset'})
            store.close()

    def test_manifest_maps_noncanonical_filenames_without_inventing_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            blob=io.BytesIO()
            with tarfile.open(fileobj=blob,mode='w:gz') as archive:
                docs={'manifest.json':{'features':[{'type':'coverage','filepath':'layers/007.json'}]},
                      'layers/007.json':{'type':'FeatureCollection','features':[]},
                      '../outside.json':{'password':'secret','map_url':'https://example.com/?token=x','geometry':[]}}
                for name,document in docs.items():
                    data=json.dumps(document).encode();info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
            store=Store(folder)
            layers=store.map_bundle(blob.getvalue(),'fixture')
            self.assertEqual(layers,[{'type':'coverage','filepath':'layers/007.json','retained':True}])
            retained=json.loads(next((Path(folder)/'maps').glob('*.json')).read_text())
            self.assertNotIn('password',retained['files']['../outside.json'])
            self.assertNotIn('map_url',retained['files']['../outside.json'])
            self.assertFalse((Path(folder).parent/'outside.json').exists())
            store.close()

    def test_loopback_status_and_no_legacy_command_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            old_data,old_port=observer.DATA,observer.PORT
            observer.DATA=Path(folder)
            store=Store(folder);store.event('connection',{'status':'connected'});store.close()
            server=observer.ThreadingHTTPServer(('127.0.0.1',0),observer.Handler)
            observer.PORT=server.server_port
            thread=threading.Thread(target=server.serve_forever);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                result=json.load(urllib.request.urlopen(base+'/api/status'))
                self.assertFalse(result['observation_http_read_only']);self.assertTrue(result['robot_control_http_available']);self.assertTrue(result['pose_stale'])
                for request in [urllib.request.Request(base+'/api/status',headers={'Host':'evil.test'}),
                                urllib.request.Request(base+'/api/status',headers={'Origin':'https://evil.test'}),
                                urllib.request.Request(base+'/api/command',method='POST')]:
                    with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(request)
            finally:
                server.shutdown();thread.join();server.server_close();observer.DATA,observer.PORT=old_data,old_port

if __name__=='__main__':
    unittest.main()
