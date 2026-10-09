"""Persistent interruption detection and bounded same-mission recovery decisions."""
import time
import control_layer as c

def recovery_allowed(current,job,settings,now):
    mission=current.get('cleanMissionStatus',{})
    if not job or mission.get('missionId')!=job.get('mission_id'):return False,'MissionIdentityChanged'
    if not settings.get('automatic_resume') or not job.get('automatic_resume') or job.get('held') or job.get('finished'):
        return False,'RecoveryDisabledOrHeld'
    if mission.get('phase') not in ('pause','paused') or mission.get('error')!=0 or mission.get('notReady')!=0 or mission.get('condNotReady'):
        return False,'FaultOrNotReadyOrNotPaused'
    # App/manual/unknown provenance is escalated instead of overriding an external pause.
    if mission.get('initiator')!='localApp':return False,'PauseOriginUnverified'
    if now-job.get('pause_since',now)<45:return False,'PauseDebounce'
    if job.get('resume_count',0)>=2 or now-job['started_at']>21600:return False,'RecoveryBudgetExhausted'
    if now-job.get('last_resume_at',0)<180:return False,'RecoveryCooldown'
    if current.get('batPct',0)<25:return False,'BatteryTooLow'
    if job.get('map_id') and not any(m.get('p2map_id')==job['map_id'] for m in current.get('p2maps',[])):
        return False,'MissionMapChanged'
    if job.get('map_version') and not any(m.get('p2map_id')==job['map_id'] and m.get('p2mapv_id')==job['map_version'] for m in current.get('p2maps',[])):
        return False,'MissionMapVersionChanged'
    return True,'SameMissionPausedAndReady'

def evaluate(current,now=None):
    now=time.time() if now is None else now
    mission=current.get('cleanMissionStatus',{});mission_id=mission.get('missionId')
    if not mission_id:return None
    job=c.job(mission_id)
    if not job and mission.get('phase') in ('run','pause','paused'):
        job={'mission_id':mission_id,'room':None,'map_id':None,'started_at':now,'last_phase':None,
             'seen_running':False,'held':False,'automatic_resume':False,'resume_count':0,'finished':False,'observed_only':True}
    if not job or job.get('finished'):return None
    settings=c.policy()
    if settings.get('owner_approved') and settings.get('authorized_existing_mission')==mission_id and job.get('plan_id') and not job.get('observed_only'):
        receipt=c.result(job['plan_id']) or {}
        if receipt.get('robot_action_confirmed') and receipt.get('mission_id')==mission_id:
            job['automatic_resume']=True
    phase=mission.get('phase');previous=job.get('last_phase')
    job['last_phase']=phase
    if phase=='run':
        job.update(seen_running=True,pause_since=None)
    elif mission.get('error') or mission.get('notReady') or mission.get('condNotReady'):
        c.notify(f'fault:{mission_id}:{mission.get("error")}:{mission.get("notReady")}',
            {'kind':'fault','mission_id':mission_id,'room':job.get('room'),'error':mission.get('error'),
             'message':f"{job.get('room') or 'Mission'} stopped/not ready; owner attention required. No automatic resume."})
    elif phase in ('pause','paused'):
        if not job.get('pause_since'):
            job['pause_since']=now;job['pause_incident']=str(int(now*1000))
            c.notify('paused:'+mission_id+':'+job['pause_incident'],{'kind':'paused','mission_id':mission_id,'room':job.get('room'),
                'held':job.get('held',False),'message':f"{job.get('room') or 'Mission'} paused; saved mission retained."})
    elif mission.get('cycle')=='none' and phase in ('charge','idle','stop','stopped'):
        job['finished']=True
        c.notify('ended:'+mission_id,{'kind':'mission_ended','mission_id':mission_id,'room':job.get('room'),
            'message':f"{job.get('room') or 'Mission'} ended; no restart or planner replay.",
            'completion_verified':False,'area_sqft':mission.get('sqft')})
    elif previous=='run' and phase not in ('run','hmPostMsn','hmUsrDock','charge','recharge','padWash'):
        c.notify(f'interrupted:{mission_id}:{phase}',{'kind':'interrupted','mission_id':mission_id,'phase':phase,
            'message':f"{job.get('room') or 'Mission'} interrupted ({phase}); no blind restart."})
    allowed,reason=recovery_allowed(current,job,settings,now)
    job['recovery_decision']=reason
    request=None
    if allowed:
        # Claim the incident durably before any attempt; restart cannot cause a duplicate resume.
        incident=job.get('pause_incident')
        if incident and job.get('claimed_incident')!=incident:
            job.update(claimed_incident=incident,resume_count=job.get('resume_count',0)+1,last_resume_at=now)
            request={'mission_id':mission_id,'room':job.get('room'),'incident':incident}
    c.save_job(mission_id,job)
    return request

def recover(request):
    """Reconcile fresh observer and cloud state, issue only a resume; never a start."""
    from write_approval import grant_explicit_request
    from supervised_command import execute
    snapshot=c.read_local('/api/status')
    current=snapshot.get('state',{}).get('data',{})
    if snapshot.get('state_stale') or current.get('cleanMissionStatus',{}).get('missionId')!=request['mission_id']:
        raise c.ControlError('FreshSameMissionRequiredForRecovery')
    job=c.job(request['mission_id'])
    allowed,reason=recovery_allowed(current,{**job,'last_resume_at':0,'resume_count':job.get('resume_count',0)-1},c.policy(),time.time())
    if not allowed:raise c.ControlError(reason)
    plan=c.prepare('resume',snapshot)
    grant_explicit_request(plan,'owner-granted same-mission recovery:'+request['mission_id']+':'+request['incident'])
    result=execute(plan['plan_id'])
    c.notify('recovery:'+plan['plan_id'],{'kind':'recovery_result','mission_id':request['mission_id'],
        'plan_id':plan['plan_id'],'outcome':result.get('outcome'),
        'message':'Resume '+('state transition observed' if result.get('robot_action_confirmed') else 'unconfirmed; no retry')})
    return result
