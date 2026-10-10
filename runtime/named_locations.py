"""Persistent vendor-coordinate service locations. Saving never moves Roomba."""
import contextlib
import json
import math
import time
from shapely.geometry import Point,shape
from shapely.ops import unary_union
import control_layer as c
from spot_control import file,features


def get():
    with contextlib.closing(c.database()) as db:
        db.execute('CREATE TABLE IF NOT EXISTS named_locations(kind TEXT PRIMARY KEY,data TEXT NOT NULL)')
        row=db.execute('SELECT data FROM named_locations WHERE kind=?',('garbage_can',)).fetchone()
    return {'location':json.loads(row[0]) if row else None,'navigation_available':True,'navigation_method':'mapped_room_approach','navigation_hardware_verified':False}


def save(map_id,map_version,snapshot_id,point):
    if type(snapshot_id) is not int or snapshot_id<1 or not isinstance(map_id,str) or not isinstance(map_version,str):raise c.ControlError('LocationSnapshotRequired')
    if not isinstance(point,list) or len(point)!=2 or any(type(v) not in (float,int) or not math.isfinite(v) or abs(v)>1e5 for v in point):raise c.ControlError('FiniteMapCoordinatesRequired')
    s=c.read_local('/api/status');active=s.get('state',{}).get('data',{}).get('p2maps',[])
    if len(active)!=1 or active[0].get('p2map_id')!=map_id:raise c.ControlError('LocationRequiresCurrentHouseMap')
    doc=c.read_local('/api/map/'+str(snapshot_id));meta=file(doc,'metadata.json') or {}
    if meta.get('id')!=map_id or meta.get('version')!=map_version:raise c.ControlError('LocationSnapshotMismatch')
    borders=[shape(f['geometry']) for f in features(file(doc,'borders.geojson')) if f.get('geometry',{}).get('type') in ('Polygon','MultiPolygon')]
    if not borders:raise c.ControlError('RetainedHouseBoundaryRequired')
    boundary=unary_union(borders);p=Point(point)
    if not boundary.is_valid or not boundary.covers(p):raise c.ControlError('LocationOutsideHouse')
    policies=file(doc,'policyZones.geojson')
    if policies is None:raise c.ControlError('LocationKeepOutGeometryRequired')
    for f in features(policies):
        if f.get('geometry',{}).get('type') in ('Polygon','MultiPolygon','LineString','MultiLineString'):
            z=shape(f['geometry'])
            if not z.is_valid:raise c.ControlError('LocationKeepOutGeometryRequired')
            if z.intersects(p):raise c.ControlError('LocationInsideKeepOut')
    now=time.time()
    with contextlib.closing(c.database()) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS named_locations(kind TEXT PRIMARY KEY,data TEXT NOT NULL)')
        old=db.execute('SELECT data FROM named_locations WHERE kind=?',('garbage_can',)).fetchone()
        prior=json.loads(old[0]) if old else {}
        data={'kind':'garbage_can','name':'Garbage can','point':list(point),'map_id':map_id,'map_version':map_version,'snapshot_id':snapshot_id,
            'created_at':prior.get('created_at',now),'updated_at':now,'revision':prior.get('revision',0)+1,'robot_commands_sent':0}
        db.execute('INSERT INTO named_locations VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET data=excluded.data',('garbage_can',json.dumps(data)))
    return {'location':data,'navigation_available':True,'navigation_method':'mapped_room_approach','navigation_hardware_verified':False}
