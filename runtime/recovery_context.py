"""Positive native mission, pause provenance and map evidence for resume."""
import time
import math
import control_layer as c
from recovery_diagnostics import stamp, command


def native_anchor(reported):
    m = reported.get('cleanMissionStatus', {})
    return {'mission_id': m.get('missionId'), 'mission_start': m.get('mssnStrtTm'),
            'n_mission': m.get('nMssn'), 'cycle': m.get('cycle'), 'operating_mode': m.get('operatingMode')}


def enrich(reported, currentstate_payload, software_payload, connection_payload, map_context=None):
    answer = dict(reported)
    software = software_payload.get('state', {}).get('reported', {})
    connection = connection_payload.get('state', {}).get('reported', {})
    answer['_recovery'] = {'checked_at': time.time(),
        'phase_reported_at': stamp(currentstate_payload, ('cleanMissionStatus', 'phase')),
        'error_reported_at': stamp(currentstate_payload, ('cleanMissionStatus', 'error')),
        'last_command': command(software.get('lastCommand')),
        'connected': connection.get('connected') is True,
        'map_context': map_context}
    return answer


def needs_pause_evidence(mission):
    return mission.get('phase') in ('pause', 'paused') or (mission.get('phase') == 'stop' and mission.get('cycle') == 'clean' and type(mission.get('mssnStrtTm')) in (int, float) and mission['mssnStrtTm'] > 0)


def is_paused(current):
    """Exact 8.4 reports native work_mode0x25 as stop+clean, not pause."""
    m = current.get('cleanMissionStatus', {})
    if m.get('phase') in ('pause', 'paused'): return True
    evidence = current.get('_recovery', {})
    last = evidence.get('last_command', {})
    phase_at = evidence.get('phase_reported_at')
    return (m.get('phase') == 'stop' and m.get('cycle') == 'clean' and
            type(m.get('mssnStrtTm')) in (int, float) and math.isfinite(m['mssnStrtTm']) and m['mssnStrtTm'] > 0 and
            type(m.get('error')) is int and m['error'] == 0 and type(m.get('notReady')) is int and m['notReady'] == 0 and m.get('condNotReady') == [] and
            last.get('command') == 'pause' and type(last.get('time')) in (int, float) and
            type(phase_at) in (int, float) and -5 <= phase_at - last['time'] <= 30)


def reconcile_pause(plan_id, snapshot):
    """Resolve one existing pause receipt using exact device proof, without replay."""
    plan = c.get_plan(plan_id, allow_expired=True)
    receipt = c.result(plan_id) or {}
    current = snapshot.get('state', {}).get('data', {})
    evidence = current.get('_recovery', {})
    last = evidence.get('last_command', {})
    if plan.get('action') != 'pause' or not receipt.get('may_have_executed') or receipt.get('command_time') != last.get('time') or last.get('initiator') != 'localApp' or not is_paused(current) or native_anchor(current) != receipt.get('before_native_anchor', receipt.get('native_anchor')):
        raise c.ControlError('ExactRetainedPauseProofRequired')
    answer = {k: v for k, v in receipt.items() if k not in ('stage', 'plan_id', 'retry_permitted')}
    answer.update(robot_action_confirmed=True, outcome='retained_pause_observed', pause_echo_verified=True,
                  pause_reported_at=evidence.get('phase_reported_at'), command_replayed=False)
    c.record(plan_id, 'complete', answer)
    return c.result(plan_id)


def validate(current, job, now=None):
    now = time.time() if now is None else now
    m = current.get('cleanMissionStatus', {})
    if not job or m.get('missionId') != job.get('mission_id'): return False, 'MissionIdentityChanged'
    if type(m.get('error')) is not int or m.get('error') != 0: return False, 'ActiveRobotFault:' + str(m.get('error'))
    if type(m.get('notReady')) is not int or m.get('notReady') != 0 or m.get('condNotReady') != []: return False, 'RobotReadinessIncompleteOrBlocked'
    if not isinstance(m.get('cycle'), str) or m.get('cycle') == 'none' or not m['cycle']: return False, 'MissionCycleEndedNoPlannerReplay'
    if not is_paused(current): return False, 'MissionNotPaused'
    expected = job.get('native_anchor')
    observed = native_anchor(current)
    if not expected or type(expected.get('mission_start')) not in (int, float) or not math.isfinite(expected['mission_start']) or expected['mission_start'] <= 0:
        return False, 'NativeMissionAnchorUnverified'
    if type(expected.get('n_mission')) is not int or expected['n_mission'] < 0 or type(expected.get('operating_mode')) is not int or expected['operating_mode'] < 0:
        return False, 'NativeMissionAnchorUnverified'
    if type(observed.get('n_mission')) is not int or type(observed.get('operating_mode')) is not int or type(observed.get('mission_start')) not in (int, float):
        return False, 'NativeMissionContextChanged'
    if any(observed.get(k) != expected.get(k) for k in ('mission_id', 'mission_start', 'n_mission', 'cycle', 'operating_mode')):
        return False, 'NativeMissionContextChanged'
    if type(current.get('batPct')) not in (int, float) or not math.isfinite(current['batPct']) or current['batPct'] < 25: return False, 'BatteryTooLow'
    evidence = current.get('_recovery', {})
    if evidence.get('connected') is not True: return False, 'DeviceConnectionUnverified'
    phase_at = evidence.get('phase_reported_at')
    if type(phase_at) not in (int, float) or not -5 <= now - phase_at <= 120:
        return False, 'RobotPauseTelemetryStale'
    last = evidence.get('last_command', {})
    authorization = job.get('authorized_pause') or {}
    owner_test = (authorization.get('command_time') == last.get('time') and
                  authorization.get('phase_reported_at') == phase_at and
                  authorization.get('generation') == job.get('hold_generation', 0) and
                  last.get('command') == 'pause' and last.get('initiator') == 'localApp')
    # The starter's mission.initiator does not identify who paused the robot.
    # No native autonomous-pause origin is verified on this exact firmware yet.
    # A production job never receives this capability from an inferred name.
    native_pause = last.get('command') == 'pause' and last.get('initiator') in ('robot', 'system') and last.get('initiator') in job.get('verified_native_pause_origins', []) and type(last.get('time')) in (int, float) and abs(last['time'] - phase_at) <= 15
    if not owner_test and not native_pause: return False, 'PauseOriginUnverified'
    expected_map = job.get('recovery_map_context')
    actual_map = evidence.get('map_context')
    if not isinstance(expected_map, dict) or not isinstance(actual_map, dict) or any(not isinstance(value.get('topology_hash'), str) or not value.get('topology_hash') or not isinstance(value.get('region_ids'), list) or not value.get('region_ids') for value in (expected_map, actual_map)): return False, 'RecoveryMapGeometryUnverified'
    if expected_map.get('map_id') != actual_map.get('map_id') or expected_map.get('topology_hash') != actual_map.get('topology_hash') or expected_map.get('region_ids') != actual_map.get('region_ids'):
        return False, 'MissionMapGeometryChanged'
    if len(current.get('p2maps', [])) != 1 or current['p2maps'][0].get('p2map_id') != actual_map.get('map_id') or current['p2maps'][0].get('p2mapv_id') != actual_map.get('version'):
        return False, 'RecoveryMapEvidenceNotCurrent'
    return True, 'SameNativeMissionPausedAndReady'


def authorize_test_pause(plan_id, snapshot):
    """One owner-selected live test may release only its own confirmed pause hold."""
    import recovery_store as store
    plan = c.get_plan(plan_id, allow_expired=True)
    receipt = c.result(plan_id) or {}
    current = snapshot.get('state', {}).get('data', {})
    m = current.get('cleanMissionStatus', {})
    evidence = current.get('_recovery', {})
    last = evidence.get('last_command', {})
    if plan.get('action') != 'pause' or not receipt.get('robot_action_confirmed') or receipt.get('mission_id') != plan.get('mission_id') or m.get('missionId') != plan['mission_id'] or not is_paused(current):
        raise c.ControlError('ConfirmedExactTestPauseRequired')
    if last.get('command') != 'pause' or last.get('initiator') != 'localApp' or type(last.get('time')) not in (int, float) or last['time'] != receipt.get('command_time') or not plan['created_at'] - 5 <= last['time'] <= plan['expires_at']:
        raise c.ControlError('TestPauseCommandEchoUnverified')
    phase_at = evidence.get('phase_reported_at')
    if type(phase_at) not in (int, float) or not -5 <= phase_at - last['time'] <= 30:
        raise c.ControlError('TestPauseRobotReportUnverified')
    def update(job, db):
        if not job or not job.get('automatic_resume') or job.get('finished') or not job.get('held') or job.get('hold_reason') != 'explicit_pause':
            raise c.ControlError('TestPauseHoldChanged')
        # A later stop/dock/pause fence cannot be released by this earlier test.
        if job.get('hold_generation', 0) != receipt.get('hold_generation'):
            raise c.ControlError('TestPauseFenceChanged')
        candidate = {**job, 'authorized_pause': {'plan_id': plan_id, 'command_time': last['time'],
            'phase_reported_at': phase_at, 'generation': job.get('hold_generation', 0)}, 'held': False}
        allowed, reason = validate(current, candidate)
        if not allowed: raise c.ControlError(reason)
        candidate.update(hold_reason=None, pause_since=time.time(), pause_incident=None, claimed_incident=None)
        return candidate, {'mission_id': plan['mission_id'], 'hold_generation': candidate.get('hold_generation', 0),
                           'one_pause_authorized': True, 'command_sent': False}
    return store.transaction(plan['mission_id'], update)
