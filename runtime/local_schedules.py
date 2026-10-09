"""Durable local cleaning schedules. Claimed occurrences never blindly retry."""
import contextlib
from datetime import datetime,timezone,timedelta
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo
import control_layer as c

def db():
    connection=c.database()
    connection.execute('CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, enabled INTEGER, data TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS schedule_runs(occurrence TEXT PRIMARY KEY,schedule_id TEXT,at REAL,outcome TEXT,data TEXT)')
    connection.commit();return connection

def list_schedules():
    with contextlib.closing(db()) as connection:
        return [{'schedule_id':r[0],'enabled':bool(r[1]),**json.loads(r[2])} for r in connection.execute('SELECT id,enabled,data FROM schedules ORDER BY id')]

def create(rooms,local_time,weekdays,timezone_name='America/New_York',mode='default',ordered=True,suction=None,passes=None):
    if not c.policy().get('direct_chat_commands'):raise c.ControlError('PersistentRoomControlGrantRequired')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',local_time):raise c.ControlError('TimeMustBeHHMM')
    if not isinstance(weekdays,list) or not weekdays or any(type(d) is not int or d not in range(7) for d in weekdays):raise c.ControlError('WeekdaysMustBe0MondayTo6Sunday')
    try:ZoneInfo(timezone_name)
    except Exception:raise c.ControlError('IanaTimezoneRequired') from None
    snapshot=c.read_local('/api/status')
    preview=json.loads(json.dumps(snapshot));preview['state']['data']['cleanMissionStatus'].update(phase='charge',error=0,notReady=0,condNotReady=[])
    chosen=c.policy().get('default_room_mode','vacuum') if mode=='default' else mode
    plan=c.prepare('clean_room',preview,mode=chosen,rooms=rooms,ordered=ordered,suction=suction,passes=passes)
    data={'rooms':plan['rooms'],'local_time':local_time,'weekdays':sorted(set(weekdays)),'timezone':timezone_name,
        'mode':chosen,'ordered':ordered,'suction':suction,'passes':passes,'map_id':plan['map_id'],'map_version':plan['map_version'],
        'created_at':time.time(),'not_before':time.time(),'missed_run_policy':'skip_after_10_minutes','busy_policy':'skip','ambiguous_dst_policy':'once_per_local_date'}
    sid=str(uuid.uuid4())
    with contextlib.closing(db()) as connection,connection:connection.execute('INSERT INTO schedules VALUES(?,1,?)',(sid,json.dumps(data)))
    return {'schedule_id':sid,'enabled':True,**data,'robot_commands_sent':0}

def set_enabled(schedule_id,enabled):
    if type(enabled) is not bool:raise c.ControlError('BooleanRequired')
    with contextlib.closing(db()) as connection,connection:
        changed=connection.execute('UPDATE schedules SET enabled=? WHERE id=?',(int(enabled),schedule_id)).rowcount
    if not changed:raise c.ControlError('UnknownSchedule')
    return {'schedule_id':schedule_id,'enabled':enabled}

def delete(schedule_id):
    with contextlib.closing(db()) as connection,connection:
        changed=connection.execute('DELETE FROM schedules WHERE id=?',(schedule_id,)).rowcount
    if not changed:raise c.ControlError('UnknownSchedule')
    return {'schedule_id':schedule_id,'deleted':True,'history_preserved':True}

def tick(now=None):
    now=time.time() if now is None else now
    if not c.policy().get('direct_chat_commands'):return []
    results=[]
    for schedule in list_schedules():
        if not schedule['enabled']:continue
        local=datetime.fromtimestamp(now,ZoneInfo(schedule['timezone']))
        if local.weekday() not in schedule['weekdays']:continue
        hour,minute=map(int,schedule['local_time'].split(':'))
        due=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
        age=now-due.timestamp()
        if not 0<=age<600 or due.timestamp()<schedule['not_before']:continue
        occurrence=schedule['schedule_id']+':'+local.date().isoformat()
        with contextlib.closing(db()) as connection,connection:
            claimed=connection.execute('INSERT OR IGNORE INTO schedule_runs VALUES(?,?,?, ?,?)',(occurrence,schedule['schedule_id'],now,'claimed',json.dumps({'retry_permitted':False}))).rowcount
        if not claimed:continue
        try:
            snapshot=c.read_local('/api/status')
            if snapshot.get('state_stale'):raise c.ControlError('FreshStateRequired')
            current=snapshot.get('state',{}).get('data',{});c.preconditions('clean_room',current.get('cleanMissionStatus',{}))
            if not any(m.get('p2map_id')==schedule['map_id'] and m.get('p2mapv_id')==schedule['map_version'] for m in current.get('p2maps',[])):raise c.ControlError('ScheduleMapChanged')
            from direct_control import run
            request='schedule-'+schedule['schedule_id']+'-'+local.date().isoformat()
            answer=run('clean_room',request,mode=schedule['mode'],rooms=schedule['rooms'],ordered=schedule['ordered'],suction=schedule['suction'],passes=schedule['passes'])
            outcome=answer.get('outcome','unknown')
        except Exception as exc:
            outcome='skipped';answer={'error_code':str(exc) if isinstance(exc,c.ControlError) else type(exc).__name__,'retry_permitted':False}
        with contextlib.closing(db()) as connection,connection:
            connection.execute('UPDATE schedule_runs SET outcome=?,data=? WHERE occurrence=?',(outcome,json.dumps(answer),occurrence))
        c.notify('schedule:'+occurrence,{'kind':'schedule_result','schedule_id':schedule['schedule_id'],'outcome':outcome,'message':'Scheduled clean: '+outcome})
        results.append({'occurrence':occurrence,'outcome':outcome,**answer})
    return results
