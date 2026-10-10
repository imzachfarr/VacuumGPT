"""Owner-approved targeted vacuum trip, paused on fresh, same-map arrival."""
import asyncio
import contextlib
import json
import math
import time
import control_layer as c

RADIUS=.25
ARRIVAL=.45

def target(snapshot):
    from named_locations import get
    from spot_control import prepare
    loc=get()['location']
    if not loc:raise c.ControlError('GarbageCanLocationRequired')
    point=loc['point']
    from shapely.geometry import shape,Point
    from shapely.ops import unary_union
    from spot_control import file,features
    doc=c.read_local('/api/map/'+str(loc['snapshot_id']))
    boundary=unary_union([shape(f['geometry']) for f in features(file(doc,'borders.geojson')) if f.get('geometry',{}).get('type') in ('Polygon','MultiPolygon')])
    radius=min(RADIUS,Point(point).distance(boundary.boundary)*.8)
    if radius<.02:raise c.ControlError('MoveGarbageCanPinFurtherInsideOpenFloor')
    ring=[[point[0]+radius*math.cos(i*math.tau/24),point[1]+radius*math.sin(i*math.tau/24)] for i in range(24)]
    details=prepare(snapshot,loc['map_id'],loc['map_version'],ring,loc['snapshot_id'],'vacuum')
    current=c.read_local('/api/map/'+str(details['map_snapshot_id']))
    rooms=[f for f in features(file(current,'rooms.geojson')) if f.get('geometry',{}).get('type') in ('Polygon','MultiPolygon') and shape(f['geometry']).covers(Point(point))]
    if len(rooms)!=1 or rooms[0].get('id') is None:raise c.ControlError('GarbageCanRequiresUnambiguousMappedRoom')
    rid=str(rooms[0]['id'])
    c.validate_retained_region(snapshot,details['map_id'],details['map_version'],rid)
    from room_labels import room_name
    return {**details,'destination':point,'location_revision':loc['revision'],'operating_mode':2,'mode':'vacuum',
            'navigation_method':'mapped_room_approach','region_id':rid,'room':room_name(details['map_id'],rid),
            'effect':'Approach the mapped room containing the garbage can, then pause; exact bin arrival is unverified'}

def distance(status,plan,mission,started):
    # Live frames lack an independently verified saved-map coordinate transform.
    # A room approach must never claim bin arrival from the inferred map label.
    if plan.get('navigation_method')=='mapped_room_approach':return None
    p=status.get('pose') or {};d=p.get('data') or {};points=d.get('points') or []
    if status.get('pose_stale') or p.get('age_seconds',999)>8 or p.get('at',0)<started or p.get('mission')!=mission or d.get('map_id')!=plan['map_id'] or not points:return None
    q=points[-1]
    if any(type(q.get(k)) not in (int,float) or not math.isfinite(q[k]) for k in ('x','y')):return None
    return math.hypot(q['x']-plan['destination'][0],q['y']-plan['destination'][1])

def cancellation(plan_id):
    with contextlib.closing(c.database()) as db:
        db.execute('CREATE TABLE IF NOT EXISTS navigation_cancels(plan_id TEXT PRIMARY KEY, at REAL)')
        return bool(db.execute('SELECT 1 FROM navigation_cancels WHERE plan_id=?',(plan_id,)).fetchone())

def cancel(request_id):
    with contextlib.closing(c.database()) as db,db:
        row=db.execute('SELECT plan_id FROM requests WHERE request_id=?',(request_id,)).fetchone()
        if not row or c.get_plan(row[0],allow_expired=True)['action']!='drive_garbage':raise c.ControlError('NoGarbageCanTripInProgress')
        db.execute('CREATE TABLE IF NOT EXISTS navigation_cancels(plan_id TEXT PRIMARY KEY, at REAL)')
        db.execute('INSERT OR IGNORE INTO navigation_cancels VALUES(?,?)',(row[0],time.time()))
    return {'action':'cancel_navigation','outcome':'pause_requested','retry_permitted':False}

async def run(plan,robot,blid,raw_publish):
    topic=robot._mqtt.cmd_topic(robot._irbt_topic_prefix);previous=robot._mqtt._publish
    owner=asyncio.current_task();stage='start';sent=set();pid=plan['plan_id'];started=time.time();mission=None
    clean=plan
    data={'action':'drive_garbage','destination':plan['destination'],'arrival_confirmed':False,'robot_action_confirmed':False,
          'vacuum_may_run':True,'may_have_executed':False,'outcome':'validating','mqtt_owner':'observer_session',
          'navigation_method':plan.get('navigation_method','native_point_clean_then_pause'),'room':plan.get('room')}
    async def publish(t,p,timeout=5):
        if t!=topic:
            from roombapy_prime.mqtt_client import _shadow_base
            if t==_shadow_base(blid,'ro-currentstate')+'/get' and p in ('',b''):return await raw_publish(t,p,timeout)
            raise c.ControlError('UnapprovedPublishBlocked')
        expected=clean if stage=='start' else {**plan,'action':'pause'}
        if asyncio.current_task() is not owner or stage in sent or time.time()>plan['approval_expires_at'] or not c.payload_matches(expected,json.loads(p),blid):raise c.ControlError('UnapprovedPublishBlocked')
        sent.add(stage);data.update(may_have_executed=True,outcome='heading_to_garbage_can' if stage=='start' else 'pausing')
        c.record(pid,'dispatching_'+stage,data)
        return await raw_publish(t,p,timeout)
    async def state():
        return (await robot.get_named_shadow('ro-currentstate')).payload['state']['reported']
    robot._mqtt._publish=publish
    try:
        before=await state();m=before['cleanMissionStatus']
        if m.get('missionId')!=plan['mission_id'] or m.get('phase')!=plan['phase']:raise c.ControlError('RobotStateChangedSinceApproval')
        c.preconditions('clean_area',m)
        if not any(x.get('p2map_id')==plan['map_id'] and x.get('p2mapv_id')==plan['map_version'] for x in before.get('p2maps',[])):raise c.ControlError('ActiveMapChangedSinceApproval')
        from named_locations import get
        loc=get()['location']
        if not loc or loc['revision']!=plan['location_revision'] or loc['point']!=plan['destination']:raise c.ControlError('GarbageCanLocationChanged')
        ack=await robot.send_routine_command_via_cmd_topic(c.region_command(clean,blid))
        data['broker_acknowledged']=bool(ack);mission=None;reason='trip_timeout';last_pose=time.monotonic();seen_pose=False;deadline=time.monotonic()+min(75,max(0,plan['approval_expires_at']-time.time()-15))
        while time.monotonic()<deadline:
            await asyncio.sleep(1)
            reported=await state();m=reported['cleanMissionStatus'];mid=m.get('missionId');phase=m.get('phase')
            if mission is None:
                if phase!='run':
                    if m.get('error'):reason='robot_fault';break
                    continue
                if mid==plan['mission_id']:continue
                mission=mid
                # Deliberately held: automatic recovery must never restart this trip.
                c.save_job(mid,{'mission_id':mid,'plan_id':pid,'held':True,'hold_reason':'garbage_can_trip','finished':False,'automatic_resume':False})
            elif mid!=mission:
                reason='mission_changed';break
            data.update(mission_id=mission,after_phase=phase,robot_error=m.get('error'))
            if phase!='run':reason='robot_fault' if m.get('error') else 'trip_interrupted';break
            if not any(x.get('p2map_id')==plan['map_id'] for x in reported.get('p2maps',[])):reason='map_changed';break
            try:s=await asyncio.to_thread(c.read_local,'/api/status');dist=distance(s,plan,mission,started)
            except Exception:dist=None
            if dist is not None:last_pose=time.monotonic();seen_pose=True;data['distance_to_destination']=dist
            c.record(pid,'navigating',data)
            if cancellation(pid):reason='owner_paused';break
            if dist is not None and dist<=ARRIVAL:reason='destination_reached';break
            if time.monotonic()-last_pose>(20 if seen_pose else 60):reason='position_unavailable';break
            if m.get('error'):reason='robot_fault';break
        # Recheck identity immediately before the only allowed interruption.
        m=(await state())['cleanMissionStatus']
        if mission and m.get('missionId')==mission and m.get('phase')=='run':
            stage='pause';c.hold_mission(mission,'pause')
            data['pause_broker_acknowledged']=bool(await robot.send_simple_command('pause'))
            for _ in range(12):
                await asyncio.sleep(1);after=(await state())['cleanMissionStatus']
                if after.get('missionId')!=mission:break
                if after.get('phase') in ('pause','paused') or after.get('phase')=='stop' and after.get('cycle')=='clean':
                    data.update(pause_confirmed=True,robot_action_confirmed=True,after_phase=after['phase']);break
        data.update(outcome='arrived_and_paused' if reason=='destination_reached' and data.get('pause_confirmed') else reason,
                    arrival_confirmed=bool(reason=='destination_reached' and data.get('pause_confirmed')))
        if reason=='destination_reached' and not data.get('pause_confirmed'):data['outcome']='arrival_pause_unconfirmed'
        c.record(pid,'complete',data)
        return c.result(pid)
    except (Exception,asyncio.CancelledError):
        # Attempt only the approved pause, and only for the trip's established mission.
        if mission and 'pause' not in sent:
            try:
                m=(await state())['cleanMissionStatus']
                if m.get('missionId')==mission and m.get('phase')=='run':
                    stage='pause';c.hold_mission(mission,'pause')
                    await robot.send_simple_command('pause')
            except Exception:pass
        raise
    finally:robot._mqtt._publish=previous
