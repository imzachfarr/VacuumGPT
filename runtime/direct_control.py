"""Owner-granted chat command gateway with request-level idempotency."""
import contextlib
import fcntl
import json
import os
import re
import time
import control_layer as c

def previous_request(request_id,intent):
    with contextlib.closing(c.database()) as db:
        prior=db.execute('SELECT plan_id,intent FROM requests WHERE request_id=?',(request_id,)).fetchone()
    if not prior:return None
    if prior[1]!=intent:raise c.ControlError('RequestIdIntentMismatch')
    return c.result(prior[0]) or {'plan_id':prior[0],'outcome':'pending_or_interrupted','retry_permitted':False}

def run(action,request_id,room=None,mode='default',rooms=None,ordered=False,suction=None,passes=None,owner_navigation_retry=False,area_points=None,map_id=None,map_version=None,snapshot_id=None,owner_brush_retry=False):
    request_started_at=time.time()
    resume_hold_generations=c.resume_fence_snapshot() if action=='resume' else None
    if not c.policy().get('direct_chat_commands'):raise c.ControlError('PersistentRoomControlGrantRequired')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',request_id):raise c.ControlError('StableUniqueRequestIdRequired')
    intent=json.dumps({'action':action,'room':room,'mode':mode,'rooms':rooms,'ordered':ordered,'suction':suction,'passes':passes},sort_keys=True)
    if action=='clean_area':intent=json.dumps({'intent':intent,'area_points':area_points,'map_id':map_id,'map_version':map_version,'snapshot_id':snapshot_id},sort_keys=True)
    if owner_navigation_retry:
        if action!='dock' or type(owner_navigation_retry) is not bool:raise c.ControlError('NavigationRetryOnlyForExplicitDock')
        intent=json.dumps({'intent':intent,'owner_navigation_retry':True},sort_keys=True)
    if owner_brush_retry:
        if type(owner_brush_retry) is not bool or action!='resume' or owner_navigation_retry:raise c.ControlError('BrushRetryOnlyForExplicitResume')
        intent=json.dumps({'intent':intent,'owner_brush_retry':True},sort_keys=True)
    prior=previous_request(request_id,intent)
    if prior is not None:return prior
    held_mission=None
    if action in ('pause','stop','dock'):
        snapshot=c.read_local('/api/status')
        if snapshot.get('target_sku')!=c.TARGET:raise c.ControlError('FreshExactDeviceStateRequired')
        held_mission=snapshot.get('state',{}).get('data',{}).get('cleanMissionStatus',{}).get('missionId')
        if held_mission:c.hold_mission(held_mission,action)
    fd=os.open(c.ROOT/'observer-data/.chat-command.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise c.ControlError('AnotherChatCommandInProgress') from None
        prior=previous_request(request_id,intent)
        if prior is not None:return prior
        selected_mode=c.policy().get('default_room_mode','vacuum') if mode=='default' else mode
        plan=c.prepare(action,room=room,mode=selected_mode,rooms=rooms,ordered=ordered,suction=suction,passes=passes,owner_navigation_retry=owner_navigation_retry,area_points=area_points,map_id=map_id,map_version=map_version,snapshot_id=snapshot_id,owner_brush_retry=owner_brush_retry,request_started_at=request_started_at,resume_hold_generations=resume_hold_generations)
        if action in ('pause','stop','dock') and plan.get('mission_id') and plan['mission_id']!=held_mission:
            c.hold_mission(plan['mission_id'],action)
        with contextlib.closing(c.database()) as db,db:
            if owner_brush_retry:
                now=time.time()
                db.execute('CREATE TABLE IF NOT EXISTS inspected_brush_retries(mission_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, at REAL NOT NULL, plan_id TEXT NOT NULL)')
                if db.execute('SELECT 1 FROM inspected_brush_retries WHERE mission_id=?',(plan['mission_id'],)).fetchone():raise c.ControlError('BrushRetryAlreadyClaimedForMission')
                if db.execute('SELECT 1 FROM inspected_brush_retries WHERE at>?',(now-21600,)).fetchone():raise c.ControlError('BrushRetryLimitOneInSixHours')
                db.execute('INSERT INTO inspected_brush_retries VALUES(?,?,?,?)',(plan['mission_id'],request_id,now,plan['plan_id']))
            if owner_navigation_retry:
                # Claim before dispatch, including uncertain/interrupted attempts.
                # The chat lock serializes requests; SQLite makes claims durable.
                now=time.time()
                db.execute('CREATE TABLE IF NOT EXISTS navigation_retries(request_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, at REAL NOT NULL, plan_id TEXT NOT NULL)')
                rows=db.execute('SELECT at FROM navigation_retries WHERE at>? ORDER BY at DESC',(now-21600,)).fetchall()
                if rows and now-rows[0][0]<180:raise c.ControlError('NavigationRetryCooldown180Seconds')
                if len(rows)>=2:raise c.ControlError('NavigationRetryLimitTwoInSixHours')
                if db.execute('SELECT 1 FROM navigation_retries WHERE mission_id=?',(plan['mission_id'],)).fetchone():
                    raise c.ControlError('NavigationRetryAlreadyClaimedForMission')
                db.execute('INSERT INTO navigation_retries VALUES(?,?,?,?)',(request_id,plan['mission_id'],now,plan['plan_id']))
            db.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,plan['plan_id'],intent))
        from write_approval import grant_explicit_request
        grant_explicit_request(plan,('owner-requested inspected-brush resume retry:' if owner_brush_retry else 'owner-requested cleared-path docking retry:' if owner_navigation_retry else 'owner-granted chat request:')+request_id)
        return c.launch_writer(plan['plan_id'])
    finally:os.close(fd)
