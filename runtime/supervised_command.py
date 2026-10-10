"""Intent-bound writes through one MQTT session; durable at-most-once dispatch."""
import asyncio
import hashlib
import json
import logging
import math
import socket
import sys
import time
from roomba_observer import TARGET, FINGERPRINT, credentials
import control_layer as c
from write_approval import consume
from control_layer import ControlError, get_plan, result, record, exclusive_writer, preconditions, payload_matches, region_command, track_result, hold_mission

def observed(action,before,after):
    retained_pause=action=='pause' and after.get('phase')=='stop'
    if action=='resume' or retained_pause:
        import recovery_context
        anchor=recovery_context.native_anchor({'cleanMissionStatus':before})
        next_anchor=recovery_context.native_anchor({'cleanMissionStatus':after})
        for native in (anchor,next_anchor):
            if (not isinstance(native.get('mission_id'),str) or not native['mission_id']
                    or type(native.get('mission_start')) not in (int,float)
                    or not math.isfinite(native['mission_start']) or native['mission_start']<=0
                    or type(native.get('n_mission')) is not int or native['n_mission']<0
                    or native.get('cycle') in (None,'','none')
                    or type(native.get('operating_mode')) is not int or native['operating_mode']<0):return False
        if anchor!=next_anchor:return False
        if retained_pause and anchor.get('cycle')!='clean':return False
    expected={'pause':('pause','paused','stop'),'resume':('run',),'start':('run',),'clean_room':('run',),'clean_area':('run',),
              'dock':('hmPostMsn','hmUsrDock','dock','charge'),'stop':('stop','stopped','charge','idle')}
    phase=after.get('phase')
    return bool(action in expected and phase in expected[action] and
        (phase!=before.get('phase') or action in ('start','clean_room','clean_area') and before.get('missionId')!=after.get('missionId')) and not after.get('error'))

def recovery_job(plan):
    """Bind one live recovery to its durable claim and the current owner fence."""
    import recovery_store
    context=plan.get('recovery_context')
    if (not isinstance(context,dict) or any(not isinstance(context.get(k),str) or not context[k]
            for k in ('attempt_id','mission_id','incident')) or type(context.get('generation')) is not int
            or context['generation']<0 or plan.get('action')!='resume'
            or context['mission_id']!=plan.get('mission_id')):
        raise ControlError('RecoveryContextRequired')
    row=recovery_store.get(context['attempt_id'])
    saved=c.job(context['mission_id'])
    if (not c.policy().get('automatic_resume') or not saved or not saved.get('automatic_resume')
            or saved.get('held') or saved.get('finished')):
        raise ControlError('RecoveryPermissionChangedBeforeDispatch')
    if (not row or row.get('status')!='validating' or row.get('plan_id')!=plan.get('plan_id')
            or any(row.get(k)!=context[k] for k in ('attempt_id','mission_id','incident','generation'))
            or saved.get('hold_generation',0)!=context['generation']
            or saved.get('pause_incident')!=context['incident']
            or saved.get('recovery_attempt_id')!=context['attempt_id']
            or plan.get('expected_hold_generation',context['generation'])!=context['generation']):
        raise ControlError('RecoveryClaimChangedBeforeDispatch')
    return saved

async def fresh_recovery(plan,robot,currentstate_payload=None):
    """Revalidate robot evidence through the same authenticated connection."""
    import recovery_context
    import map_recovery
    saved=recovery_job(plan)
    currentstate_payload=currentstate_payload or (await robot.get_named_shadow('ro-currentstate')).payload
    reported=currentstate_payload.get('state',{}).get('reported',{})
    mission=reported.get('cleanMissionStatus',{})
    if mission.get('missionId')!=plan['mission_id'] or mission.get('phase')!=plan['phase']:
        raise ControlError('RobotStateChangedSinceApproval')
    software=(await robot.get_named_shadow('rw-software')).payload
    connection=(await robot.get_named_shadow('rw-constatus')).payload
    region_ids=(saved.get('recovery_map_context') or {}).get('region_ids')
    if not isinstance(region_ids,list) or not region_ids:
        raise ControlError('RecoveryMapTargetsUnverified')
    topology=await map_recovery.current(robot,reported,region_ids)
    current=recovery_context.enrich(reported,currentstate_payload,software,connection,topology)
    saved=recovery_job(plan)
    allowed,reason=recovery_context.validate(current,saved)
    if not allowed:raise ControlError(reason)
    return current

async def fresh_pause_state(robot,currentstate_payload):
    """Classify firmware STOP-with-retained-clean using fresh pause evidence."""
    import recovery_context
    software=(await robot.get_named_shadow('rw-software')).payload
    connection=(await robot.get_named_shadow('rw-constatus')).payload
    reported=currentstate_payload.get('state',{}).get('reported',{})
    return recovery_context.enrich(reported,currentstate_payload,software,connection)

def check_brush_owner_fence(plan,saved):
    expected=plan.get('expected_hold_generation')
    current=(saved or {}).get('hold_generation',0)
    if type(expected) is not int or expected<0 or type(current) is not int or current<0 or expected!=current:
        raise ControlError('BrushRecoveryOwnerHoldChanged')
    if saved and saved.get('finished'):raise ControlError('BrushRecoveryMissionCompleted')

def check_resume_owner_fence(plan,saved):
    if plan.get('owner_brush_retry'):
        return check_brush_owner_fence(plan,saved)
    expected=plan.get('expected_hold_generation')
    current=(saved or {}).get('hold_generation',0)
    if type(expected) is not int or expected<0 or type(current) is not int or current<0 or expected!=current:
        raise ControlError('ResumeOwnerHoldChanged')
    if saved and saved.get('finished'):raise ControlError('ResumeMissionCompleted')

def check_resume_mission(plan,mission):
    if plan.get('owner_brush_retry'):
        if c.brush_resume_anchor(mission)!=plan.get('expected_native_anchor'):
            raise ControlError('BrushRecoveryNativeMissionChanged')
    elif c.resume_anchor(mission)!=plan.get('expected_native_anchor'):
        raise ControlError('ResumeNativeMissionChanged')

async def fresh_manual_resume(plan,robot,currentstate_payload=None):
    """Re-read a retained manual mission after any SDK yield before publish."""
    import recovery_context
    currentstate_payload=currentstate_payload or (await robot.get_named_shadow('ro-currentstate')).payload
    reported=currentstate_payload.get('state',{}).get('reported',{})
    mission=reported.get('cleanMissionStatus',{})
    if mission.get('missionId')!=plan.get('mission_id') or mission.get('phase')!=plan.get('phase'):
        raise ControlError('RobotStateChangedSinceApproval')
    reported=await fresh_pause_state(robot,currentstate_payload)
    evidence=reported['_recovery']
    if evidence.get('connected') is not True:raise ControlError('ResumeConnectionUnverified')
    started=plan.get('request_started_at')
    if type(started) not in (int,float) or not math.isfinite(started) or started<=0:
        raise ControlError('ResumeIntentTimeUnverified')
    last=evidence.get('last_command',{})
    if last.get('command') in ('pause','stop','dock','reset','wipe'):
        if type(last.get('time')) not in (int,float) or not math.isfinite(last['time']):
            raise ControlError('ResumeOwnerCommandUnverified')
        if last['time']>=int(started):raise ControlError('ResumeInterruptedByOwnerCommand')
    preconditions('resume',mission,retained_stop_pause=mission.get('phase')=='stop' and recovery_context.is_paused(reported))
    check_resume_mission(plan,mission)
    check_resume_owner_fence(plan,c.job(plan['mission_id']))
    return reported

async def fresh_brush_recovery(plan,robot,currentstate_payload=None):
    """Fixed GETs verify one inspected fault and its immutable native mission."""
    import recovery_context
    currentstate_payload=currentstate_payload or (await robot.get_named_shadow('ro-currentstate')).payload
    software=(await robot.get_named_shadow('rw-software')).payload
    connection=(await robot.get_named_shadow('rw-constatus')).payload
    reported=currentstate_payload.get('state',{}).get('reported',{})
    mission=reported.get('cleanMissionStatus',{})
    preconditions('resume',mission,owner_brush_retry=True)
    c.brush_resume_battery(reported.get('batPct'))
    if (plan.get('expected_cycle')!='clean' or c.brush_resume_anchor(mission)!=plan.get('expected_native_anchor')
            or mission.get('missionId')!=plan.get('mission_id') or mission.get('phase')!=plan.get('phase')):
        raise ControlError('BrushRecoveryNativeMissionChanged')
    current=recovery_context.enrich(reported,currentstate_payload,software,connection)
    evidence=current['_recovery']
    if evidence.get('connected') is not True:raise ControlError('BrushRecoveryConnectionUnverified')
    now=time.time()
    for field in ('phase_reported_at','error_reported_at'):
        stamp=evidence.get(field)
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not -5<=now-stamp<=120:
            raise ControlError('BrushRecoveryTelemetryStale')
    last=evidence.get('last_command',{})
    if last.get('command') in ('pause','stop','dock','reset','wipe'):
        if type(last.get('time')) not in (int,float) or not math.isfinite(last['time']):
            raise ControlError('BrushRecoveryOwnerCommandUnverified')
        if last['time']>=int(plan['created_at']):
            raise ControlError('BrushRecoveryInterruptedByOwnerCommand')
    evidence['shadow_version']=currentstate_payload.get('version')
    check_brush_owner_fence(plan,c.job(plan['mission_id']))
    return current

async def brush_resume_observation(plan,robot,currentstate_payload,command_time,before_evidence):
    """A cached running shadow cannot confirm or release an inspected retry."""
    import recovery_context
    software=(await robot.get_named_shadow('rw-software')).payload
    connection=(await robot.get_named_shadow('rw-constatus')).payload
    reported=currentstate_payload.get('state',{}).get('reported',{})
    current=recovery_context.enrich(reported,currentstate_payload,software,connection)
    evidence=current['_recovery']
    evidence['shadow_version']=currentstate_payload.get('version')
    mission=current.get('cleanMissionStatus',{})
    version=evidence['shadow_version']
    before_version=before_evidence.get('shadow_version')
    last=evidence.get('last_command',{})
    verified=(type(command_time) is int and command_time>0
        and type(before_version) is int and before_version>=0
        and type(version) is int and version>before_version
        and evidence.get('connected') is True
        and mission.get('phase')=='run' and type(mission.get('error')) is int and mission['error']==0
        and type(mission.get('notReady')) is int and mission['notReady']==0 and mission.get('condNotReady')==[]
        and last.get('command')=='resume' and last.get('initiator')=='localApp'
        and type(last.get('time')) is int and last['time']==command_time)
    now=time.time()
    for field in ('phase_reported_at','error_reported_at'):
        stamp=evidence.get(field)
        prior_stamp=before_evidence.get(field)
        verified=bool(verified and type(stamp) in (int,float) and math.isfinite(stamp)
            and type(prior_stamp) in (int,float) and math.isfinite(prior_stamp)
            and stamp>=command_time and stamp>=prior_stamp and -5<=now-stamp<=120)
    try:verified=bool(verified and c.brush_resume_anchor(mission)==plan.get('expected_native_anchor'))
    except ControlError:verified=False
    return current,verified

def record_brush_dispatch(plan,data):
    """Serialize the final at-most-once receipt with a concurrent owner fence."""
    import recovery_store
    def update(saved,db):
        check_brush_owner_fence(plan,saved)
        prior=db.execute('SELECT stage FROM attempts WHERE plan_id=?',(plan['plan_id'],)).fetchone()
        if prior and prior[0]!='validating':raise ControlError('BrushRecoveryAlreadyDispatched')
        now=time.time()
        db.execute('INSERT INTO command_audit(plan_id,at,stage,data) VALUES(?,?,?,?)',
                   (plan['plan_id'],now,'dispatching',json.dumps(data)))
        db.execute('INSERT INTO attempts VALUES(?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET updated=excluded.updated,stage=excluded.stage,result=excluded.result',
                   (plan['plan_id'],now,'dispatching',json.dumps(data)))
        return None,None
    recovery_store.transaction(plan['mission_id'],update)

def record_resume_dispatch(plan,data):
    """The captured owner generation and dispatch receipt commit together."""
    import recovery_store
    def update(saved,db):
        check_resume_owner_fence(plan,saved)
        prior=db.execute('SELECT stage FROM attempts WHERE plan_id=?',(plan['plan_id'],)).fetchone()
        if prior and prior[0]!='validating':raise ControlError('ResumeAlreadyDispatched')
        now=time.time()
        db.execute('INSERT INTO command_audit(plan_id,at,stage,data) VALUES(?,?,?,?)',
                   (plan['plan_id'],now,'dispatching',json.dumps(data)))
        db.execute('INSERT INTO attempts VALUES(?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET updated=excluded.updated,stage=excluded.stage,result=excluded.result',
                   (plan['plan_id'],now,'dispatching',json.dumps(data)))
        return None,None
    recovery_store.transaction(plan['mission_id'],update)

async def run_on_robot(plan,robot,blid,raw_publish):
    """Use the already authenticated exact robot; other observer tasks remain read-only."""
    if plan['action']=='drive_garbage':
        from garbage_navigation import run
        return await run(plan,robot,blid,raw_publish)
    from roombapy_prime.mqtt_client import _shadow_base
    action=plan['action'];plan_id=plan['plan_id']
    command_topic=robot._mqtt.cmd_topic(robot._irbt_topic_prefix)
    allowed={_shadow_base(blid,name)+'/get' for name in (None,'ro-currentstate','rw-software','rw-constatus')}
    previous_publish=robot._mqtt._publish
    command_task=asyncio.current_task()
    sent=False
    command_time=None
    async def guarded_publish(topic,payload,timeout=5.0):
        nonlocal sent,command_time,reported
        if topic in allowed and payload in ('',b''):return await raw_publish(topic,payload,timeout)
        body=json.loads(payload)
        if asyncio.current_task() is not command_task or topic!=command_topic or sent or not payload_matches(plan,body,blid):
            raise ControlError('UnapprovedPublishBlocked')
        if time.time()>plan['approval_expires_at']:raise ControlError('ApprovalExpiredBeforeDispatch')
        if plan.get('automatic_recovery'):
            # The SDK may yield before its publish. Read again at the final guard,
            # then serialize the durable dispatch claim against an owner hold.
            reported=await fresh_recovery(plan,robot)
            if time.time()>plan['approval_expires_at']:raise ControlError('ApprovalExpiredBeforeDispatch')
            if not payload_matches(plan,body,blid):raise ControlError('CommandPayloadExpiredBeforeDispatch')
            recovery_job(plan)
        if plan.get('owner_brush_retry'):
            reported=await fresh_brush_recovery(plan,robot)
            if time.time()>plan['approval_expires_at']:raise ControlError('ApprovalExpiredBeforeDispatch')
            if not payload_matches(plan,body,blid):raise ControlError('CommandPayloadExpiredBeforeDispatch')
        elif action=='resume' and not plan.get('automatic_recovery'):
            reported=await fresh_manual_resume(plan,robot)
            if time.time()>plan['approval_expires_at']:raise ControlError('ApprovalExpiredBeforeDispatch')
            if not payload_matches(plan,body,blid):raise ControlError('CommandPayloadExpiredBeforeDispatch')
        if action=='resume':
            check_resume_mission(plan,reported.get('cleanMissionStatus',{}))
            check_resume_owner_fence(plan,c.job(plan['mission_id']))
        import recovery_context
        dispatch_data={'action':action,'room':plan.get('room'),'region_id':plan.get('region_id'),
            'map_id':plan.get('map_id'),'outcome':'unknown','may_have_executed':True,
            'command_time':body['time'],'native_anchor':recovery_context.native_anchor(reported),
            'before_native_anchor':recovery_context.native_anchor(reported),
            'hold_generation':plan.get('dispatch_hold_generation'),'mqtt_owner':'observer_session'}
        if plan.get('automatic_recovery'):
            import recovery_store
            recovery_store.dispatch(plan['recovery_context']['attempt_id'],plan_id,dispatch_data)
        elif plan.get('owner_brush_retry'):record_brush_dispatch(plan,dispatch_data)
        elif action=='resume':record_resume_dispatch(plan,dispatch_data)
        else:record(plan_id,'dispatching',dispatch_data)
        command_time=body['time']
        sent=True
        try:return await raw_publish(topic,payload,timeout)
        except Exception as exc:
            reason=getattr(exc,'reason',None)
            disconnect=getattr(robot._mqtt,'_disconnect_reason',None) or ''
            category='session_collision' if 'taken over' in disconnect.lower() else 'authorization' if 'authoriz' in disconnect.lower() else 'connection_ended' if disconnect else None
            record(plan_id,'dispatch_uncertain',{**dispatch_data,'action':action,'may_have_executed':True,'outcome':'unknown',
                'transport_error_type':type(exc).__name__,'transport_reason':getattr(reason,'name',None),
                'mqtt_connected':bool(getattr(robot._mqtt,'_connected',False)),'disconnect_category':category})
            raise
    robot._mqtt._publish=guarded_publish
    try:
        before_shadow=(await robot.get_named_shadow('ro-currentstate')).payload
        reported=before_shadow['state']['reported']
        before=reported['cleanMissionStatus']
        if before.get('missionId')!=plan['mission_id'] or before.get('phase')!=plan['phase']:
            raise ControlError('RobotStateChangedSinceApproval')
        if (plan.get('owner_navigation_retry') or plan.get('owner_brush_retry')) and before.get('error')!=plan.get('expected_error'):
            raise ControlError('FaultChangedSinceOwnerRequest')
        if plan.get('owner_brush_retry'):
            reported=await fresh_brush_recovery(plan,robot,before_shadow)
            before=reported['cleanMissionStatus']
        if action=='soft_reboot':
            import soft_reboot
            return await soft_reboot.execute(plan,robot,before_shadow)
        if plan.get('automatic_recovery'):
            reported=await fresh_recovery(plan,robot,before_shadow)
        elif action in ('resume','pause') and before.get('phase')=='stop':
            reported=await fresh_pause_state(robot,before_shadow)
        import recovery_context
        retained_stop_pause=before.get('phase')=='stop' and recovery_context.is_paused(reported)
        if plan.get('no_op') and action=='pause':
            if not recovery_context.is_paused(reported):raise ControlError('PausedNoOpStateChanged')
            import recovery_context
            data={'action':action,'broker_acknowledged':False,'robot_action_confirmed':True,
                'may_have_executed':False,'outcome':'already_paused_noop','no_op':True,'command_sent':False,
                'before_phase':before.get('phase'),'after_phase':before.get('phase'),'robot_error':before.get('error'),
                'mission_id':before.get('missionId'),'same_mission':True,
                'native_anchor':recovery_context.native_anchor(reported),
                'before_native_anchor':recovery_context.native_anchor(reported),
                'command_time':None,'pause_echo_verified':False,
                'hold_generation':plan.get('dispatch_hold_generation'),'mqtt_owner':'observer_session'}
            record(plan_id,'complete',data)
            return result(plan_id)
        preconditions(action,before,plan.get('owner_navigation_retry',False),plan.get('owner_brush_retry',False),
            retained_stop_pause=retained_stop_pause)
        if action=='resume':
            check_resume_mission(plan,before)
            check_resume_owner_fence(plan,c.job(plan['mission_id']))
        if action in ('clean_room','clean_area'):
            if not any(m.get('p2map_id')==plan['map_id'] and m.get('p2mapv_id')==plan['map_version'] for m in reported.get('p2maps',[])):
                raise ControlError('ActiveMapChangedSinceApproval')
            ack=await robot.send_routine_command_via_cmd_topic(region_command(plan,blid))
        else:ack=await robot.send_simple_command(action)
        data={'action':action,'broker_acknowledged':bool(ack),'before_phase':before.get('phase'),
            'robot_action_confirmed':False,'may_have_executed':True,'outcome':'unconfirmed',
            'requested_room':plan.get('room'),'requested_region_id':plan.get('region_id'),
            'requested_map_id':plan.get('map_id'),'target_echo_verified':False,'mqtt_owner':'observer_session',
            'hold_generation':plan.get('dispatch_hold_generation'),'command_time':command_time}
        if action=='pause':data['pause_echo_verified']=False
        import recovery_context
        data['native_anchor']=recovery_context.native_anchor(reported)
        data['before_native_anchor']=recovery_context.native_anchor(reported)
        brush_before_evidence=dict(reported.get('_recovery',{}))
        if plan.get('owner_brush_retry'):
            data.update(fault_cleared_observed=False,same_mission_resume_verified=False,
                fresh_resume_report_verified=False,before_shadow_version=brush_before_evidence.get('shadow_version'),
                before_phase_reported_at=brush_before_evidence.get('phase_reported_at'),
                before_error_reported_at=brush_before_evidence.get('error_reported_at'))
        if action=='clean_area':data.update(requested_area_points=plan['area_points'],boundary_verified=False)
        prior=result(plan_id) or {}
        for key in ('transport_error_type','transport_reason','mqtt_connected','disconnect_category'):
            if key in prior:data[key]=prior[key]
        record(plan_id,'broker_acknowledged' if ack else 'delivery_unconfirmed',data)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            await asyncio.sleep(2)
            after_shadow=(await robot.get_named_shadow('ro-currentstate')).payload
            after_reported=after_shadow['state']['reported']
            after=after_reported['cleanMissionStatus']
            import recovery_context
            pause_echo_required=action=='pause' and after.get('phase')=='stop'
            brush_resume_verified=False
            if plan.get('owner_brush_retry'):
                after_reported,brush_resume_verified=await brush_resume_observation(
                    plan,robot,after_shadow,command_time,brush_before_evidence)
                evidence=after_reported['_recovery']
                data.update(fresh_resume_report_verified=brush_resume_verified,
                    after_shadow_version=evidence.get('shadow_version'),
                    after_phase_reported_at=evidence.get('phase_reported_at'),
                    after_error_reported_at=evidence.get('error_reported_at'))
            if pause_echo_required:
                after_reported=await fresh_pause_state(robot,after_shadow)
                evidence=after_reported.get('_recovery',{})
                last=evidence.get('last_command',{})
                phase_at=evidence.get('phase_reported_at')
                data['pause_echo_verified']=bool(recovery_context.is_paused(after_reported)
                    and last.get('command')=='pause' and last.get('initiator')=='localApp'
                    and type(command_time) is int and last.get('time')==command_time
                    and type(phase_at) in (int,float) and phase_at>=command_time
                    and recovery_context.native_anchor(after_reported)==data['before_native_anchor'])
            data.update(after_phase=after.get('phase'),robot_error=after.get('error'),
                same_mission=before.get('missionId')==after.get('missionId'),mission_id=after.get('missionId'),
                native_anchor=recovery_context.native_anchor(after_reported))
            if (observed(action,before,after) and (not pause_echo_required or data['pause_echo_verified'])
                    and (not plan.get('owner_brush_retry') or brush_resume_verified)
                    and (not plan.get('owner_navigation_retry') or data['same_mission'])):
                data.update(robot_action_confirmed=True,outcome='state_transition_observed');break
            if plan.get('owner_navigation_retry') and not data['same_mission']:break
            if after.get('error') and not (plan.get('owner_navigation_retry') and after.get('error')==1010 or plan.get('owner_brush_retry') and after.get('error')==2) or action=='find':break
        if plan.get('owner_navigation_retry'):
            data.update(recovery_kind='owner_cleared_path_dock_retry',fault_clear_command_sent=False,
                fault_cleared_observed=bool(data.get('robot_error')==0),remote_fault_handling_verified=bool(data.get('robot_action_confirmed') and data.get('robot_error')==0))
        if plan.get('owner_brush_retry'):
            verified=bool(data.get('robot_action_confirmed') and data.get('fresh_resume_report_verified'))
            data.update(recovery_kind='owner_inspected_brush_resume',fault_clear_command_sent=False,
                fault_cleared_observed=verified,same_mission_resume_verified=verified)
        record(plan_id,'complete',data)
        answer=result(plan_id);track_result(plan,answer)
        return answer
    finally:robot._mqtt._publish=previous_publish

def load_approved(plan_id):
    plan=get_plan(plan_id)
    if plan['action']=='clean_area' and c.policy().get('spot_cleaning_blocked'):
        raise ControlError('SpotTargetingFailedRequiresCommandFix')
    if plan.get('target_sku')!=TARGET or plan.get('fingerprint')!=FINGERPRINT:raise ControlError('ExactPlanTargetRequired')
    approval=consume(plan['action'],plan)
    if plan.get('owner_navigation_retry') and not str(approval.get('explicit_owner_request','')).startswith('owner-requested cleared-path docking retry:'):
        raise ControlError('ExplicitOwnerClearedPathRetryRequired')
    if plan.get('owner_brush_retry') and not str(approval.get('explicit_owner_request','')).startswith('owner-requested inspected-brush resume retry:'):raise ControlError('ExplicitInspectedBrushRequestRequired')
    plan['approval_expires_at']=approval['expires_at']
    plan['automatic_recovery']=str(approval.get('explicit_owner_request','')).startswith('owner-granted same-mission recovery:')
    if plan['automatic_recovery']:recovery_job(plan)
    if plan['action']=='soft_reboot' and (not plan.get('one_use_soft_reboot') or not str(approval.get('explicit_owner_request','')).startswith('owner-requested one-use soft reboot:')):
        raise ControlError('ExplicitOneUseSoftRebootRequired')
    if plan['action'] in ('pause','stop','dock','soft_reboot'):hold_mission(plan['mission_id'],plan['action'])
    saved=c.job(plan.get('mission_id'))
    plan['dispatch_hold_generation']=(saved or {}).get('hold_generation',0)
    record(plan_id,'validating',{'action':plan['action'],'may_have_executed':False,
        'hold_generation':plan['dispatch_hold_generation']})
    return plan

def failed(plan,exc):
    prior=result(plan['plan_id']) or {}
    data={k:v for k,v in prior.items() if k not in ('stage','retry_permitted','plan_id')}
    data.update(action=plan['action'],error_code=str(exc) if isinstance(exc,ControlError) else type(exc).__name__,
        may_have_executed=prior.get('may_have_executed',False),outcome='unknown' if prior.get('may_have_executed') else 'refused')
    record(plan['plan_id'],'failed',data)
    return result(plan['plan_id'])

async def execute_on_service(plan_id,robot,blid,raw_publish):
    if hashlib.sha256(blid.encode()).hexdigest()[:16]!=FINGERPRINT:raise ControlError('ExactServiceDeviceRequired')
    with exclusive_writer():
        previous=result(plan_id)
        if previous:return {**previous,'duplicate_request':True}
        plan=load_approved(plan_id)
        try:
            async with asyncio.timeout(180 if plan['action']=='soft_reboot' else 130 if plan['action']=='drive_garbage' else 100):return await run_on_robot(plan,robot,blid,raw_publish)
        except (Exception,asyncio.CancelledError) as exc:
            answer=failed(plan,exc)
            if isinstance(exc,asyncio.CancelledError):raise
            return answer

async def command(plan):
    """Isolated transport harness for offline tests. Live CLI uses the observer actor."""
    import aiohttp
    from roombapy_prime.auth import login
    from roombapy_prime.prime_factory import PrimeFactory
    email,password,country=credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        auth=await login(session,email,password,country)
        matches=[b for b,r in auth.robots.items() if r.sku==TARGET and hashlib.sha256(b.encode()).hexdigest()[:16]==FINGERPRINT]
        if len(matches)!=1:raise ControlError('ExactDeviceNotFound')
        robot=await PrimeFactory.create_prime_robot(session,email,password,country,blid=matches[0],login_result=auth,auto_refresh=False)
        try:
            await robot.connect()
            return await run_on_robot(plan,robot,matches[0],robot._mqtt._publish)
        finally:await robot.disconnect()

def execute(plan_id):
    actor=c.ROOT/'observer-data/command.sock'
    if actor.exists():
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            action=c.get_plan(plan_id,allow_expired=True)['action']
            connection.settimeout(200 if action=='soft_reboot' else 150 if action=='drive_garbage' else 120);connection.connect(str(actor))
            connection.sendall((json.dumps({'plan_id':plan_id})+'\n').encode())
            data=bytearray()
            while b'\n' not in data:
                chunk=connection.recv(4096)
                if not chunk:raise ControlError('CommandActorResponseMissingCheckJournal')
                data.extend(chunk)
                if len(data)>65536:raise ControlError('CommandActorResponseTooLarge')
            answer=json.loads(data.split(b'\n',1)[0])
            if answer.get('error_code') and not answer.get('plan_id'):raise ControlError(answer['error_code'])
            return answer
    receipt=c.ROOT/'activation-result.json'
    if receipt.exists() and json.loads(receipt.read_text()).get('launch_agent_created'):
        raise ControlError('ObserverCommandSocketUnavailableNoSecondMqttSession')
    # Tests use an isolated root without an activated service.
    with exclusive_writer():
        previous=result(plan_id)
        if previous:return {**previous,'duplicate_request':True}
        plan=load_approved(plan_id)
        try:return asyncio.run(command(plan))
        except Exception as exc:return failed(plan,exc)

if __name__=='__main__':
    logging.disable(logging.CRITICAL)
    try:
        if len(sys.argv)!=2:raise ControlError('PlanIdRequired')
        answer=execute(sys.argv[1]);print(json.dumps(answer));sys.exit(1 if answer.get('stage')=='failed' else 0)
    except Exception as exc:
        print(json.dumps({'error_code':str(exc) if isinstance(exc,(ControlError,PermissionError)) else type(exc).__name__,'may_have_executed':False}));sys.exit(1)
