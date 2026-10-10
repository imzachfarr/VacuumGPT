"""Durable exact-device plans and at-most-once dispatch journal. No network writes."""
import contextlib
import fcntl
import hashlib
import json
import math
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

def brush_resume_anchor(mission):
    """A brush retry may continue only a complete retained cleaning mission."""
    anchor={'mission_id':mission.get('missionId'),'mission_start':mission.get('mssnStrtTm'),
            'n_mission':mission.get('nMssn'),'cycle':mission.get('cycle'),'operating_mode':mission.get('operatingMode')}
    if (not isinstance(anchor['mission_id'],str) or not anchor['mission_id']
            or anchor['cycle']!='clean' or type(anchor['mission_start']) not in (int,float)
            or not math.isfinite(anchor['mission_start']) or anchor['mission_start']<=0
            or type(anchor['n_mission']) is not int or anchor['n_mission']<0
            or type(anchor['operating_mode']) is not int or anchor['operating_mode']<0):
        raise ControlError('BrushRecoveryRequiresActiveNativeMission')
    return anchor

def resume_anchor(mission):
    """Resume continues a complete retained native cleaning mission."""
    try:return brush_resume_anchor(mission)
    except ControlError:raise ControlError('ResumeRequiresActiveNativeMission') from None

def brush_resume_battery(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100:
        raise ControlError('RecoveryBatteryUnverified')
    if value<25:raise ControlError('RecoveryBatteryTooLow')

def preconditions(action, mission, owner_navigation_retry=False, owner_brush_retry=False, retained_stop_pause=False):
    phase=mission.get('phase')
    if phase is None or 'error' not in mission:
        raise ControlError('IncompleteRobotState')
    if owner_navigation_retry and (action!='dock' or mission.get('error')!=1010 or phase!='stuck' or not mission.get('missionId') or 'notReady' not in mission or 'condNotReady' not in mission):
        raise ControlError('NavigationRetryRequiresConfirmed1010StuckState')
    if owner_brush_retry:
        if action!='resume' or type(mission.get('error')) is not int or mission['error']!=2 or phase!='stuck' or type(mission.get('notReady')) is not int or mission['notReady']!=0 or mission.get('condNotReady')!=[]:
            raise ControlError('InspectedBrushRetryRequiresExactReadyError2')
        brush_resume_anchor(mission)
    if type(retained_stop_pause) is not bool:raise ControlError('RetainedPauseProofMustBeBoolean')
    if retained_stop_pause:
        start=mission.get('mssnStrtTm')
        if action not in ('pause','resume') or phase!='stop' or mission.get('cycle')!='clean' or type(start) not in (int,float) or not math.isfinite(start) or start<=0 or not mission.get('missionId') or type(mission.get('error')) is not int or mission['error']!=0 or type(mission.get('notReady')) is not int or mission['notReady']!=0 or mission.get('condNotReady')!=[]:
            raise ControlError('RetainedPauseRequiresReadyCurrentCleanMission')
    navigation_exception=action=='dock' and owner_navigation_retry is True and mission.get('error')==1010
    if ((mission.get('error') and not navigation_exception and not owner_brush_retry) or mission.get('notReady') or mission.get('condNotReady')) and action in ('start','clean_room','clean_area','resume','dock'):
        raise ControlError('RobotFaultRequiresOwner')
    if action=='pause' and phase not in ('run','pause','paused') and not retained_stop_pause:
        raise ControlError('PauseRequiresRunningMission')
    if action=='resume' and not owner_brush_retry and phase not in ('pause','paused') and not retained_stop_pause:
        raise ControlError('ResumeRequiresPausedMission')
    if action=='resume' and not mission.get('missionId'):
        raise ControlError('ResumeRequiresExistingMissionIdentity')
    if action=='resume' and not owner_brush_retry:
        resume_anchor(mission)
        if type(mission.get('error')) is not int or mission['error']!=0 or type(mission.get('notReady')) is not int or mission['notReady']!=0 or mission.get('condNotReady')!=[]:
            raise ControlError('ResumeRequiresReadyRobot')
    if action in ('start','clean_room','clean_area') and phase not in ('charge','idle','stop','stopped'):
        raise ControlError('StartRequiresIdleRobot')
    if action in ('start','clean_room','clean_area') and mission.get('cycle') not in (None,'none'):
        raise ControlError('ActiveMissionRequiresResumeOrStop')

def resume_fence_snapshot():
    """Capture owner generations before a status read can yield to a stop."""
    with contextlib.closing(database()) as db:
        rows=db.execute('SELECT mission_id,data FROM jobs').fetchall()
    return {mission_id:json.loads(data).get('hold_generation',0) for mission_id,data in rows}

def prepare(action, snapshot=None, room=None, mode='vacuum', rooms=None, ordered=False, suction=None, passes=None, owner_navigation_retry=False, area_points=None, map_id=None, map_version=None, snapshot_id=None, owner_brush_retry=False, recovery_context=None, request_started_at=None, resume_hold_generations=None):
    request_started_at=time.time() if request_started_at is None else request_started_at
    if type(request_started_at) not in (int,float) or not math.isfinite(request_started_at) or request_started_at<=0:
        raise ControlError('ResumeIntentTimeUnverified')
    if action=='resume':
        resume_hold_generations=resume_fence_snapshot() if resume_hold_generations is None else resume_hold_generations
        if not isinstance(resume_hold_generations,dict):raise ControlError('ResumeOwnerFenceUnverified')
    if action in ('drive_garbage','clean_area') and not policy().get('experimental_geometry_commands'):
        raise ControlError('ExperimentalGeometryCommandsDisabled')
    if action not in ACTIONS:
        raise ControlError('UnsupportedAction')
    if action=='clean_area' and policy().get('spot_cleaning_blocked'):
        raise ControlError('SpotTargetingFailedRequiresCommandFix')
    snapshot=snapshot if snapshot is not None else read_local('/api/status')
    if snapshot.get('target_sku')!=TARGET or snapshot.get('state_stale') or snapshot.get('connection',{}).get('data',{}).get('status')!='connected':
        raise ControlError('FreshExactDeviceStateRequired')
    mission=snapshot.get('state',{}).get('data',{}).get('cleanMissionStatus',{})
    if type(owner_navigation_retry) is not bool or owner_navigation_retry and action!='dock':raise ControlError('NavigationRetryOnlyForExplicitDock')
    if type(owner_brush_retry) is not bool or owner_brush_retry and (action!='resume' or owner_navigation_retry):raise ControlError('BrushRetryOnlyForExplicitResume')
    retained_stop_pause=False
    if action in ('pause','resume') and mission.get('phase')=='stop':
        import recovery_context as rc
        retained_stop_pause=rc.is_paused(snapshot.get('state',{}).get('data',{}))
    preconditions('clean_area' if action=='drive_garbage' else action,mission,owner_navigation_retry,owner_brush_retry,retained_stop_pause)
    now=time.time()
    plan={'plan_id':str(uuid.uuid4()), 'action':action,'target_sku':TARGET,
          'fingerprint':FINGERPRINT,'mission_id':mission.get('missionId'),
          'phase':mission.get('phase'),'created_at':now,'expires_at':now+600,
          'effect':{'start':'Start whole-house cleaning','stop':'Stop current mission','pause':'Pause current mission','resume':'Resume current mission','dock':'Return to dock','find':'Sound locator chime','clean_room':'Clean exactly one named room','clean_area':'Clean the drawn spot using a temporary region','drive_garbage':'Travel to the garbage can with brief vacuuming, then pause'}[action]}
    if retained_stop_pause:
        plan['retained_stop_pause']=True
    if action=='pause' and (mission.get('phase') in ('pause','paused') or retained_stop_pause):
        plan['no_op']=True
    if action=='resume':
        plan['expected_native_anchor']=resume_anchor(mission)
        current_job=job(plan['mission_id'])
        generation=(current_job or {}).get('hold_generation',0)
        entry_generation=resume_hold_generations.get(plan['mission_id'],0)
        held_at=(current_job or {}).get('hold_at')
        if (type(generation) is not int or generation<0 or type(entry_generation) is not int
                or entry_generation<0 or generation!=entry_generation
                or held_at is not None and (type(held_at) not in (int,float) or not math.isfinite(held_at) or held_at>request_started_at)):
            raise ControlError('BrushRecoveryOwnerHoldChanged' if owner_brush_retry else 'ResumeOwnerHoldChanged')
        if current_job and current_job.get('finished'):
            raise ControlError('BrushRecoveryMissionCompleted' if owner_brush_retry else 'ResumeMissionCompleted')
        plan['request_started_at']=request_started_at
        plan['expected_hold_generation']=generation
    if recovery_context is not None:
        if not isinstance(recovery_context,dict):raise ControlError('RecoveryContextMustBeObject')
        if recovery_context.get('mission_id')!=plan['mission_id']:raise ControlError('RecoveryContextMissionMismatch')
        plan['recovery_context']=json.loads(json.dumps(recovery_context))
    if owner_brush_retry:
        brush_resume_battery(snapshot['state']['data'].get('batPct'))
        plan.update(owner_brush_retry=True,expected_error=2,expected_cycle='clean',
                    expected_native_anchor=brush_resume_anchor(mission))
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
    if action=='start':
        maps=snapshot.get('state',{}).get('data',{}).get('p2maps') or []
        if isinstance(maps,list) and len(maps)==1 and isinstance(maps[0],dict) and maps[0].get('p2map_id') and maps[0].get('p2mapv_id'):
            plan.update(map_id=maps[0]['p2map_id'],map_version=maps[0]['p2mapv_id'])
    if action in ('start','clean_room','clean_area'):
        import map_recovery
        if action=='start':
            # Basic start remains available when recovery geometry is missing.
            # The monitor requires this context before any automatic resume.
            try:plan['recovery_map_context']=map_recovery.from_plan(plan)
            except (ControlError,OSError,ValueError,sqlite3.Error):plan['recovery_map_context']=None
        else:plan['recovery_map_context']=map_recovery.from_plan(plan)
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
        if plan.get('navigation_method')=='mapped_room_approach':
            if not plan.get('region_id'):raise ControlError('IncompleteRegionalTarget')
            return RoutineCommand(command_type=MissionCommandType.START,asset_id=blid,map_id=plan['map_id'],pmap_version_id=plan['map_version'],
                regions=[Region(region_id=plan['region_id'],region_type=RegionType.RID,params=CommandParams(operating_mode=2))],initiator='rmtApp')
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
        if plan.get('navigation_method')=='mapped_room_approach':return body==expected and body.get('command')=='start' and not body.get('select_all')
        return body==expected and body.get('command')=='point_clean' and body.get('geom')=={'center':plan['destination']} and not body.get('select_all')
    if plan['action'] in ('clean_room','clean_area'):
        expected=region_command(plan,blid).to_json();expected['initiator']='localApp'
        return body==expected and len(body.get('regions',[]))==(1 if plan['action']=='clean_area' else len(plan.get('region_ids',[plan['region_id']]))) and not body.get('select_all')
    if plan['action']=='soft_reboot':return body=={'command':'reset','initiator':'rmtApp'}
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
    from recovery_store import fence
    return fence(mission_id,'explicit_'+action)

def track_result(plan,answer):
    from recovery_store import transaction
    confirmed=answer.get('robot_action_confirmed')
    action=plan['action']
    if confirmed and action in ('start','clean_room','clean_area') and answer.get('mission_id'):
        mission_id=answer['mission_id']
        # Copy the complete intent and anchor before the transaction so callers
        # cannot later mutate the persisted recovery evidence.
        fields=('action','room','rooms','region_id','region_ids','map_id','map_version',
                'mode','operating_mode','suction','passes','ordered','area_points',
                'temporary_region_id')
        intent=json.loads(json.dumps({key:plan.get(key) for key in fields}))
        anchor=answer.get('native_anchor')
        anchor=json.loads(json.dumps(anchor)) if isinstance(anchor,dict) and anchor.get('mission_id')==mission_id else None
        map_context=json.loads(json.dumps(plan.get('recovery_map_context')))
        requested=bool(policy().get('automatic_resume'))
        now=time.time()
        def update(current,db):
            current=current or {'mission_id':mission_id,'held':False,'hold_generation':0,
                                'resume_count':0,'finished':False,'started_at':now}
            current.setdefault('started_at',now)
            current.setdefault('resume_count',0)
            current.setdefault('finished',False)
            # Keep current hold/generation, completion and recovery budgets. An
            # owner fence may have landed while the command was being observed.
            current.update(plan_id=plan['plan_id'],intent=intent,native_anchor=anchor,
                           recovery_map_context=map_context,automatic_resume=requested,
                           observed_only=False,last_phase='run',seen_running=True)
            current.update({key:value for key,value in intent.items() if key!='action'})
            return current,None
        transaction(mission_id,update)
    elif confirmed and action=='resume' and plan.get('mission_id'):
        def update(current,db):
            if not current:return None,None
            expected=plan.get('expected_hold_generation')
            recovery=plan.get('recovery_context')
            same_generation=(type(expected) is int and expected==current.get('hold_generation',0))
            if recovery is not None:
                same_generation=(same_generation and recovery.get('generation')==expected
                                 and recovery.get('incident')==current.get('pause_incident')
                                 and recovery.get('attempt_id')==current.get('recovery_attempt_id'))
            if same_generation and not current.get('finished'):
                current.update(held=False,hold_reason=None,last_phase='run',pause_since=None,authorized_pause=None)
            else:
                # Motion may have been observed, but it cannot cancel an owner
                # decision made after this plan was prepared.
                current.update(last_phase='run',seen_running=True)
            anchor=answer.get('native_anchor')
            if isinstance(anchor,dict) and anchor.get('mission_id')==plan['mission_id']:
                current['native_anchor']=json.loads(json.dumps(anchor))
            return current,None
        transaction(plan['mission_id'],update)
    if action in ('start','clean_room','clean_area') and not confirmed:
        notify('command-unconfirmed:'+plan['plan_id'],{'kind':'command_unconfirmed','plan_id':plan['plan_id'],
            'message':f"{plan.get('room') or action} command unconfirmed; no retry was sent.", 'outcome':answer.get('outcome')})

def launch_writer(plan_id):
    import subprocess
    get_plan(plan_id,allow_expired=True)
    prior=result(plan_id)
    if prior:return {**prior,'duplicate_request':True}
    try:
        timeout=210 if get_plan(plan_id,allow_expired=True)['action']=='soft_reboot' else 180
        process=subprocess.run([str(ROOT/'.venv/bin/python'),str(ROOT/'supervised_command.py'),plan_id],capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired:
        answer=result(plan_id)
        return {**(answer or {}),'error_code':'WriterTimeout','outcome':'unknown','may_have_executed':True,'retry_permitted':False}
    try:answer=json.loads(process.stdout)
    except ValueError:raise ControlError('WriterResultUnavailable') from None
    if process.returncode and not answer.get('plan_id'):raise ControlError(answer.get('error_code','CommandRefused'))
    return answer
