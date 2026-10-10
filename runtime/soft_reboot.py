"""One owner-requested app soft restart. No persistent reboot grant or motion chain.

The vendor app MQTT reset command reboots. REST reset/wipe is never used here.
Ordinary chat actions and the automatic recovery worker cannot request this action.
"""
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import control_layer as c
from recovery_diagnostics import filtered

OWNER_PREFIX='owner-requested one-use soft reboot:'

def validate(reported,plan=None):
    m=reported.get('cleanMissionStatus',{})
    if (m.get('phase')!='stuck' or m.get('error')!=2 or m.get('cycle')!='none' or
        not m.get('missionId') or m.get('notReady')!=0 or m.get('condNotReady')!=[] or
        type(reported.get('batPct')) is not int or reported['batPct']<25):
        raise c.ControlError('SoftRebootRequiresStoppedReadyBrushFault')
    if plan and any(m.get(k)!=plan['before_mission'].get(k) for k in ('missionId','phase','cycle','error','nMssn','mssnStrtTm','notReady','condNotReady')):
        raise c.ControlError('FaultChangedSinceSoftRebootRequest')
    return m

def prepare(snapshot):
    if snapshot.get('target_sku')!=c.TARGET or snapshot.get('state_stale') or snapshot.get('connection',{}).get('data',{}).get('status')!='connected':
        raise c.ControlError('FreshExactDeviceStateRequired')
    reported=snapshot.get('state',{}).get('data',{})
    m=validate(reported);now=time.time()
    plan={'plan_id':str(uuid.uuid4()),'action':'soft_reboot','one_use_soft_reboot':True,
        'target_sku':c.TARGET,'fingerprint':c.FINGERPRINT,'mission_id':m['missionId'],
        'phase':m['phase'],'created_at':now,'expires_at':now+600,'before_mission':m,
        'effect':'Restart Roomba through the app soft reboot command; no follow-up movement; onboard planner continuity unverified'}
    plan['intent_hash']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    with contextlib.closing(c.database()) as db,db:db.execute('INSERT INTO plans VALUES(?,?,?)',(plan['plan_id'],now,json.dumps(plan)))
    return plan

def preserve(plan,snapshot):
    folder=c.ROOT/'observer-data'/('soft-reboot-preserved-'+plan['plan_id'])
    folder.mkdir(mode=0o700)
    for name,data in (('status.json',snapshot),('trajectory.json',c.read_local('/api/trajectory'))):
        path=folder/name
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:json.dump(data,output);output.flush();os.fsync(output.fileno())
    for name in ('observer.sqlite3','control.sqlite3'):
        target=folder/name
        with contextlib.closing(sqlite3.connect('file:'+str(c.ROOT/'observer-data'/name)+'?mode=ro',uri=True)) as source,contextlib.closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
        target.chmod(0o600)
    return str(folder)

def run(request_id,human_request_reference):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',request_id):raise c.ControlError('StableUniqueRequestIdRequired')
    if not isinstance(human_request_reference,str) or not human_request_reference.strip():raise PermissionError('DirectHumanSoftRebootRequestRequired')
    intent=json.dumps({'action':'soft_reboot','owner_reference':human_request_reference},sort_keys=True)
    fd=os.open(c.ROOT/'observer-data/.chat-command.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise c.ControlError('AnotherChatCommandInProgress') from None
        with contextlib.closing(c.database()) as db:
            prior=db.execute('SELECT plan_id,intent FROM requests WHERE request_id=?',(request_id,)).fetchone()
            db.execute('CREATE TABLE IF NOT EXISTS soft_reboot_claims(mission_id TEXT PRIMARY KEY,request_id TEXT UNIQUE,at REAL NOT NULL,plan_id TEXT NOT NULL)');db.commit()
            if prior:
                if prior[1]!=intent:raise c.ControlError('RequestIdIntentMismatch')
                return c.result(prior[0]) or {'plan_id':prior[0],'outcome':'pending_or_interrupted','retry_permitted':False}
            if db.execute('SELECT 1 FROM soft_reboot_claims WHERE at>?',(time.time()-21600,)).fetchone():raise c.ControlError('SoftRebootAlreadyClaimedInSixHours')
        snapshot=c.read_local('/api/status');plan=prepare(snapshot)
        backup=preserve(plan,snapshot)
        with contextlib.closing(c.database()) as db,db:
            if db.execute('SELECT 1 FROM soft_reboot_claims WHERE mission_id=?',(plan['mission_id'],)).fetchone():raise c.ControlError('SoftRebootAlreadyClaimedForMission')
            db.execute('INSERT INTO soft_reboot_claims VALUES(?,?,?,?)',(plan['mission_id'],request_id,time.time(),plan['plan_id']))
            db.execute('INSERT INTO requests VALUES(?,?,?)',(request_id,plan['plan_id'],intent))
        from write_approval import grant_explicit_request
        grant_explicit_request(plan,OWNER_PREFIX+human_request_reference)
        answer=c.launch_writer(plan['plan_id'])
        return {**answer,'preserved_data':backup,'automatic_followup_command_sent':False}
    finally:os.close(fd)

async def execute(plan,robot,before_shadow):
    validate(before_shadow.get('state',{}).get('reported',{}),plan)
    before=filtered('ro-currentstate',before_shadow)
    sent_at=time.time()
    ack=await robot.send_simple_command('reset',initiator='rmtApp')
    data={'action':'soft_reboot','wire_command':'reset','broker_acknowledged':bool(ack),
        'outcome':'unconfirmed','may_have_executed':True,'robot_action_confirmed':False,
        'fault_cleared_observed':False,'same_mission_resume_verified':False,
        'automatic_followup_command_sent':False,'before':before,'mqtt_owner':'observer_session',
        'samples':[]}
    c.record(plan['plan_id'],'broker_acknowledged' if ack else 'delivery_unconfirmed',data)
    deadline=time.monotonic()+140
    disconnected=False
    echo_observed=False
    while time.monotonic()<deadline:
        await asyncio.sleep(5)
        sample={'at':time.time()}
        for name in ('rw-constatus','ro-currentstate','rw-software'):
            try:
                async with asyncio.timeout(8):response=await robot.get_named_shadow(name)
                sample[name]=filtered(name,response.payload)
            except Exception as exc:sample[name]={'error_type':type(exc).__name__}
        connected=sample['rw-constatus'].get('connected')
        connected_v2=sample['rw-constatus'].get('connectedv2')
        if connected is False or connected_v2 is False:disconnected=True
        current=sample['ro-currentstate'];m=current.get('mission',{})
        error_stamp=current.get('error_reported_at')
        # Shadow GET time is broker time, not proof of a fresh robot report.
        fresh_zero=m.get('error')==0 and type(error_stamp) in (int,float) and error_stamp>=int(sent_at)-1
        command=sample['rw-software'].get('last_command',{})
        echo=command.get('command')=='reset' and type(command.get('time')) in (int,float) and command['time']>=int(sent_at)-1
        echo_observed=echo_observed or echo
        reconnected=disconnected and (connected is True or connected_v2 is True) and connected is not False and connected_v2 is not False
        verified=fresh_zero and reconnected and echo_observed
        data['samples'].append(sample)
        data.update(after_phase=m.get('phase'),robot_error=m.get('error'),mission_id=m.get('missionId'),
            same_mission=m.get('missionId')==plan['mission_id'],device_disconnect_observed=disconnected,
            reset_command_echo_observed=echo_observed,fault_cleared_observed=fresh_zero,
            device_reconnected_observed=reconnected,unexpected_motion_observed=m.get('phase')=='run',
            robot_action_confirmed=bool(verified),outcome='restart_and_fault_clearance_observed' if verified else 'unconfirmed')
        c.record(plan['plan_id'],'observing_restart',data)
        if verified:break
    c.record(plan['plan_id'],'complete',data)
    return c.result(plan['plan_id'])
