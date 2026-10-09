"""Temporary-region cleaning: exact drawn polygon, never room/global fallback."""
import math
import control_layer as c
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union


def file(doc,name):
    return next((v for k,v in doc.get('files',{}).items() if k.split('/')[-1]==name),None)


def features(value):
    if not isinstance(value,dict):return []
    return value.get('features',[]) if value.get('type')=='FeatureCollection' else [value] if value.get('type')=='Feature' else []


def polygon(points):
    if not isinstance(points,list) or not 3<=len(points)<=4096:raise c.ControlError('SpotRequires3To4096Points')
    if any(not isinstance(p,list) or len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e5 for v in p) for p in points):raise c.ControlError('FiniteMapCoordinatesRequired')
    p=Polygon(points)
    if p.is_empty or p.area<1e-5:raise c.ControlError('AreaTooSmallOrDegenerate')
    if not p.is_valid:raise c.ControlError('SpotOutlineCrossesItself')
    return p


def validate_document(doc,points,mode):
    p=polygon(points)
    borders=[shape(f['geometry']) for f in features(file(doc,'borders.geojson')) if f.get('geometry',{}).get('type') in ('Polygon','MultiPolygon')]
    if not borders:raise c.ControlError('RetainedHouseBoundaryRequired')
    boundary=unary_union(borders)
    if not boundary.is_valid or not boundary.covers(p):raise c.ControlError('SelectionOutsideHouseBoundary')
    policies=file(doc,'policyZones.geojson')
    if policies is None:raise c.ControlError('SpotKeepOutGeometryRequired')
    # Conservative: reject intersection with every polygon policy zone, including
    # no-mop areas in vacuum mode. No clipping or conversion to a bounding box.
    for f in features(policies):
        g=f.get('geometry',{})
        if g.get('type') in ('Polygon','MultiPolygon','LineString','MultiLineString'):
            z=shape(g)
            if not z.is_valid:raise c.ControlError('SpotKeepOutGeometryRequired')
            if z.intersects(p):raise c.ControlError('SpotOverlapsKeepOut')
    return [list(p) for p in p.exterior.coords]


def prepare(snapshot,map_id,map_version,points,snapshot_id,mode):
    if type(snapshot_id) is not int or snapshot_id<1:raise c.ControlError('SpotSnapshotRequired')
    if not isinstance(map_id,str) or not isinstance(map_version,str):raise c.ControlError('SpotSnapshotRequired')
    maps=snapshot.get('state',{}).get('data',{}).get('p2maps') or []
    if len(maps)!=1 or not maps[0].get('p2map_id') or not maps[0].get('p2mapv_id'):raise c.ControlError('UnambiguousActiveMapRequired')
    active=maps[0]
    if active['p2map_id']!=map_id:raise c.ControlError('SpotRequiresCurrentHouseMap')
    drawn=c.read_local('/api/map/'+str(snapshot_id));meta=file(drawn,'metadata.json') or {}
    if meta.get('id')!=map_id or meta.get('version')!=map_version:raise c.ControlError('SpotSnapshotMismatch')
    ring=validate_document(drawn,points,mode)
    current=None;current_id=None
    if meta.get('version')==active['p2mapv_id']:current=drawn;current_id=snapshot_id
    else:
        for item in snapshot.get('maps',[]):
            doc=c.read_local('/api/map/'+str(item['id']));m=file(doc,'metadata.json') or {}
            if m.get('id')==map_id and m.get('version')==active['p2mapv_id']:current=doc;current_id=item['id'];break
    if current is None:raise c.ControlError('SpotCurrentMapNotRetained')
    validate_document(current,ring,mode)
    return {'map_id':map_id,'map_version':active['p2mapv_id'],'drawn_map_version':map_version,'drawn_snapshot_id':snapshot_id,
            'map_snapshot_id':current_id,'area_points':ring,'temporary_region_id':'161','boundary_verified':False}
