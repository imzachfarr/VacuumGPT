"""Same-origin dashboard requests, durably claimed before asynchronous dispatch.

A restart never replays a queued or uncertain request. All robot dispatch still
passes through the owner grant, exact-device gates and existing command journal.
"""
import contextlib
import json
import re
import threading
import time
import control_layer as c

_lock = threading.Lock()
_active = set()


def database():
    db = c.database()
    db.execute('CREATE TABLE IF NOT EXISTS dashboard_requests(id TEXT PRIMARY KEY, intent TEXT NOT NULL, state TEXT NOT NULL, result TEXT, updated REAL)')
    db.commit()
    return db


def validate(item):
    if not isinstance(item, dict):
        raise c.ControlError('InvalidRequest')
    if not isinstance(item.get('request_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', item['request_id']):
        raise c.ControlError('StableUniqueRequestIdRequired')
    operation = item.get('operation')
    keys = {'request_id', 'operation'}
    if operation == 'command':
        keys |= {'action', 'rooms', 'mode', 'ordered', 'suction', 'passes','area_points','map_id','map_version','snapshot_id'}
        if item.get('action') not in c.ACTIONS:
            raise c.ControlError('UnsupportedAction')
        if item['action'] not in ('clean_room','clean_area') and set(item) - {'request_id', 'operation', 'action'}:
            raise c.ControlError('BasicActionSettingsRefused')
        if item['action']=='clean_area' and (set(item)-{'request_id','operation','action','area_points','map_id','map_version','snapshot_id','mode'} or not {'area_points','map_id','map_version','snapshot_id'}<=set(item)):raise c.ControlError('SpotFieldsRequired')
        if item['action']=='clean_room' and set(item)&{'area_points','map_id','map_version','snapshot_id'}:raise c.ControlError('UnexpectedRequestFields')
    elif operation == 'cancel_navigation':
        keys |= {'target_request_id'}
        if not isinstance(item.get('target_request_id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',item['target_request_id']):raise c.ControlError('StableUniqueRequestIdRequired')
    elif operation == 'schedule':
        keys |= {'rooms', 'local_time', 'weekdays', 'timezone_name', 'mode', 'ordered', 'suction', 'passes'}
        if not {'rooms', 'local_time', 'weekdays'} <= set(item):
            raise c.ControlError('ScheduleFieldsRequired')
    else:
        raise c.ControlError('UnsupportedOperation')
    if set(item) - keys:
        raise c.ControlError('UnexpectedRequestFields')
    if not c.policy().get('direct_chat_commands'):
        raise c.ControlError('PersistentRoomControlGrantRequired')


def result(request_id):
    with _lock:
        with contextlib.closing(database()) as db:
            row = db.execute('SELECT intent,state,result FROM dashboard_requests WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise c.ControlError('UnknownRequest')
        state = row[1]
        answer = json.loads(row[2]) if row[2] else None
        if state in ('queued', 'running') and request_id not in _active:
            # Recover the direct command journal if it completed before the
            # HTTP worker was interrupted. Otherwise report uncertainty.
            with contextlib.closing(c.database()) as db:
                plan = db.execute('SELECT plan_id FROM requests WHERE request_id=?', (request_id,)).fetchone()
            answer = c.result(plan[0]) if plan else None
            answer = answer or {'outcome': 'interrupted', 'retry_permitted': False}
            state = 'finished'
        if state=='running' and not answer:
            with contextlib.closing(c.database()) as db:
                plan=db.execute('SELECT plan_id FROM requests WHERE request_id=?',(request_id,)).fetchone()
            answer=c.result(plan[0]) if plan else None
        return {'request_id': request_id, 'state': state, 'result': answer, 'retry_permitted': False}


def submit(item):
    validate(item)
    rid = item['request_id']
    if item['operation']=='cancel_navigation':
        from garbage_navigation import cancel
        return {'request_id':rid,'state':'finished','result':cancel(item['target_request_id']),'retry_permitted':False}
    intent = json.dumps({k: v for k, v in item.items() if k != 'request_id'}, sort_keys=True)
    with _lock:
        with contextlib.closing(database()) as db, db:
            row = db.execute('SELECT intent FROM dashboard_requests WHERE id=?', (rid,)).fetchone()
            if row:
                if row[0] != intent:
                    raise c.ControlError('RequestIdIntentMismatch')
            else:
                if _active:
                    raise c.ControlError('AnotherDashboardRequestInProgress')
                db.execute('INSERT INTO dashboard_requests VALUES(?,?,?,NULL,?)', (rid, intent, 'queued', time.time()))
                _active.add(rid)
                threading.Thread(target=_execute, args=(rid, item), daemon=True).start()
    return result(rid)


def _execute(rid, item):
    try:
        with contextlib.closing(database()) as db, db:
            db.execute('UPDATE dashboard_requests SET state=?,updated=? WHERE id=?', ('running', time.time(), rid))
        settings = {k: v for k, v in item.items() if k not in ('request_id', 'operation')}
        if item['operation'] == 'command':
            import direct_control
            answer = direct_control.run(request_id=rid, **settings)
        else:
            import local_schedules
            answer = local_schedules.create(**settings)
    except Exception as exc:
        answer = {'error_code': str(exc) if isinstance(exc, c.ControlError) else type(exc).__name__, 'retry_permitted': False}
    try:
        with contextlib.closing(database()) as db, db:
            db.execute('UPDATE dashboard_requests SET state=?,result=?,updated=? WHERE id=?', ('finished', json.dumps(answer), time.time(), rid))
    finally:
        with _lock:
            _active.discard(rid)
