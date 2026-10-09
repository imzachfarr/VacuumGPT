"""Map-bound area drafts; unverified ad-hoc dispatch never falls back to rooms."""
import contextlib
import json
import math
import time
import uuid
import control_layer as c

def inside(point,ring):
    x,y=point;result=False
    for a,b in zip(ring,ring[1:]):
        cross=(b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0])
        if abs(cross)<1e-7 and min(a[0],b[0])<=x<=max(a[0],b[0]) and min(a[1],b[1])<=y<=max(a[1],b[1]):return True
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:result=not result
    return result

def save(name,map_id,map_version,points):
    if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise c.ControlError('AreaNameRequired')
    if not isinstance(points,list) or not 3<=len(points)<=100:raise c.ControlError('AreaRequires3To100MapPoints')
    for p in points:
        if not isinstance(p,list) or len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e8 for v in p):raise c.ControlError('FiniteMapCoordinatesRequired')
    ring=[list(p) for p in points]
    if ring[0]!=ring[-1]:ring.append(ring[0])
    area=abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:])))/2
    if area<1e-8:raise c.ControlError('AreaTooSmallOrDegenerate')
    snapshot=c.read_local('/api/status')
    # Drafts are immutable snapshot annotations, not live robot commands. The
    # active map may advance while the user draws; validate the drawn version.
    borders=[];retained_version=False
    for item in snapshot.get('maps',[]):
        doc=c.read_local('/api/map/'+str(item['id']));files=doc.get('files',{})
        meta=next((v for k,v in files.items() if k.split('/')[-1]=='metadata.json'),{})
        if meta.get('id')!=map_id or meta.get('version')!=map_version:continue
        retained_version=True
        for k,v in files.items():
            if k.split('/')[-1]=='borders.geojson':
                features=v.get('features',[]) if v.get('type')=='FeatureCollection' else [v] if v.get('type')=='Feature' else []
                for feature in features:
                    geometry=feature.get('geometry',{})
                    if geometry.get('type')=='Polygon':borders.append(geometry.get('coordinates',[]))
                    elif geometry.get('type')=='MultiPolygon':borders.extend(geometry.get('coordinates',[]))
        if borders:break
    if not retained_version:raise c.ControlError('RetainedMapVersionRequired')
    if not borders:raise c.ControlError('RetainedHouseBoundaryRequired')
    # A draft is checked against the retained footprint. Dispatch requires a separate
    # hardware verification gate and stricter polygon-edge/keep-out validation.
    if any(not any(inside(p,poly[0]) and not any(inside(p,hole) for hole in poly[1:]) for poly in borders) for p in ring):raise c.ControlError('SelectionOutsideHouseBoundary')
    data={'area_id':str(uuid.uuid4()),'name':name.strip(),'map_id':map_id,'map_version':map_version,
        'geometry':{'type':'Polygon','coordinates':[ring]},'created_at':time.time(),
        'dispatch_available':False,'reason':'ExactDeviceAdHocBoundaryNotVerified','robot_commands_sent':0}
    with contextlib.closing(c.database()) as connection,connection:
        connection.execute('CREATE TABLE IF NOT EXISTS areas(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        connection.execute('INSERT INTO areas VALUES(?,?)',(data['area_id'],json.dumps(data)))
    return data

def list_areas():
    with contextlib.closing(c.database()) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS areas(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        return [json.loads(r[0]) for r in connection.execute('SELECT data FROM areas ORDER BY rowid DESC LIMIT 100')]
