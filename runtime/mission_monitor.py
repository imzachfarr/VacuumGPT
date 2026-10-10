"""Atomic interruption detection and durable, bounded native-mission recovery."""
import json
import time
import uuid

import control_layer as c
import recovery_store as store


def _validate(current, job, now):
    from recovery_context import validate
    return validate(current, job, now=now)


def recovery_allowed(current, job, settings, now, *, reserved=False):
    mission = current.get('cleanMissionStatus', {})
    if not job or mission.get('missionId') != job.get('mission_id'):
        return False, 'MissionIdentityChanged'
    if (not settings.get('automatic_resume') or not job.get('automatic_resume') or
            job.get('held') or job.get('finished')):
        return False, 'RecoveryDisabledOrHeld'
    if type(mission.get('error')) is not int:
        return False, 'IncompleteRobotState'
    if mission['error'] != 0:
        return False, 'ActiveRobotFault:' + str(mission['error'])
    if (type(mission.get('notReady')) is not int or mission['notReady'] != 0 or
            mission.get('condNotReady') != []):
        return False, 'RobotNotReady'
    if not mission.get('cycle'):
        return False, 'IncompleteRobotState'
    if mission['cycle'] == 'none':
        return False, 'MissionCycleEndedNoPlannerReplay'
    from recovery_context import is_paused
    if not is_paused(current):
        return False, 'MissionNotPaused'
    allowed, reason = _validate(current, job, now)
    if not allowed:
        return False, reason
    pause_since = job.get('pause_since')
    if type(pause_since) not in (int, float) or now - pause_since < 45:
        return False, 'PauseDebounce'
    started_at = job.get('started_at')
    if type(started_at) not in (int, float) or not 0 <= now - started_at <= 21600:
        return False, 'RecoveryBudgetExhausted'
    # reserve() already charged this exact durable attempt. The reserved path
    # cannot manufacture a new claim or bypass the incident/generation fence.
    if not reserved:
        if job.get('resume_count', 0) >= 2:
            return False, 'RecoveryBudgetExhausted'
        if now - job.get('last_resume_at', 0) < 180:
            return False, 'RecoveryCooldown'
    return True, 'SameMissionPausedAndReady'


def _cancel_queued(db, mission_id, now):
    db.execute("UPDATE recovery_outbox SET status='cancelled',updated=? WHERE mission_id=? AND status='queued'",
               (now, mission_id))


def evaluate(current, now=None):
    now = time.time() if now is None else now
    mission = current.get('cleanMissionStatus', {})
    mission_id = mission.get('missionId')
    if not isinstance(mission_id, str) or not mission_id:
        return None
    settings = c.policy()
    from recovery_context import is_paused
    has_fault = bool(mission.get('error') or mission.get('notReady') or mission.get('condNotReady'))
    explanation = {}
    if has_fault:
        from roombapy_prime.vendor_errors import vendor_error
        explanation = vendor_error(mission.get('error')) or {}

    def update(job, db):
        notices = []
        phase = mission.get('phase')
        if not job and (phase == 'run' or is_paused(current)):
            job = {'mission_id': mission_id, 'room': None, 'map_id': None,
                   'started_at': now, 'last_phase': None, 'seen_running': False,
                   'held': False, 'automatic_resume': False, 'resume_count': 0,
                   'finished': False, 'observed_only': True}
        if not job or job.get('finished'):
            return None, (None, notices)
        if (settings.get('owner_approved') and
                settings.get('authorized_existing_mission') == mission_id and
                job.get('plan_id') and not job.get('observed_only')):
            receipt_row = db.execute('SELECT result FROM attempts WHERE plan_id=?',
                                     (job['plan_id'],)).fetchone()
            receipt = json.loads(receipt_row[0]) if receipt_row else {}
            if receipt.get('robot_action_confirmed') and receipt.get('mission_id') == mission_id:
                job['automatic_resume'] = True
        previous = job.get('last_phase')
        job['last_phase'] = phase
        ready_pause = (is_paused(current) and
                       type(mission.get('error')) is int and mission['error'] == 0 and
                       type(mission.get('notReady')) is int and mission['notReady'] == 0 and
                       mission.get('condNotReady') == [])
        if not ready_pause:
            # Any intermediate fault/non-pause ends the incident. Returning to
            # paused begins a fresh fault-free debounce, never an old claim.
            job.update(pause_since=None, pause_incident=None)
            _cancel_queued(db, mission_id, now)
        if has_fault:
            title = explanation.get('title') or ('Robot not ready' if not mission.get('error') else 'Robot fault')
            notices.append((f'fault-detail:{mission_id}:{mission.get("error")}:{mission.get("notReady")}',
                {'kind': 'fault', 'mission_id': mission_id, 'room': job.get('room'),
                 'error': mission.get('error'), 'fault_title': title,
                 'vendor_guidance': explanation.get('content'), 'phase': phase,
                 'cycle': mission.get('cycle'), 'automatic_resume_blocked': True,
                 'message': f"{job.get('room') or 'Mission'}: {title} (error {mission.get('error')}). Automatic resume withheld; resolve the reported fault first."}))
        elif phase == 'run':
            job['seen_running'] = True
        elif ready_pause:
            if job.get('pause_since') is None or not job.get('pause_incident'):
                since = job.get('pause_since')
                job.update(pause_since=since if type(since) in (int, float) else now,
                           pause_incident=str(uuid.uuid4()))
                notices.append(('paused:' + mission_id + ':' + job['pause_incident'],
                    {'kind': 'paused', 'mission_id': mission_id, 'room': job.get('room'),
                     'held': job.get('held', False),
                     'message': f"{job.get('room') or 'Mission'} paused; state and requested targets saved."}))
        elif mission.get('cycle') == 'none' and phase in ('charge', 'idle', 'stop', 'stopped'):
            job['finished'] = True
            notices.append(('ended:' + mission_id,
                {'kind': 'mission_ended', 'mission_id': mission_id, 'room': job.get('room'),
                 'message': f"{job.get('room') or 'Mission'} ended; no restart or planner replay.",
                 'completion_verified': False, 'area_sqft': mission.get('sqft')}))
        elif previous == 'run' and phase not in ('run', 'hmPostMsn', 'hmUsrDock', 'charge', 'recharge', 'padWash'):
            notices.append((f'interrupted:{mission_id}:{phase}',
                {'kind': 'interrupted', 'mission_id': mission_id, 'phase': phase,
                 'message': f"{job.get('room') or 'Mission'} interrupted ({phase}); no blind restart."}))
        allowed, reason = recovery_allowed(current, job, settings, now)
        job['recovery_decision'] = reason
        request = store.reserve(db, job, job['pause_incident'], now) if allowed else None
        if request:
            request['room'] = job.get('room')
        return job, (request, notices)

    request, notices = store.transaction(mission_id, update)
    for key, notice in notices:
        c.notify(key, notice)
    return request


def _request_matches(request, row):
    return bool(row and all(request.get(key) == row[key]
                           for key in ('attempt_id', 'mission_id', 'incident', 'generation')))


def _job_matches(request, job):
    return bool(job and job.get('mission_id') == request['mission_id'] and
                job.get('pause_incident') == request['incident'] and
                job.get('claimed_incident') == request['incident'] and
                job.get('hold_generation', 0) == request['generation'] and
                job.get('recovery_attempt_id') == request['attempt_id'])


def recover(request):
    """Consume one durable claim; resume only the still-retained native mission."""
    from write_approval import grant_explicit_request
    from supervised_command import execute
    attempt_id = request.get('attempt_id') if isinstance(request, dict) else None
    if not attempt_id:
        raise c.ControlError('DurableRecoveryAttemptRequired')
    row = store.get(attempt_id)
    if not _request_matches(request, row):
        raise c.ControlError('RecoveryAttemptIntentMismatch')
    if not store.claim(attempt_id):
        raise c.ControlError('RecoveryAttemptAlreadyClaimedOrFenced')
    plan = None
    try:
        snapshot = c.read_local('/api/status')
        current = snapshot.get('state', {}).get('data', {})
        if (snapshot.get('state_stale') or
                snapshot.get('connection', {}).get('data', {}).get('status') != 'connected' or
                current.get('cleanMissionStatus', {}).get('missionId') != request['mission_id']):
            raise c.ControlError('FreshSameMissionRequiredForRecovery')
        job = c.job(request['mission_id'])
        if not _job_matches(request, job):
            raise c.ControlError('RecoveryIncidentOrGenerationChanged')
        allowed, reason = recovery_allowed(current, job, c.policy(), time.time(), reserved=True)
        if not allowed:
            raise c.ControlError(reason)
        context = {key: request[key] for key in ('attempt_id', 'mission_id', 'incident', 'generation')}
        plan = c.prepare('resume', snapshot, recovery_context=context)
        if not store.link_plan(attempt_id, plan['plan_id']):
            raise c.ControlError('RecoveryOwnerFenceChanged')
        grant_explicit_request(plan, 'owner-granted same-mission recovery:' + request['mission_id'] + ':' + request['incident'] + ':' + attempt_id)
        answer = execute(plan['plan_id'])
        status = ('complete' if answer.get('robot_action_confirmed') else
                  'unknown' if answer.get('may_have_executed') else 'needs_attention')
        store.finish(attempt_id, status, {'result': answer})
    except Exception as exc:
        journal = c.result(plan['plan_id']) if plan else None
        status = 'unknown' if journal and journal.get('may_have_executed') else 'needs_attention'
        store.finish(attempt_id, status,
                     {'error_code': str(exc) if isinstance(exc, c.ControlError) else type(exc).__name__,
                      'result': journal})
        raise
    c.notify('recovery:' + attempt_id,
        {'kind': 'recovery_result', 'mission_id': request['mission_id'],
         'attempt_id': attempt_id, 'plan_id': plan['plan_id'], 'outcome': answer.get('outcome'),
         'message': 'Resume ' + ('state transition observed' if answer.get('robot_action_confirmed') else 'unconfirmed; no retry')})
    return answer
