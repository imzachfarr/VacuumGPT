"""Atomic mission updates and a durable, at-most-once recovery outbox.

This module never talks to Roomba. Eligibility belongs to the monitor and the
supervisor; this store preserves claims, owner fences, and uncertain receipts.
All paths are resolved through control_layer.ROOT so offline tests stay isolated.
"""
import contextlib
import json
import time
import uuid

import control_layer as c

TERMINAL = frozenset(('complete', 'unknown', 'needs_attention', 'cancelled'))
COLUMNS = 'attempt_id,mission_id,incident,generation,status,plan_id,created,updated,data'


def _database():
    db = c.database()
    db.execute('''CREATE TABLE IF NOT EXISTS recovery_outbox(
        attempt_id TEXT PRIMARY KEY,
        mission_id TEXT NOT NULL,
        incident TEXT NOT NULL,
        generation INTEGER NOT NULL,
        status TEXT NOT NULL,
        plan_id TEXT,
        created REAL NOT NULL,
        updated REAL NOT NULL,
        data TEXT NOT NULL,
        UNIQUE(mission_id,incident,generation))''')
    db.execute('CREATE INDEX IF NOT EXISTS recovery_outbox_pending ON recovery_outbox(status,created)')
    db.commit()
    return db


def _row(row):
    if row is None:
        return None
    answer = dict(zip(COLUMNS.split(','), row))
    answer['data'] = json.loads(answer['data'])
    return answer


def _read_job(db, mission_id):
    row = db.execute('SELECT data FROM jobs WHERE mission_id=?', (mission_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _save_job(db, mission_id, job, previous_revision):
    if not isinstance(job, dict) or job.get('mission_id') != mission_id:
        raise c.ControlError('RecoveryJobIdentityMismatch')
    job['revision'] = previous_revision + 1
    job.setdefault('hold_generation', 0)
    db.execute('''INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(mission_id)
        DO UPDATE SET updated=excluded.updated,data=excluded.data''',
        (mission_id, time.time(), json.dumps(job)))


def transaction(mission_id, callback):
    """Apply callback(job_or_None, db)->(updated_job_or_None, value) atomically.

    A None updated job leaves the job untouched. Callbacks must not notify, call
    another store/database function, or perform I/O while holding the transaction.
    """
    if not isinstance(mission_id, str) or not mission_id:
        raise c.ControlError('RecoveryMissionIdentityRequired')
    with contextlib.closing(_database()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            job = _read_job(db, mission_id)
            revision = (job or {}).get('revision', 0)
            updated, value = callback(job, db)
            if updated is not None:
                _save_job(db, mission_id, updated, revision)
            db.commit()
            return value
        except BaseException:
            db.rollback()
            raise


def fence(mission_id, reason):
    """Hold this mission, advance its generation, and cancel queued claims."""
    def update(job, db):
        job = job or {'mission_id': mission_id, 'automatic_resume': False,
                      'observed_only': True, 'finished': False}
        job.update(held=True, hold_reason=str(reason), hold_at=time.time(),
                   hold_generation=job.get('hold_generation', 0) + 1)
        cancelled = db.execute('''UPDATE recovery_outbox SET status='cancelled',updated=?
            WHERE mission_id=? AND status='queued' ''', (time.time(), mission_id)).rowcount
        return job, {'mission_id': mission_id, 'generation': job['hold_generation'],
                     'cancelled': cancelled}
    return transaction(mission_id, update)


def reserve(db, job, incident, now):
    """Reserve once inside transaction(); duplicate incident returns None.

    The job is mutated in the same transaction. A reserved attempt consumes its
    budget even if later refused or uncertain, and is never recreated on restart.
    """
    if not isinstance(job, dict) or not job.get('mission_id'):
        raise c.ControlError('RecoveryMissionIdentityRequired')
    if job.get('held') or job.get('finished'):
        raise c.ControlError('RecoveryDisabledOrHeld')
    if not isinstance(incident, str) or not incident:
        raise c.ControlError('RecoveryIncidentRequired')
    if job.get('pause_incident') not in (None, incident):
        raise c.ControlError('RecoveryIncidentChanged')
    generation = job.get('hold_generation', 0)
    if type(generation) is not int or generation < 0:
        raise c.ControlError('RecoveryGenerationInvalid')
    attempt_id = str(uuid.uuid4())
    inserted = db.execute('''INSERT OR IGNORE INTO recovery_outbox
        VALUES(?,?,?,?,?,?,?,?,?)''',
        (attempt_id, job['mission_id'], incident, generation, 'queued', None,
         now, now, '{}')).rowcount
    if not inserted:
        return None
    job.update(claimed_incident=incident, recovery_attempt_id=attempt_id,
               resume_count=job.get('resume_count', 0) + 1, last_resume_at=now)
    return {'attempt_id': attempt_id, 'mission_id': job['mission_id'],
            'incident': incident, 'generation': generation}


def get(attempt_id):
    with contextlib.closing(_database()) as db:
        return _row(db.execute('SELECT ' + COLUMNS + ' FROM recovery_outbox WHERE attempt_id=?',
                              (attempt_id,)).fetchone())


def pending(limit=10):
    with contextlib.closing(_database()) as db:
        rows = db.execute('SELECT ' + COLUMNS + ''' FROM recovery_outbox
            WHERE status='queued' ORDER BY created,attempt_id LIMIT ?''',
            (max(1, min(int(limit), 100)),)).fetchall()
    return [_row(row) for row in rows]


def _fenced(row, job):
    return (not job or job.get('held') or job.get('finished') or
            job.get('hold_generation', 0) != row['generation'] or
            job.get('recovery_attempt_id') != row['attempt_id'] or
            job.get('pause_incident') not in (None, row['incident']))


def claim(attempt_id):
    """CAS queued to validating; an owner fence makes the claim ineligible."""
    with contextlib.closing(_database()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = _row(db.execute('SELECT ' + COLUMNS + ' FROM recovery_outbox WHERE attempt_id=?',
                                 (attempt_id,)).fetchone())
            if not row or row['status'] != 'queued':
                db.commit()
                return None
            job = _read_job(db, row['mission_id'])
            if _fenced(row, job):
                db.execute("UPDATE recovery_outbox SET status='cancelled',updated=? WHERE attempt_id=?",
                           (time.time(), attempt_id))
                db.commit()
                return None
            now = time.time()
            db.execute("UPDATE recovery_outbox SET status='validating',updated=? WHERE attempt_id=? AND status='queued'",
                       (now, attempt_id))
            db.commit()
            return {**row, 'status': 'validating', 'updated': now}
        except BaseException:
            db.rollback()
            raise


def link_plan(attempt_id, plan_id):
    """Link the one validating attempt to one persisted exact-mission plan."""
    with contextlib.closing(_database()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = _row(db.execute('SELECT ' + COLUMNS + ' FROM recovery_outbox WHERE attempt_id=?',
                                 (attempt_id,)).fetchone())
            if not row or row['status'] != 'validating':
                db.commit()
                return None
            if row['plan_id'] and row['plan_id'] != plan_id:
                raise c.ControlError('RecoveryAttemptPlanMismatch')
            plan_row = db.execute('SELECT data FROM plans WHERE id=?', (plan_id,)).fetchone()
            if not plan_row or json.loads(plan_row[0]).get('mission_id') != row['mission_id']:
                raise c.ControlError('RecoveryPlanIdentityMismatch')
            if _fenced(row, _read_job(db, row['mission_id'])):
                db.execute("UPDATE recovery_outbox SET status='cancelled',updated=? WHERE attempt_id=?",
                           (time.time(), attempt_id))
                db.commit()
                return None
            now = time.time()
            db.execute('UPDATE recovery_outbox SET plan_id=?,updated=? WHERE attempt_id=?',
                       (plan_id, now, attempt_id))
            db.commit()
            return {**row, 'plan_id': plan_id, 'updated': now}
        except BaseException:
            db.rollback()
            raise


def dispatch(attempt_id, plan_id, data):
    """Commit one logical dispatch, serialized with owner fences.

    A committed journal is conservatively treated as possibly executed even if
    the process exits before MQTT publication. The outbox remains validating
    until the normal receipt or restart reconciliation finishes it. No network
    call belongs inside this transaction.
    """
    if not c.policy().get('automatic_resume'):
        raise c.ControlError('RecoveryPermissionChangedBeforeDispatch')
    with contextlib.closing(_database()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = _row(db.execute('SELECT ' + COLUMNS + ' FROM recovery_outbox WHERE attempt_id=?',
                                 (attempt_id,)).fetchone())
            if not row or row['status'] != 'validating' or row['plan_id'] != plan_id:
                raise c.ControlError('RecoveryClaimChangedBeforeDispatch')
            job = _read_job(db, row['mission_id'])
            if _fenced(row, job) or not job.get('automatic_resume'):
                raise c.ControlError('RecoveryPermissionChangedBeforeDispatch')
            if job.get('pause_incident') != row['incident']:
                raise c.ControlError('RecoveryClaimChangedBeforeDispatch')
            plan_row = db.execute('SELECT data FROM plans WHERE id=?', (plan_id,)).fetchone()
            plan = json.loads(plan_row[0]) if plan_row else {}
            context = plan.get('recovery_context')
            if (plan.get('action') != 'resume' or plan.get('mission_id') != row['mission_id']
                    or not isinstance(context, dict)
                    or any(context.get(key) != row[key] for key in
                           ('attempt_id', 'mission_id', 'incident', 'generation'))
                    or plan.get('expected_hold_generation') != row['generation']):
                raise c.ControlError('RecoveryPlanContextMismatch')
            journal = db.execute('SELECT stage,result FROM attempts WHERE plan_id=?', (plan_id,)).fetchone()
            if (not journal or journal[0] != 'validating'
                    or json.loads(journal[1]).get('may_have_executed')):
                raise c.ControlError('RecoveryDispatchAlreadyCommittedOrUnvalidated')
            now = time.time()
            dispatch_data = {**data, 'may_have_executed': True,
                             'recovery_context': context, 'hold_generation': row['generation']}
            encoded = json.dumps(dispatch_data)
            db.execute('INSERT INTO command_audit(plan_id,at,stage,data) VALUES(?,?,?,?)',
                       (plan_id, now, 'dispatching', encoded))
            db.execute('''INSERT INTO attempts VALUES(?,?,?,?) ON CONFLICT(plan_id)
                DO UPDATE SET updated=excluded.updated,stage=excluded.stage,result=excluded.result''',
                       (plan_id, now, 'dispatching', encoded))
            metadata = {**row['data'], 'dispatch_committed_at': now}
            db.execute('UPDATE recovery_outbox SET updated=?,data=? WHERE attempt_id=?',
                       (now, json.dumps(metadata), attempt_id))
            db.commit()
            return {**row, 'updated': now, 'data': metadata}
        except BaseException:
            db.rollback()
            raise


def finish(attempt_id, status, data=None):
    """Finish without ever making an uncertain or completed attempt replayable."""
    if status not in TERMINAL:
        raise c.ControlError('RecoveryTerminalStatusRequired')
    with contextlib.closing(_database()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = _row(db.execute('SELECT ' + COLUMNS + ' FROM recovery_outbox WHERE attempt_id=?',
                                 (attempt_id,)).fetchone())
            if not row:
                raise c.ControlError('UnknownRecoveryAttempt')
            if row['status'] in TERMINAL:
                db.commit()
                return row
            merged = {**row['data'], **(data or {})}
            now = time.time()
            db.execute('UPDATE recovery_outbox SET status=?,updated=?,data=? WHERE attempt_id=?',
                       (status, now, json.dumps(merged), attempt_id))
            db.commit()
            return {**row, 'status': status, 'updated': now, 'data': merged}
        except BaseException:
            db.rollback()
            raise


def list_attempts(limit=50, mission_id=None):
    with contextlib.closing(_database()) as db:
        query = 'SELECT ' + COLUMNS + ' FROM recovery_outbox'
        values = []
        if mission_id is not None:
            query += ' WHERE mission_id=?'
            values.append(mission_id)
        query += ' ORDER BY created DESC,attempt_id DESC LIMIT ?'
        values.append(max(1, min(int(limit), 100)))
        return [_row(row) for row in db.execute(query, values).fetchall()]


def reconcile():
    """Reconcile restart gaps; a linked plan is never automatically replayed.

    Only a validating claim without any plan can return to queued. A journal that
    may have dispatched is terminal complete/unknown; a linked pre-dispatch plan
    becomes needs_attention because its ticket may already have been consumed.
    """
    changed = []
    with contextlib.closing(_database()) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            rows = db.execute('SELECT ' + COLUMNS + ''' FROM recovery_outbox
                WHERE status IN ('queued','validating') ORDER BY created''').fetchall()
            for raw in rows:
                row = _row(raw)
                journal = db.execute('SELECT stage,result FROM attempts WHERE plan_id=?',
                                     (row['plan_id'],)).fetchone() if row['plan_id'] else None
                receipt = json.loads(journal[1]) if journal else {}
                may_have_executed = bool(receipt.get('may_have_executed')) or bool(journal and
                    journal[0] in ('dispatching', 'dispatch_uncertain', 'broker_acknowledged',
                                   'delivery_unconfirmed', 'observing_restart', 'complete'))
                if may_have_executed:
                    status = 'complete' if receipt.get('robot_action_confirmed') else 'unknown'
                    reason = 'DispatchJournalNeverReplay'
                elif _fenced(row, _read_job(db, row['mission_id'])):
                    status, reason = 'cancelled', 'OwnerFenceOrIncidentChanged'
                elif row['plan_id']:
                    status, reason = 'needs_attention', 'LinkedPlanBeforeDispatchNeedsReview'
                elif row['status'] == 'validating':
                    status, reason = 'queued', 'NoPlanCreatedSafeToRevalidate'
                else:
                    continue
                now = time.time()
                data = {**row['data'], 'reconciliation': reason}
                if journal:
                    data.update(journal_stage=journal[0], journal_result=receipt)
                db.execute('UPDATE recovery_outbox SET status=?,updated=?,data=? WHERE attempt_id=?',
                           (status, now, json.dumps(data), row['attempt_id']))
                changed.append({**row, 'status': status, 'updated': now, 'data': data})
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return changed
