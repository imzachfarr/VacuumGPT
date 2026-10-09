"""Durable 20-second low-motion review; never auto-classifies an obstacle."""
import contextlib,json,math,time,uuid
import control_layer as c

def database():
    db=c.database()
    db.execute('CREATE TABLE IF NOT EXISTS motion_windows(key TEXT PRIMARY KEY,data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS stuck_reviews(id TEXT PRIMARY KEY,dedup TEXT UNIQUE,state TEXT NOT NULL,data TEXT NOT NULL)')
    db.commit();return db

def observe(snapshot):
    if snapshot.get('state_stale') or snapshot.get('pose_stale'):return None
    mission=snapshot.get('state',{}).get('data',{}).get('cleanMissionStatus',{})
    pose=snapshot.get('pose') or {};frame=pose.get('data',{});points=frame.get('points') or []
    mid=mission.get('missionId');map_id=frame.get('map_id')
    if mission.get('phase') not in ('run','pause','paused') or mission.get('cycle')!='clean' or not mid or pose.get('mission')!=mid or not map_id or not points:return None
    managed=c.job(mid)
    if managed and (managed.get('held') or managed.get('finished')):return None
    point=points[-1];x,y=point.get('x'),point.get('y');stamp=frame.get('timestamp',pose.get('at'))
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (x,y,stamp)):return None
    key=mid+':'+map_id
    with contextlib.closing(database()) as db,db:
        row=db.execute('SELECT data FROM motion_windows WHERE key=?',(key,)).fetchone()
        window=json.loads(row[0]) if row else None
        if window and stamp<=window['last_timestamp']:return None
        if not window or stamp-window['last_timestamp']>15 or math.hypot(x-window['anchor'][0],y-window['anchor'][1])>.10:
            window={'anchor':[x,y],'since':stamp,'last_timestamp':stamp,'samples':1,'map_version':frame.get('map_version')}
        else:window.update(last_timestamp=stamp,samples=window['samples']+1)
        db.execute('INSERT INTO motion_windows VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data',(key,json.dumps(window)))
        if stamp-window['since']<=20 or window['samples']<3:return None
        dedup=map_id+':'+str(round(window['anchor'][0]*10))+':'+str(round(window['anchor'][1]*10))
        pending=db.execute("SELECT id FROM stuck_reviews WHERE dedup=? AND state IN ('pending','permanent','permanent_requested','ban_unverified')",(dedup,)).fetchone()
        if pending:return None
        prior=db.execute('SELECT data FROM stuck_reviews WHERE dedup=?',(dedup,)).fetchone()
        if prior and stamp-json.loads(prior[0]).get('reviewed_at',0)<3600:return None
        incident={'incident_id':str(uuid.uuid4()),'created_at':time.time(),'mission_id':mid,'map_id':map_id,'map_version':frame.get('map_version'),
            'center':window['anchor'],'last_point':[x,y],'stationary_seconds':stamp-window['since'],'samples':window['samples'],
            'radius_map_units':.10,'state':'pending','classification':None,'fault_code':mission.get('error'),'automatic_ban':False}
        db.execute('DELETE FROM stuck_reviews WHERE dedup=?',(dedup,))
        db.execute('INSERT INTO stuck_reviews VALUES(?,?,?,?)',(incident['incident_id'],dedup,'pending',json.dumps(incident)))
    c.notify('stuck-review:'+incident['incident_id'],{'kind':'stuck_review','incident_id':incident['incident_id'],'mission_id':mid,'center':incident['center'],
        'message':'Roomba has stayed near the same location for over 20 seconds. Review: temporary obstruction or permanent obstacle? No location was automatically banned.'})
    return incident

def reviews():
    with contextlib.closing(database()) as db:return [json.loads(row[0]) for row in db.execute('SELECT data FROM stuck_reviews ORDER BY rowid DESC LIMIT 100')]

def classify(incident_id,classification):
    if not c.policy().get('direct_chat_commands'):raise c.ControlError('PersistentRoomControlGrantRequired')
    if classification not in ('temporary','permanent','not_stuck'):raise c.ControlError('InvalidStuckClassification')
    with contextlib.closing(database()) as db:
        row=db.execute('SELECT state,data FROM stuck_reviews WHERE id=?',(incident_id,)).fetchone()
    if not row:raise c.ControlError('UnknownStuckReview')
    incident=json.loads(row[1])
    if row[0]!='pending':return {**incident,'duplicate_review':True}
    incident.update(classification=classification,reviewed_at=time.time(),state='permanent_requested' if classification=='permanent' else classification)
    # Claim before any map write; repeated classifications never dispatch again.
    with contextlib.closing(database()) as db,db:
        if not db.execute("UPDATE stuck_reviews SET state=?,data=? WHERE id=? AND state='pending'",(incident['state'],json.dumps(incident),incident_id)).rowcount:
            raise c.ControlError('ReviewAlreadyClaimed')
    if classification=='permanent':
        from chair_keepout import ban_point
        answer=ban_point(incident['center'],incident['map_id'],'user permanent-obstacle review:'+incident_id)
        incident.update(state='permanent' if answer.get('keepout_verified') else 'ban_unverified',keepout_result=answer)
        with contextlib.closing(database()) as db,db:db.execute('UPDATE stuck_reviews SET state=?,data=? WHERE id=?',(incident['state'],json.dumps(incident),incident_id))
    return incident
