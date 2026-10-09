"""Intent-bound writes through one MQTT session; durable at-most-once dispatch."""
import asyncio
import hashlib
import json
import logging
import socket
import sys
import time
from roomba_observer import TARGET, FINGERPRINT, credentials
import control_layer as c
from write_approval import consume
from control_layer import ControlError, get_plan, result, record, exclusive_writer, preconditions, payload_matches, region_command, track_result, hold_mission

def observed(action,before,after):
    if action=='resume' and before.get('missionId')!=after.get('missionId'):return False
    expected={'pause':('pause','paused'),'resume':('run',),'start':('run',),'clean_room':('run',),'clean_area':('run',),
              'dock':('hmPostMsn','hmUsrDock','dock','charge'),'stop':('stop','stopped','charge','idle')}
    phase=after.get('phase')
    return bool(action in expected and phase in expected[action] and
        (phase!=before.get('phase') or action in ('start','clean_room','clean_area') and before.get('missionId')!=after.get('missionId')) and not after.get('error'))

async def run_on_robot(plan,robot,blid,raw_publish):
    """Use the already authenticated exact robot; other observer tasks remain read-only."""
    if plan['action']=='drive_garbage':
        from garbage_navigation import run
        return await run(plan,robot,blid,raw_publish)
    from roombapy_prime.mqtt_client import _shadow_base
    action=plan['action'];plan_id=plan['plan_id']
    command_topic=robot._mqtt.cmd_topic(robot._irbt_topic_prefix)
    allowed={_shadow_base(blid,name)+'/get' for name in (None,'ro-currentstate')}
    previous_publish=robot._mqtt._publish
    command_task=asyncio.current_task()
    sent=False
    async def guarded_publish(topic,payload,timeout=5.0):
        nonlocal sent
        if topic in allowed and payload in ('',b''):return await raw_publish(topic,payload,timeout)
        body=json.loads(payload)
        if asyncio.current_task() is not command_task or topic!=command_topic or sent or not payload_matches(plan,body,blid):
            raise ControlError('UnapprovedPublishBlocked')
        if time.time()>plan['approval_expires_at']:raise ControlError('ApprovalExpiredBeforeDispatch')
        if plan.get('automatic_recovery'):
            saved=c.job(plan['mission_id'])
            if not c.policy().get('automatic_resume') or not saved or saved.get('held') or saved.get('finished'):
                raise ControlError('RecoveryPermissionChangedBeforeDispatch')
        sent=True
        record(plan_id,'dispatching',{'action':action,'room':plan.get('room'),'region_id':plan.get('region_id'),
            'map_id':plan.get('map_id'),'outcome':'unknown','may_have_executed':True,'mqtt_owner':'observer_session'})
        try:return await raw_publish(topic,payload,timeout)
        except Exception as exc:
            reason=getattr(exc,'reason',None)
            disconnect=getattr(robot._mqtt,'_disconnect_reason',None) or ''
            category='session_collision' if 'taken over' in disconnect.lower() else 'authorization' if 'authoriz' in disconnect.lower() else 'connection_ended' if disconnect else None
            record(plan_id,'dispatch_uncertain',{'action':action,'may_have_executed':True,'outcome':'unknown',
                'transport_error_type':type(exc).__name__,'transport_reason':getattr(reason,'name',None),
                'mqtt_connected':bool(getattr(robot._mqtt,'_connected',False)),'disconnect_category':category})
            raise
    robot._mqtt._publish=guarded_publish
    try:
        reported=(await robot.get_named_shadow('ro-currentstate')).payload['state']['reported']
        before=reported['cleanMissionStatus']
        if before.get('missionId')!=plan['mission_id'] or before.get('phase')!=plan['phase']:
            raise ControlError('RobotStateChangedSinceApproval')
        if plan.get('owner_navigation_retry') and before.get('error')!=plan.get('expected_error'):
            raise ControlError('FaultChangedSinceOwnerRequest')
        preconditions(action,before,plan.get('owner_navigation_retry',False))
        if action in ('clean_room','clean_area'):
            if not any(m.get('p2map_id')==plan['map_id'] and m.get('p2mapv_id')==plan['map_version'] for m in reported.get('p2maps',[])):
                raise ControlError('ActiveMapChangedSinceApproval')
            ack=await robot.send_routine_command_via_cmd_topic(region_command(plan,blid))
        else:ack=await robot.send_simple_command(action)
        data={'action':action,'broker_acknowledged':bool(ack),'before_phase':before.get('phase'),
            'robot_action_confirmed':False,'may_have_executed':True,'outcome':'unconfirmed',
            'requested_room':plan.get('room'),'requested_region_id':plan.get('region_id'),
            'requested_map_id':plan.get('map_id'),'target_echo_verified':False,'mqtt_owner':'observer_session'}
        if action=='clean_area':data.update(requested_area_points=plan['area_points'],boundary_verified=False)
        prior=result(plan_id) or {}
        for key in ('transport_error_type','transport_reason','mqtt_connected','disconnect_category'):
            if key in prior:data[key]=prior[key]
        record(plan_id,'broker_acknowledged' if ack else 'delivery_unconfirmed',data)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            await asyncio.sleep(2)
            after=(await robot.get_named_shadow('ro-currentstate')).payload['state']['reported']['cleanMissionStatus']
            data.update(after_phase=after.get('phase'),robot_error=after.get('error'),
                same_mission=before.get('missionId')==after.get('missionId'),mission_id=after.get('missionId'))
            if observed(action,before,after) and (not plan.get('owner_navigation_retry') or data['same_mission']):data.update(robot_action_confirmed=True,outcome='state_transition_observed');break
            if plan.get('owner_navigation_retry') and not data['same_mission']:break
            if after.get('error') and (not plan.get('owner_navigation_retry') or after.get('error')!=1010) or action=='find':break
        if plan.get('owner_navigation_retry'):
            data.update(recovery_kind='owner_cleared_path_dock_retry',fault_clear_command_sent=False,
                fault_cleared_observed=bool(data.get('robot_error')==0),remote_fault_handling_verified=bool(data.get('robot_action_confirmed') and data.get('robot_error')==0))
        record(plan_id,'complete',data)
        answer=result(plan_id);track_result(plan,answer)
        return answer
    finally:robot._mqtt._publish=previous_publish

def load_approved(plan_id):
    plan=get_plan(plan_id)
    if plan.get('target_sku')!=TARGET or plan.get('fingerprint')!=FINGERPRINT:raise ControlError('ExactPlanTargetRequired')
    approval=consume(plan['action'],plan)
    if plan.get('owner_navigation_retry') and not str(approval.get('explicit_owner_request','')).startswith('owner-requested cleared-path docking retry:'):
        raise ControlError('ExplicitOwnerClearedPathRetryRequired')
    plan['approval_expires_at']=approval['expires_at']
    plan['automatic_recovery']=str(approval.get('explicit_owner_request','')).startswith('owner-granted same-mission recovery:')
    if plan['action'] in ('pause','stop','dock'):hold_mission(plan['mission_id'],plan['action'])
    record(plan_id,'validating',{'action':plan['action'],'may_have_executed':False})
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
            async with asyncio.timeout(130 if plan['action']=='drive_garbage' else 100):return await run_on_robot(plan,robot,blid,raw_publish)
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
            connection.settimeout(150 if c.get_plan(plan_id,allow_expired=True)['action']=='drive_garbage' else 120);connection.connect(str(actor))
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
