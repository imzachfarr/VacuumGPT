"""Fixed, filtered recovery reads through Roomba's existing authenticated session."""
import asyncio
import hashlib
import json
import math
import re
import time

SHADOWS=('ro-currentstate','rw-software','rw-constatus','ro-stats')
FAULT_COUNTERS=('nMBStll','nLBStll','nRBStll','nDStll','nWStll','nOvertemps','nPicks','nStuck')
def stamp(payload,path):
    current=payload.get('metadata',{}).get('reported',{})
    for key in path:
        current=current.get(key,{}) if isinstance(current,dict) else {}
    value=current.get('timestamp') if isinstance(current,dict) else None
    return value if type(value) in (int,float) else None

def command(value):
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError:return {'format':'unparsed','fingerprint':hashlib.sha256(value.encode()).hexdigest()[:16]}
    if not isinstance(value,dict):return {'format':type(value).__name__}
    answer={}
    for key in ('command','initiator'):
        if isinstance(value.get(key),str) and len(value[key])<=80:answer[key]=value[key]
    if type(value.get('time')) in (int,float):answer['time']=value['time']
    return answer

def filtered(name,payload):
    reported=payload.get('state',{}).get('reported',{})
    answer={'shadow_version':payload.get('version'),'shadow_timestamp':payload.get('timestamp')}
    if name=='ro-currentstate':
        m=reported.get('cleanMissionStatus',{})
        keys=('phase','cycle','error','notReady','condNotReady','missionId','nMssn','mssnStrtTm','initiator','sqft','operatingMode')
        answer.update(mission={k:m[k] for k in keys if k in m},battery=reported.get('batPct'),
            error_reported_at=stamp(payload,('cleanMissionStatus','error')),
            phase_reported_at=stamp(payload,('cleanMissionStatus','phase')))
    elif name=='rw-software':
        answer.update(last_command=command(reported.get('lastCommand')),last_command_reported_at=stamp(payload,('lastCommand',)),software_version=reported.get('softwareVer'))
    elif name=='ro-stats':
        # An unmapped vendor fault can appear here. Recognized 519/567 both
        # become cloud error2; this field cannot distinguish those two.
        raw=reported.get('unprocessedError')
        match=re.fullmatch(r'picea unknown fault code:(\d{1,8})',raw) if isinstance(raw,str) else None
        counters=reported.get('bbrun')
        counters=counters if isinstance(counters,dict) else {}
        answer.update(unmapped_fault_field_present='unprocessedError' in reported,
            unmapped_fault_present=raw not in (None,''),
            unmapped_fault_code=int(match[1]) if match else None,
            unmapped_fault_reported_at=stamp(payload,('unprocessedError',)),
            declared_fault_counters={k:v for k,v in counters.items() if k in FAULT_COUNTERS and type(v) is int and v>=0},
            counter_semantics_verified=False)
    else:answer.update({k:reported[k] for k in ('connected','connectedv2') if type(reported.get(k)) is bool})
    return answer

async def read(robot):
    answer={'read_only':True,'mqtt_owner':'observer_session','read_started_at':time.time(),'shadows':{}}
    for name in SHADOWS:
        try:
            async with asyncio.timeout(10):response=await robot.get_named_shadow(name)
            answer['shadows'][name]=filtered(name,response.payload)
        except Exception as exc:answer['shadows'][name]={'error_type':type(exc).__name__}
    answer['read_completed_at']=time.time()
    return answer


def assessment(answer,now=None):
    """Describe reported recovery evidence; this never authorizes a command."""
    now=time.time() if now is None else now
    shadows=answer.get('shadows',{})
    stats=shadows.get('ro-stats',{})
    report_present=stats.get('unmapped_fault_present') is True
    code=stats.get('unmapped_fault_code')
    known_firmware=shadows.get('rw-software',{}).get('software_version')=='p25-105+8.4.0+I3.7.118'
    native_type=(3 if code>=2100 else 2 if code>=2000 else 1 if code>=500 else 0) if known_firmware and type(code) is int else None
    report_class=('pause_or_broken_clean_state' if known_firmware and code==2102 else
                  'unclassified_type3_state_report' if native_type==3 else 'unclassified_native_report' if report_present else 'none')
    current=shadows.get('ro-currentstate',{})
    mission=current.get('mission',{})
    import control_layer as c
    phase_at=current.get('phase_reported_at')
    error_at=current.get('error_reported_at')
    def age(stamp):
        return round(now-stamp,3) if type(stamp) in (int,float) and math.isfinite(stamp) else None
    phase_age,error_age=age(phase_at),age(error_at)
    reason='RobotNotInBrushFault'
    candidate=False
    if mission.get('error')==2:
        try:
            c.preconditions('resume',mission,owner_brush_retry=True)
            battery=current.get('battery')
            if type(battery) not in (int,float) or not math.isfinite(battery) or not 25<=battery<=100:
                raise c.ControlError('RecoveryBatteryUnverifiedOrLow')
            if shadows.get('rw-constatus',{}).get('connected') is not True:
                raise c.ControlError('DeviceConnectionUnverified')
            if phase_age is None or error_age is None or not -5<=phase_age<=120 or not -5<=error_age<=120:
                raise c.ControlError('RobotFaultTelemetryStale')
            candidate=True;reason='RetainedCleanCycleInspectedResumeCandidate'
        except c.ControlError as exc:reason=str(exc)
    context=('ended_or_retention_unverified' if mission.get('cycle')=='none' else
             'clean_cycle_reported' if mission.get('cycle')=='clean' else 'unverified')
    running=mission.get('phase')=='run' and type(mission.get('error')) is int and mission['error']==0
    native_clear=stats.get('unmapped_fault_field_present') is True and stats.get('unmapped_fault_present') is False
    robot_report_fresh=phase_age is not None and error_age is not None and -5<=phase_age<=120 and -5<=error_age<=120
    ready=type(mission.get('notReady')) is int and mission['notReady']==0 and mission.get('condNotReady')==[]
    if candidate and report_present:
        candidate=False;reason='NativeReportNeedsInterpretationBeforeBrushRetry'
    return {'reported_mission_context':context,'running_fault_free':running and native_clear and robot_report_fresh and ready and shadows.get('rw-constatus',{}).get('connected') is True,
        'running_without_standard_error':running,
        'native_report':{'present':report_present,'code':code,'type':native_type,'classification':report_class,
            'reported_at':stats.get('unmapped_fault_reported_at'),'age_seconds':age(stats.get('unmapped_fault_reported_at')),
            'code2102_is_main_brush_error2':False,'cleared_or_active_state_verified':native_clear,
            'field_exposed':stats.get('unmapped_fault_field_present') is True},
        'standard_inspected_resume_candidate':candidate,'candidate_reason':reason,
        'physical_inspection_required_for_retry':True,'claim_budget_checked':False,
        'robot_phase_age_seconds':phase_age,'robot_error_age_seconds':error_age,
        'fresh_shadow_read_is_fresh_robot_report':False,
        'fault_override_supported':False,'onboard_motor_protection_preserved':True,
        'unfinished_only_fault_recovery_verified':False,
        'brush_current_available':False,'brush_speed_available':False,
        'recognized_native_brush_fault_distinguishable':False,
        'next_step':('Respect current docking; no cleaning or resume command needed' if mission.get('cycle')=='dock' else
            ('Interpret native 2102 with current mission/map/origin; it is a separate pause/broken state' if report_class=='pause_or_broken_clean_state' else 'Interpret the unclassified native report with current mission/map/origin; do not assume a brush fault') if report_present else
            'Observe current mission; no recovery command needed' if running else
            'After inspection, one bounded standard resume can be considered; claim budget and final state must pass' if candidate else
            'Do not resume an ended cycle as unfinished cleaning; a new clean requires an explicit new target' if context=='ended_or_retention_unverified' else
            'Resolve the candidate_reason before any retry')}


def read_via_actor():
    """Fixed read-only request; never creates a second MQTT session."""
    import socket
    import control_layer as c
    status=c.read_local('/api/status')
    if status.get('target_sku')!=c.TARGET or status.get('connection',{}).get('data',{}).get('status')!='connected':
        raise c.ControlError('ExactObserverConnectionRequired')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(50);connection.connect(str(c.ROOT/'observer-data/command.sock'))
        connection.sendall(b'{"diagnostic":"fault_read"}\n')
        data=bytearray()
        while b'\n' not in data:
            chunk=connection.recv(8192)
            if not chunk:raise c.ControlError('DiagnosticResponseMissing')
            data.extend(chunk)
            if len(data)>65536:raise c.ControlError('DiagnosticResponseTooLarge')
    answer=json.loads(data.split(b'\n',1)[0])
    if answer.get('error_code'):raise c.ControlError(answer['error_code'])
    return {'target_sku':c.TARGET,'fingerprint':c.FINGERPRINT,**answer,'recovery_assessment':assessment(answer),
        'automatic_fault_recovery_enabled':False,'mission_continuity_after_reboot_verified':False}
