"""Exact-device observer with same-origin owner-granted dashboard controls."""
import asyncio
import contextlib
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from observer_store import Store, sanitize
from room_labels import names_by_map
import control_layer
import mission_monitor
import stationary_review
import command_actor
import local_schedules
import dashboard_commands

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "source-review/prime"))
DATA = ROOT / "observer-data"
from roo_config import PORT
from roo_config import TARGET
from roo_config import FINGERPRINT
logging.disable(logging.CRITICAL)
os.umask(0o077)

from roo_credentials import credentials

def status():
    receipt = ROOT / 'activation-result.json'
    setup = json.loads(receipt.read_text()) if receipt.exists() else {}
    writes_enabled = bool(setup.get('writes_enabled'))
    legacy_maps = {}
    prior_result = ROOT / 'connection-result.json'
    if prior_result.exists():
        prior = json.loads(prior_result.read_text())
        if prior.get('identity',{}).get('device_fingerprint') == FINGERPRINT:
            old_state = prior.get('current_state',{})
            old_mission = old_state.get('state.reported.cleanMissionStatus.missionId')
            old_map = old_state.get('state.reported.p2maps[0].p2map_id')
            if old_mission and old_map:legacy_maps[old_mission] = old_map
    db = sqlite3.connect(f"file:{DATA / 'observer.sqlite3'}?mode=ro", uri=True)
    try:
        def latest(kind):
            row = db.execute("SELECT at,mission,segment,data FROM events WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
            return None if not row else {"at": row[0], "age_seconds": round(time.time()-row[0],1), "mission": row[1], "segment": row[2], "data": json.loads(row[3])}
        live = latest("position")
        state = latest("state")
        return {"read_only": not writes_enabled, "observation_http_read_only": False, "robot_control_http_available": True, "area_draft_http_writes": True, "spot_cleaning_http_available": bool(control_layer.policy().get("experimental_geometry_commands")), "spot_boundary_hardware_verified": False,
                "write_actions": setup.get('write_actions',[]) if writes_enabled else [],
                "legacy_frame_map_ids": legacy_maps,
                "room_names_by_map": names_by_map(),
                "robot_name": __import__("roo_config").ROBOT_NAME, "control_policy":control_layer.policy(),
                "target_sku": TARGET, "connection": latest("connection"),
                "identity":latest('identity'),
                "state": state, "pose": live, "pose_stale": not live or live["age_seconds"] > 15,
                "mission_history": latest('mission_history'), "map_error": latest('map_error'),
                "map_retention_gap":latest('map_retention_gap'),
                "state_stale": not state or state["age_seconds"] > 90,
                "retention": {"position_messages": db.execute("SELECT COUNT(*) FROM events WHERE kind='position'").fetchone()[0],
                              "state_samples":db.execute("SELECT COUNT(*) FROM events WHERE kind='state'").fetchone()[0],
                              "state_deltas":db.execute("SELECT COUNT(*) FROM events WHERE kind='state_delta'").fetchone()[0],
                              "maps": db.execute("SELECT COUNT(*) FROM maps").fetchone()[0]},
                "missions": [{"mission": r[0], "segments": r[1], "first_seen": r[2], "last_seen": r[3]}
                    for r in db.execute("SELECT mission,COUNT(DISTINCT segment),MIN(at),MAX(at) FROM events WHERE mission IS NOT NULL GROUP BY mission ORDER BY MAX(at) DESC LIMIT 50")],
                "maps": [{"id": r[0], "at": r[1], "mission": r[2], "source": r[3], "layers": json.loads(r[4])}
                         for r in db.execute("SELECT id,at,mission,source,layers FROM maps ORDER BY id DESC LIMIT 30")],
                "capabilities": {"observe_state": True, "observe_pose": True, "retain_maps": True,
                    "pause_resume": "owner-granted chat controls; bounded same-mission recovery",
                    "coverage": "Only vendor layers count as coverage; trajectory is never treated as cleaned coverage",
                    "restore_onboard_planner": False}}
    finally:
        db.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self.send_error(403)
            return
        try:
            if self.path == "/":
                data = (ROOT / "observer.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            elif self.path in ('/ui/roo.css','/ui/roo.js','/ui/map.js'):
                data=(ROOT/'frontend'/self.path.rsplit('/',1)[-1]).read_bytes()
                content_type='text/css; charset=utf-8' if self.path.endswith('.css') else 'text/javascript; charset=utf-8'
            elif self.path in ('/favicon.svg','/favicon.ico'):
                data=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#202327"/><text x="32" y="45" text-anchor="middle" font-family="Arial,sans-serif" font-size="44" font-weight="700" fill="#eef1f5">V</text></svg>'
                content_type='image/svg+xml'
            elif self.path == "/api/status":
                data = json.dumps(status()).encode()
                content_type = "application/json"
            elif self.path == '/api/schedules':
                data=json.dumps({'schedules':local_schedules.list_schedules()}).encode()
                content_type='application/json'
            elif self.path.startswith('/api/request/'):
                try:
                    answer=dashboard_commands.result(self.path.rsplit('/',1)[-1])
                except control_layer.ControlError:
                    self.send_error(404);return
                data=json.dumps(answer).encode()
                content_type='application/json'
            elif self.path == '/api/stuck-reviews':
                data=json.dumps({'reviews':stationary_review.reviews()}).encode()
                content_type='application/json'
            elif self.path == '/api/locations/garbage-can':
                import named_locations
                data=json.dumps(named_locations.get()).encode()
                content_type='application/json'
            elif self.path == '/api/areas':
                import area_selection
                data=json.dumps({'areas':area_selection.list_areas()}).encode()
                content_type='application/json'
            elif self.path == "/api/trajectory":
                with sqlite3.connect(f"file:{DATA / 'observer.sqlite3'}?mode=ro", uri=True) as db:
                    rows = db.execute("SELECT kind,mission,segment,data FROM events WHERE kind IN ('position','coordinate_gap','segment_start') ORDER BY id DESC LIMIT 2000").fetchall()
                data = json.dumps([{"kind": r[0], "mission": r[1], "segment": r[2], "data": json.loads(r[3])} for r in reversed(rows)]).encode()
                content_type = "application/json"
            elif self.path == '/api/notifications/recent':
                with contextlib.closing(control_layer.database()) as db:
                    rows=db.execute('SELECT id,at,data FROM notices ORDER BY id DESC LIMIT 50').fetchall()
                data=json.dumps({'events':[{'id':r[0],'at':r[1],**json.loads(r[2])} for r in reversed(rows)]}).encode()
                content_type='application/json'
            elif self.path == '/api/notifications':
                data=json.dumps(control_layer.notices()).encode()
                content_type='application/json'
            elif self.path.startswith("/api/map/") and self.path.rsplit("/", 1)[-1].isdigit():
                with sqlite3.connect(f"file:{DATA / 'observer.sqlite3'}?mode=ro", uri=True) as db:
                    row = db.execute("SELECT filename FROM maps WHERE id=?", (int(self.path.rsplit("/",1)[-1]),)).fetchone()
                if not row:
                    self.send_error(404)
                    return
                document = json.loads((DATA / row[0]).read_bytes())
                metadata = next((v for k,v in document.get('files',{}).items() if k.split('/')[-1]=='metadata.json'),{})
                document['companion_room_names'] = names_by_map().get(metadata.get('id'),{})
                data = json.dumps(document).encode()
                content_type = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
        except (OSError, sqlite3.Error):
            self.send_error(503, "Observer starting or data unavailable")

    def do_POST(self):
        if self.path not in ('/api/locations/garbage-can','/api/areas','/api/requests','/api/schedules/enabled','/api/schedules/delete','/api/stuck-reviews/classify'):
            self.send_error(405);return
        if self.headers.get('Host') not in (f'127.0.0.1:{PORT}',f'localhost:{PORT}') or self.headers.get('Origin') not in (f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'):
            self.send_error(403);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            limit=262144 if self.path=='/api/requests' else 32768
            if not 0<length<=limit or self.headers.get('Content-Type')!='application/json':raise ValueError('InvalidDraftRequest')
            item=json.loads(self.rfile.read(length))
            if not isinstance(item,dict):raise ValueError('ObjectRequired')
            if self.path == '/api/locations/garbage-can':
                if set(item)!={'map_id','map_version','snapshot_id','point'}:raise ValueError('OnlyLocationFieldsAccepted')
                import named_locations
                result=named_locations.save(**item);code=200
            elif self.path == '/api/areas':
                if set(item)!={'name','map_id','map_version','points'}:raise ValueError('OnlyAreaDraftFieldsAccepted')
                import area_selection
                result=area_selection.save(**item);code=200
            elif self.path == '/api/stuck-reviews/classify':
                if set(item)!={'incident_id','classification'}:raise ValueError('OnlyReviewDecisionFieldsAccepted')
                result=stationary_review.classify(**item);code=200
            elif self.path == '/api/requests':
                result=dashboard_commands.submit(item);code=202 if result['state']!='finished' else 200
            else:
                if not control_layer.policy().get('direct_chat_commands'):raise control_layer.ControlError('PersistentRoomControlGrantRequired')
                if self.path.endswith('/enabled'):
                    if set(item)!={'schedule_id','enabled'}:raise ValueError('InvalidScheduleFields')
                    result=local_schedules.set_enabled(**item)
                else:
                    if set(item)!={'schedule_id'}:raise ValueError('InvalidScheduleFields')
                    result=local_schedules.delete(**item)
                code=200
        except Exception as exc:
            result={'error_code':str(exc) if isinstance(exc,control_layer.ControlError) else type(exc).__name__};code=400
        data=json.dumps(result).encode();self.send_response(code)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)

async def download_map(session, url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not (parsed.hostname.endswith(".amazonaws.com") or parsed.hostname.endswith(".irobotapi.com")):
        raise ValueError("UnexpectedMapOrigin")
    # Credentials embedded in presigned URL stay in memory only; redirects disallowed.
    async with session.get(url, allow_redirects=False) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("MapTooLarge")
        return bytes(data)

async def connected_session(store, stop):
    import aiohttp
    from roombapy_prime.auth import login
    from roombapy_prime.prime_factory import PrimeFactory
    from roombapy_prime.mqtt_client import _shadow_base
    from roombapy_prime.models import PositionUpdateMessage, MapUpdateMessage
    email, password, country = credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        auth = await login(session, email, password, country)
        matches = [(blid, entry) for blid, entry in auth.robots.items()
                   if entry.sku == TARGET and hashlib.sha256(blid.encode()).hexdigest()[:16] == FINGERPRINT]
        if len(matches) != 1:
            raise RuntimeError("ExactDeviceNotFound")
        blid, entry = matches[0]
        robot = await PrimeFactory.create_prime_robot(session, email, password, country,
                            blid=blid, login_result=auth, auto_refresh=True)
        mqtt_publish = robot._mqtt._publish
        allowed = {_shadow_base(blid, name)+"/get" for name in (None, "ro-currentstate", *command_actor.VOICE_SHADOW_FIELDS)}
        async def read_publish(topic, payload, timeout=5.0):
            if topic not in allowed or payload not in (b"", ""):
                raise RuntimeError("WriteBlocked")
            return await mqtt_publish(topic, payload, timeout)
        robot._mqtt._publish = read_publish
        rest_request = robot._rest._request
        async def read_request(method, url, *args, **kwargs):
            if method != "GET":
                raise RuntimeError("WriteBlocked")
            return await rest_request(method, url, *args, **kwargs)
        robot._rest._request = read_request
        actor=None
        try:
            await robot.connect()
            reported = (await robot.get_state()).payload.get("state", {}).get("reported", {})
            if reported.get("sku") != TARGET:
                raise RuntimeError("ExactStateMismatch")
            store.event("identity", {"sku": entry.sku, "software_version": entry.software_version, "device_fingerprint": FINGERPRINT,
                                     "mqtt_client_id_fingerprint":hashlib.sha256(robot._mqtt._token.client_id.encode()).hexdigest()[:16],
                                     "capabilities": reported.get("cap"), "digital_capabilities": reported.get("digiCap")})
            store.event("connection", {"status": "connected", "verified_exact_device": True})
            actor=await command_actor.start(robot,blid,mqtt_publish)
            store.event('command_transport',{'owner':'observer_mqtt_session','private_unix_socket':True,'second_mqtt_session':False})
            pending_maps = asyncio.Queue(maxsize=32)
            recoveries=asyncio.Queue(maxsize=1)
            async def states():
                failures = 0
                last_history_mission=None
                last_history_cycle=None
                while not stop.is_set():
                    try:
                        payload = (await robot.get_named_shadow("ro-currentstate")).payload
                        current = payload.get("state", {}).get("reported", {})
                        p2maps = current.get('p2maps') or []
                        store.map_id = p2maps[0].get('p2map_id') if p2maps else None
                        store.map_version = p2maps[0].get('p2mapv_id') if p2maps else None
                        mission = current.get("cleanMissionStatus", {}).get("missionId")
                        store.set_mission(mission)
                        store.event("state", current)
                        recovery=await asyncio.to_thread(mission_monitor.evaluate,current)
                        if recovery and not recoveries.full():recoveries.put_nowait(recovery)
                        if current.get('cleanMissionStatus',{}).get('phase')=='run' and store.last and time.time()-store.last[1]>120:
                            control_layer.notify('trajectory-stale:'+str(mission),{'kind':'trajectory_stale','mission_id':mission,
                                'message':'State says running but live trajectory is stale; inspect Roomba. No automatic restart.'})
                        cycle=current.get('cleanMissionStatus',{}).get('cycle')
                        if mission!=last_history_mission or cycle=='none' and last_history_cycle not in (None,'none'):
                            try:store.event('mission_history',await robot.get_mission_history(blid,max_reports=10))
                            except Exception as exc:store.event('history_error',{'type':type(exc).__name__})
                        last_history_mission=mission;last_history_cycle=cycle
                        failures = 0
                    except Exception as exc:
                        store.event("state_error", {"type": type(exc).__name__})
                        failures += 1
                        if failures >= 3:
                            raise RuntimeError("RepeatedStateFailure")
                    await asyncio.sleep(10)
            async def recovery_worker():
                while not stop.is_set():
                    request=await recoveries.get()
                    try:
                        result=await asyncio.to_thread(mission_monitor.recover,request)
                        store.event('recovery_result',result)
                    except Exception as exc:
                        store.event('recovery_refused',{'type':type(exc).__name__,'mission':request['mission_id']})
                        control_layer.notify('recovery-refused:'+request['mission_id']+':'+request['incident'],
                            {'kind':'recovery_refused','mission_id':request['mission_id'],'message':'Automatic resume refused; no retry. Inspect Roomba status.'})
            async def stationary_worker():
                while not stop.is_set():
                    try:await asyncio.to_thread(stationary_review.observe,status())
                    except Exception as exc:store.event("stuck_review_error",{"type":type(exc).__name__})
                    await asyncio.sleep(2)
            async def state_deltas():
                async for response in robot.watch_state(named='ro-currentstate',queue_maxsize=1000):
                    store.event('state_delta',response.payload)
            async def schedule_worker():
                while not stop.is_set():
                    try:
                        results=await asyncio.to_thread(local_schedules.tick)
                        for result in results:store.event('local_schedule_result',result)
                    except Exception as exc:store.event('schedule_error',{'type':type(exc).__name__})
                    await asyncio.sleep(15)
            async def stream():
                async for message in robot.watch_live_map():
                    if isinstance(message, PositionUpdateMessage):
                        store.position({"sequence": message.sequence_number,
                            "timestamp": message.last_update_timestamp.timestamp(),
                            "points": [{"x": p.point[0], "y": p.point[1], "heading_raw": p.orientation,
                                        "mode": p.operating_modes} for p in message.updates]})
                    elif isinstance(message, MapUpdateMessage):
                        if pending_maps.full():
                            pending_maps.get_nowait()
                            store.event("map_retention_gap", {"reason": "download_queue_full_oldest_reference_dropped"})
                        pending_maps.put_nowait(message.livemap_url)
            async def maps():
                try:
                    active = await robot.get_active_map_versions()
                    store.event("active_maps", active)
                    for item in active[:10]:
                        link = await robot.get_map_geojson_link(item["p2map_id"], item["active_p2mapv_id"])
                        store.map_bundle(await download_map(session, link["map_url"]), "saved_map")
                except Exception as exc:
                    store.event("map_error", {"type": type(exc).__name__, "source": "saved_map"})
                # Bounded exact-device history read. Preserve vendor area/timeline information;
                # look only for explicitly named map/version pairs, without guessing IDs.
                try:
                    history = await robot.get_mission_history(blid, max_reports=10)
                    store.event('mission_history', history)
                    references = []
                    def find_refs(value, mission_id=None):
                        if isinstance(value, dict):
                            mission_id = value.get('missionId') or value.get('mission_id') or mission_id
                            map_id = value.get('p2mapId') or value.get('p2map_id')
                            version = value.get('p2mapvId') or value.get('p2mapv_id') or value.get('user_p2mapv_id')
                            if map_id and version:
                                references.append((map_id,version,mission_id))
                            for child in value.values():find_refs(child,mission_id)
                        elif isinstance(value,list):
                            for child in value:find_refs(child,mission_id)
                    find_refs(history)
                    for map_id,version,mission_id in list(dict.fromkeys(references))[:3]:
                        try:
                            link = await robot.get_map_geojson_link(map_id,version)
                            store.map_bundle(await download_map(session,link['map_url']), 'mission_history', mission_id)
                        except Exception as exc:
                            store.event('map_error',{'type':type(exc).__name__,'source':'mission_history'})
                except Exception as exc:
                    store.event('history_error',{'type':type(exc).__name__})
                while not stop.is_set():
                    url = await pending_maps.get()
                    try:
                        store.map_bundle(await download_map(session, url), "live_map")
                    except Exception as exc:
                        store.event("map_error", {"type": type(exc).__name__, "source": "live_map"})
            tasks = [asyncio.create_task(function()) for function in (states, stream, maps,state_deltas,recovery_worker,schedule_worker,stationary_worker)]
            tasks.append(asyncio.create_task(stop.wait()))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if actor:await command_actor.close(*actor)
            await robot.disconnect()

async def run():
    store = Store(DATA)
    # Preserve the successful temporary session as an older, separate segment.
    if not store.db.execute("SELECT 1 FROM events WHERE kind='temporary_session_import' LIMIT 1").fetchone():
        previous = ROOT / "trajectory-samples.json"
        result = ROOT / "connection-result.json"
        if previous.exists() and result.exists():
            identity = json.loads(result.read_text())
            if identity.get("identity", {}).get("device_fingerprint") == FINGERPRINT:
                store.set_mission(identity.get("current_state", {}).get("state.reported.cleanMissionStatus.missionId"))
                store.map_id = identity.get('current_state',{}).get('state.reported.p2maps[0].p2map_id')
                store.map_version = identity.get('current_state',{}).get('state.reported.p2maps[0].p2mapv_id')
                for frame in json.loads(previous.read_text()):
                    frame["timestamp"] = datetime.fromisoformat(frame["timestamp"]).timestamp()
                    store.position(frame)
                store.event("temporary_session_import", {"source": "original_read_only_check", "points_are_travel_not_coverage": True})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    delay = 5
    try:
        while not stop.is_set():
            store.segment += 1
            store.last = None
            store.event("segment_start", {"reason": "cloud_connection_attempt", "coordinate_continuity": "unknown"})
            attempted_at = time.monotonic()
            try:
                await connected_session(store, stop)
                delay = 5
            except Exception as exc:
                if time.monotonic() - attempted_at > 60:
                    delay = 5
                store.event("connection", {"status": "disconnected", "error_type": type(exc).__name__, "retry_seconds": delay})
            if not stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), delay)
                delay = min(delay*2, 300)
    finally:
        store.event("connection", {"status": "stopped"})
        server.shutdown()
        server.server_close()
        store.close()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as exc:
        # Never print raw exceptions, tokens, URLs or traceback locals.
        print(json.dumps({"fatal_type": type(exc).__name__}), flush=True)
        sys.exit(1)
