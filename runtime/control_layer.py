"""Durable exact-device plans and at-most-once dispatch journal. No network writes."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
from roo_config import TARGET
from roo_config import FINGERPRINT
ACTIONS = ('start', 'stop', 'pause', 'resume', 'dock', 'find', 'clean_room', 'clean_area', 'drive_garbage')

class ControlError(Exception):
    pass

def read_local(route):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(__import__('roo_config').BASE_URL+route, timeout=5) as response:
        data = response.read(70*1024*1024+1)
    if len(data)>70*1024*1024:
        raise ControlError('ResponseTooLarge')
    return json.loads(data)

def database():
    folder=ROOT/'observer-data'
    folder.mkdir(mode=0o700, exist_ok=True)
    path=folder/'control.sqlite3'
    db=sqlite3.connect(path, timeout=5)
    os.chmod(path,0o600)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, created REAL, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(plan_id TEXT PRIMARY KEY, updated REAL, stage TEXT, result TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS command_audit(id INTEGER PRIMARY KEY, plan_id TEXT, at REAL, stage TEXT, data TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS requests(request_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, intent TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS jobs(mission_id TEXT PRIMARY KEY, updated REAL, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY, at REAL, dedup TEXT UNIQUE, data TEXT NOT NULL)')
    db.commit()
    return db

def preconditions(action, mission, owner_navigation_retry=False):
    phase=mission.get('phase')
    if phase is None or 'error' not in mission:
        raise ControlError('IncompleteRobotState')
    if owner_navigation_retry and (action!='dock' or mission.get('error')!=1010 or phase!='stuck' or not mission.get('missionId') or 'notReady' not in mission or 'condNotReady' not in mission):
        raise ControlError('NavigationRetryRequiresConfirmed1010StuckState')
    navigation_exception=action=='dock' and owner_navigation_retry is True and mission.get('error')==1010
    if ((mission.get('error') and not navigation_exception) or mission.get('notReady') or mission.get('condNotReady')) and action in ('start','clean_room','clean_area','resume','dock'):
        raise ControlError('RobotFaultRequiresOwner')
    if action=='pause' and phase!='run':
        raise ControlError('PauseRequiresRunningMission')
    if action=='resume' and phase not in ('pause','paused'):
        raise ControlError('ResumeRequiresPausedMission')
    if action=='resume' and not mission.get('missionId'):
        raise ControlError('ResumeRequiresExistingMissionIdentity')
    if action in ('start','clean_room','clean_area') and phase not in ('charge','idle','stop','stopped'):
        raise ControlError('StartRequiresIdleRobot')

def prepare(action, snapshot=None, room=None, mode='vacuum', rooms=None, ordered=False, suction=None, passes=None, owner_navigation_retry=False, area_points=None, map_id=None, map_version=None, snapshot_id=None):
    if action in ('drive_garbage','clean_area') and not policy().get('experimental_geometry_commands'):
        raise ControlError('ExperimentalGeometryCommandsDisabled')
    if action not in ACTIONS:
        raise ControlError('UnsupportedAction')
    snapshot=snapshot if snapshot is not None else read_local('/api/status')
    if snapshot.get('target_sku')!=TARGET or snapshot.get('state_stale') or snapshot.get('connection',{}).get('data',{}).get('status')!='connected':
        raise ControlError('FreshExactDeviceStateRequired')
    mission=snapshot.get('state',{}).get('data',{}).get('cleanMissionStatus',{})
    if type(owner_navigation_retry) is not bool or owner_navigation_retry and action!='dock':raise ControlError('NavigationRetryOnlyForExplicitDock')
    preconditions('clean_area' if action=='drive_garbage' else action,mission,owner_navigation_retry)
    now=time.time()
    plan={'plan_id':str(uuid.uuid4()), 'action':action,'target_sku':TARGET,
          'fingerprint':FINGERPRINT,'mission_id':mission.get('missionId'),
          'phase':mission.get('phase'),'created_at':now,'expires_at':now+600,
          'effect':{'start':'Start whole-house cleaning','stop':'Stop current mission','pause':'Pause current mission','resume':'Resume current mission','dock':'Return to dock','find':'Sound locator chime','clean_room':'Clean exactly one named room','clean_area':'Clean the drawn spot using a temporary region','drive_garbage':'Travel to the garbage can with brief vacuuming, then pause'}[action]}
    if owner_navigation_retry:
        plan['owner_navigation_retry']=True
        plan['expected_error']=1010
    if action=='clean_room':
        selected=rooms if rooms is not None else [room] if room else []
        if not isinstance(selected,list) or not 1<=len(selected)<=20 or any(not isinstance(x,str) or not x.strip() for x in selected):
            raise ControlError('RoomRequiredNoWholeHouseFallback')
        if len({x.strip().casefold() for x in selected})!=len(selected):raise ControlError('DuplicateRoomsRefused')
        if type(ordered) is not bool:raise ControlError('OrderedMustBeBoolean')
        if suction is not None:
            if type(suction) is not int or suction not in (1,2,3,4):raise ControlError('SuctionMustBe1To4')
            cap=snapshot.get('identity',{}).get('data',{}).get('capabilities',{})
            if cap.get('suctionLvl',0)<suction:raise ControlError('SuctionNotAdvertisedByDevice')
        if passes is not None and (type(passes) is not int or passes not in (1,2)):raise ControlError('OnlyOneOrTwoPassesSupported')
        maps=snapshot.get('state',{}).get('data',{}).get('p2maps') or []
        if len(maps)!=1 or not maps[0].get('p2map_id') or not maps[0].get('p2mapv_id'):
            raise ControlError('UnambiguousActiveMapRequired')
        active=maps[0];targets=[resolve_room(name,active['p2map_id']) for name in selected]
        if mode not in ('vacuum','vacuum_and_mop'):raise ControlError('UnsupportedCleaningMode')
        evidence=[validate_retained_region(snapshot,active['p2map_id'],active['p2mapv_id'],target['vendor_region_id']) for target in targets]
        plan.update(room=', '.join(t['name'] for t in targets),region_id=targets[0]['vendor_region_id'],map_id=active['p2map_id'],map_version=active['p2mapv_id'],
                    mode=mode,operating_mode=2 if mode=='vacuum' else 32,map_snapshot_id=evidence,
                    effect=f"Clean only {', '.join(t['name'] for t in targets)} ({mode})")
        if rooms is not None or suction is not None or passes is not None or ordered:
            plan.update(rooms=[t['name'] for t in targets],region_ids=[t['vendor_region_id'] for t in targets],ordered=ordered,suction=suction,passes=passes)
    if action=='clean_area':
        if mode not in ('vacuum','vacuum_and_mop'):raise ControlError('UnsupportedCleaningMode')
        if rooms is not None or room is not None or ordered or suction is not None or passes is not None:raise ControlError('SpotRoomSettingsRefused')
        from spot_control import prepare as prepare_spot
        plan.update(prepare_spot(snapshot,map_id,map_version,area_points,snapshot_id,mode))
        plan.update(mode=mode,operating_mode=2 if mode=='vacuum' else 32)
    if action=='drive_garbage':
        from garbage_navigation import target
        plan.update(target(snapshot))
    plan['intent_hash']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    with contextlib.closing(database()) as db, db:
        db.execute('INSERT INTO plans VALUES(?,?,?)',(plan['plan_id'],now,json.dumps(plan)))
    return plan

def get_plan(plan_id, allow_expired=False):
    try:uuid.UUID(plan_id)
    except (ValueError,TypeError,AttributeError):raise ControlError('InvalidPlanId') from None
    with contextlib.closing(database()) as db:
        row=db.execute('SELECT data FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row:raise ControlError('UnknownPlan')
    plan=json.loads(row[0])
    if not allow_expired and plan['expires_at']<time.time():raise ControlError('PlanExpired')
    return plan

def result(plan_id):
    with contextlib.closing(database()) as db:
        row=db.execute('SELECT stage,result FROM attempts WHERE plan_id=?',(plan_id,)).fetchone()
    if not row:return None
    data=json.loads(row[1])
    return {'plan_id':plan_id,'stage':row[0], **data, 'retry_permitted':False}

def record(plan_id,stage,data):
    with contextlib.closing(database()) as db,db:
        db.execute('INSERT INTO command_audit(plan_id,at,stage,data) VALUES(?,?,?,?)',(plan_id,time.time(),stage,json.dumps(data)))
        db.execute('INSERT INTO attempts VALUES(?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET updated=excluded.updated,stage=excluded.stage,result=excluded.result',
                   (plan_id,time.time(),stage,json.dumps(data)))

def history(limit=20):
    with contextlib.closing(database()) as db:
        rows=db.execute('SELECT plan_id,updated,stage,result FROM attempts ORDER BY updated DESC LIMIT ?', (max(1,min(limit,100)),)).fetchall()
    return [{'plan_id':r[0],'at':r[1],'stage':r[2],**json.loads(r[3])} for r in rows]

def audit(plan_id):
    get_plan(plan_id,allow_expired=True)
    with contextlib.closing(database()) as db:
        rows=db.execute('SELECT at,stage,data FROM command_audit WHERE plan_id=? ORDER BY id',(plan_id,)).fetchall()
    return [{'at':r[0],'stage':r[1],**json.loads(r[2])} for r in rows]

@contextlib.contextmanager
def exclusive_writer():
    fd=os.open(ROOT/'observer-data/.writer.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ControlError('AnotherCommandInProgress') from None
        yield
    finally:os.close(fd)

def resolve_room(name,map_id):
    from room_labels import names_by_map
    matches=[v for v in names_by_map().get(map_id,{}).values() if v['name'].casefold()==name.strip().casefold()]
    if len(matches)!=1:raise ControlError('UnknownOrAmbiguousRoom')
    return {'map_id':map_id,**matches[0],'regional_payload_supported':True}

def validate_retained_region(snapshot,map_id,version,region_id):
    for item in snapshot.get('maps',[]):
        doc=read_local('/api/map/'+str(item['id']))
        files=doc.get('files',{})
        meta=next((v for k,v in files.items() if k.split('/')[-1]=='metadata.json'),{})
        if meta.get('id')!=map_id or meta.get('version')!=version:continue
        room_files={layer['filepath'] for layer in item.get('layers',[]) if layer.get('type')=='rooms' and layer.get('retained')}
        regions=[f for name in room_files for f in files.get(name,{}).get('features',[]) if str(f.get('id'))==str(region_id)]
        if len(regions)==1 and regions[0].get('geometry',{}).get('type') in ('Polygon','MultiPolygon'):
            return item['id']
    raise ControlError('RoomMissingFromVerifiedActiveMapGeometry')

def region_command(plan,blid):
    from roombapy_prime.models import RoutineCommand, MissionCommandType, Region, RegionType, CommandParams
    if plan.get('action')=='drive_garbage':
        if not plan.get('destination') or not plan.get('map_id') or not plan.get('map_version'):raise ControlError('IncompleteGarbageDestination')
        return RoutineCommand(command_type=MissionCommandType.CLEAN_SPOT,asset_id=blid,map_id=plan['map_id'],pmap_version_id=plan['map_version'],
            spot_geometry={'center':plan['destination']},params=CommandParams(operating_mode=2),initiator='rmtApp')
    if plan.get('action')=='clean_area':
        from roombapy_prime.models.mission_control import CommandPolygon
        if not plan.get('area_points') or plan.get('temporary_region_id')!='161' or not plan.get('map_id') or not plan.get('map_version'):raise ControlError('IncompleteSpotTarget')
        return RoutineCommand(command_type=MissionCommandType.START,asset_id=blid,map_id=plan['map_id'],pmap_version_id=plan['map_version'],
            regions=[Region(region_id='161',region_type=RegionType.TID,params=CommandParams(operating_mode=plan['operating_mode']))],
            id_multipolys=[CommandPolygon(polygon_id='161',poly=plan['area_points'])],initiator='rmtApp')
    if plan.get('action')!='clean_room' or not plan.get('map_id') or not plan.get('map_version') or not plan.get('region_id'):
        raise ControlError('IncompleteRegionalTarget')
    params=CommandParams(operating_mode=plan['operating_mode'],suction_level=plan.get('suction'),
        two_pass=None if plan.get('passes') is None else plan['passes']==2)
    return RoutineCommand(command_type=MissionCommandType.START,asset_id=blid,map_id=plan['map_id'],
        pmap_version_id=plan['map_version'],ordered=1 if plan.get('ordered') else 0,
        regions=[Region(region_id=rid,region_type=RegionType.RID,params=params) for rid in plan.get('region_ids',[plan['region_id']])],initiator='rmtApp')

def payload_matches(plan,payload,blid):
    body=dict(payload);stamp=body.pop('time',None)
    if type(stamp) is not int or abs(time.time()-stamp)>10:return False
    if plan['action']=='drive_garbage':
        expected=region_command(plan,blid).to_json();expected['initiator']='localApp'
        return body==expected and body.get('command')=='point_clean' and body.get('geom')=={'center':plan['destination']} and not body.get('select_all')
    if plan['action'] in ('clean_room','clean_area'):
        expected=region_command(plan,blid).to_json();expected['initiator']='localApp'
        return body==expected and len(body.get('regions',[]))==(1 if plan['action']=='clean_area' else len(plan.get('region_ids',[plan['region_id']]))) and not body.get('select_all')
    return body=={'command':plan['action'],'initiator':'localApp'}

def policy():
    file=ROOT/'observer-data/control-policy.json'
    return json.loads(file.read_text()) if file.exists() else {'direct_chat_commands':False,'automatic_resume':False}

def notices(after=0,limit=50):
    with contextlib.closing(database()) as db:
        rows=db.execute('SELECT id,at,data FROM notices WHERE id>? ORDER BY id LIMIT ?', (after,max(1,min(limit,100)))).fetchall()
    return {'events':[{'id':r[0],'at':r[1],**json.loads(r[2])} for r in rows],'next_cursor':rows[-1][0] if rows else after}

def notify(key,data):
    with contextlib.closing(database()) as db,db:
        created=db.execute('INSERT OR IGNORE INTO notices(at,dedup,data) VALUES(?,?,?)',(time.time(),key,json.dumps(data))).rowcount
    if created and policy().get('desktop_notifications'):
        # Static AppleScript reads the message as argv; data is never interpolated as code.
        import subprocess
        text='VacuumGPT: '+str(data.get('message',data.get('kind','Mission event')))[:350]
        try:subprocess.run(['/usr/bin/osascript','-e','on run argv\ndisplay notification (item 1 of argv) with title "VacuumGPT"\nend run',text],capture_output=True,timeout=5)
        except (OSError,subprocess.TimeoutExpired):pass
    return bool(created)

def job(mission_id):
    with contextlib.closing(database()) as db:
        row=db.execute('SELECT data FROM jobs WHERE mission_id=?',(mission_id,)).fetchone()
    return json.loads(row[0]) if row else None

def save_job(mission_id,data):
    with contextlib.closing(database()) as db,db:
        db.execute('INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(mission_id) DO UPDATE SET updated=excluded.updated,data=excluded.data', (mission_id,time.time(),json.dumps(data)))

def hold_mission(mission_id,action):
    current=job(mission_id)
    if current:
        current.update(held=True,hold_reason='explicit_'+action);save_job(mission_id,current)

def track_result(plan,answer):
    if answer.get('robot_action_confirmed') and plan['action'] in ('start','clean_room','clean_area') and answer.get('mission_id'):
        save_job(answer['mission_id'],{'mission_id':answer['mission_id'],'plan_id':plan['plan_id'],'room':plan.get('room'),'region_id':plan.get('region_id'),
            'map_id':plan.get('map_id'),'map_version':plan.get('map_version'),'rooms':plan.get('rooms'),
            'region_ids':plan.get('region_ids'),'started_at':time.time(),'last_phase':'run','seen_running':True,'held':False,
            'automatic_resume':bool(policy().get('automatic_resume')),'resume_count':0,'finished':False})
    elif answer.get('robot_action_confirmed') and plan['action']=='resume':
        current=job(plan['mission_id'])
        if current:current.update(held=False,last_phase='run',pause_since=None);save_job(plan['mission_id'],current)
    if plan['action'] in ('start','clean_room','clean_area') and not answer.get('robot_action_confirmed'):
        notify('command-unconfirmed:'+plan['plan_id'],{'kind':'command_unconfirmed','plan_id':plan['plan_id'],
            'message':f"{plan.get('room') or plan['action']} command unconfirmed; no retry was sent.", 'outcome':answer.get('outcome')})

def launch_writer(plan_id):
    import subprocess
    get_plan(plan_id,allow_expired=True)
    prior=result(plan_id)
    if prior:return {**prior,'duplicate_request':True}
    try:
        process=subprocess.run([str(ROOT/'.venv/bin/python'),str(ROOT/'supervised_command.py'),plan_id],capture_output=True,timeout=180)
    except subprocess.TimeoutExpired:
        answer=result(plan_id)
        return {**(answer or {}),'error_code':'WriterTimeout','outcome':'unknown','may_have_executed':True,'retry_permitted':False}
    try:answer=json.loads(process.stdout)
    except ValueError:raise ControlError('WriterResultUnavailable') from None
    if process.returncode and not answer.get('plan_id'):raise ControlError(answer.get('error_code','CommandRefused'))
    return answer
