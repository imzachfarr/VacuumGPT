"""Compare native target geometry without treating map uploads as room edits."""
import contextlib
import hashlib
import io
import json
import math
import sqlite3
import tarfile
import control_layer as c


def _geometry(value):
    if isinstance(value, dict):
        return {key: _geometry(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [_geometry(child) for child in value]
    if type(value) in (int, float):
        if not math.isfinite(value): raise c.ControlError('InvalidRecoveryMapGeometry')
        return round(value, 6)
    return value


def context(doc, region_ids=None):
    files = doc.get('files', {})
    meta = next((v for k, v in files.items() if k.split('/')[-1] == 'metadata.json'), {})
    if not meta.get('id') or not meta.get('version'):
        raise c.ControlError('RecoveryMapMetadataRequired')
    wanted = None if region_ids is None else {str(x) for x in region_ids}
    selected = []
    policies = []
    room_layer = False
    policy_layer = False
    for filename, layer in files.items():
        stem = filename.split('/')[-1].lower()
        if stem not in ('rooms.geojson', 'policyzones.geojson'): continue
        if not isinstance(layer, dict) or not isinstance(layer.get('features'), list):
            raise c.ControlError('IncompleteRecoveryMapLayer')
        if stem == 'rooms.geojson': room_layer = True
        else: policy_layer = True
        for feature in layer['features']:
            fid = str(feature.get('id'))
            if stem == 'rooms.geojson' and wanted is not None and fid not in wanted: continue
            geo = feature.get('geometry')
            supported = ('Polygon', 'MultiPolygon') if stem == 'rooms.geojson' else ('Polygon', 'MultiPolygon', 'LineString', 'MultiLineString')
            if not isinstance(geo, dict) or geo.get('type') not in supported or not geo.get('coordinates'):
                raise c.ControlError('InvalidRecoveryMapGeometry')
            row = {'id': fid, 'geometry': _geometry(geo)}
            if stem == 'policyzones.geojson':
                row['properties'] = _geometry(feature.get('properties', {}))
                policies.append(row)
            else: selected.append(row)
    if not room_layer or not policy_layer or not selected or wanted is not None and {r['id'] for r in selected} != wanted:
        raise c.ControlError('RecoveryMapTargetIncomplete')
    value = {'map_id': meta['id'], 'source_format': meta.get('sourceFormat'),
             'coordinate_frame': {k: meta[k] for k in ('coordinateFrame', 'origin', 'resolution') if k in meta},
             'rooms': sorted(selected, key=lambda x: x['id']), 'policy_zones': sorted(policies, key=lambda x: x['id'])}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'map_id': meta['id'], 'version': meta['version'], 'topology_hash': digest,
            'region_ids': sorted(r['id'] for r in selected)}


def retained(map_id, version, region_ids=None):
    database = c.ROOT / 'observer-data/observer.sqlite3'
    if not database.exists(): return None
    with contextlib.closing(sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT filename FROM maps ORDER BY id DESC').fetchall()
    folder = (c.ROOT / 'observer-data').resolve()
    for row in rows:
        path = (folder / row[0]).resolve()
        if folder not in path.parents: raise c.ControlError('InvalidRetainedMapPath')
        doc = json.loads(path.read_bytes())
        meta = next((v for k, v in doc.get('files', {}).items() if k.split('/')[-1] == 'metadata.json'), {})
        if meta.get('id') == map_id and meta.get('version') == version:
            return context(doc, region_ids)
    return None


def from_plan(plan):
    if not plan.get('map_id') or not plan.get('map_version'): return None
    ids = plan.get('region_ids') or ([plan['region_id']] if plan.get('region_id') else None)
    return retained(plan['map_id'], plan['map_version'], ids)


def _bundle(data):
    if len(data) > 32 * 1024 * 1024: raise c.ControlError('RecoveryMapTooLarge')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        members = archive.getmembers()
        if len(members) > 500: raise c.ControlError('RecoveryMapTooManyMembers')
        total = 0
        for member in members:
            if not member.isfile(): continue
            total += member.size
            if total > 64 * 1024 * 1024: raise c.ControlError('RecoveryMapExpandedTooLarge')
            if member.name.split('/')[-1].lower() not in ('metadata.json', 'rooms.geojson', 'policyzones.geojson'): continue
            files[member.name] = json.loads(archive.extractfile(member).read())
    return {'files': files}


async def current(robot, reported, region_ids=None):
    maps = reported.get('p2maps') or []
    if len(maps) != 1 or not maps[0].get('p2map_id') or not maps[0].get('p2mapv_id'):
        raise c.ControlError('UnambiguousRecoveryMapRequired')
    active = maps[0]
    value = retained(active['p2map_id'], active['p2mapv_id'], region_ids)
    if value is not None: return value
    # Exact-device authenticated GET; URL credentials remain in memory only.
    from roomba_observer import download_map
    link = await robot.get_map_geojson_link(active['p2map_id'], active['p2mapv_id'])
    doc = _bundle(await download_map(robot._rest._session, link['map_url']))
    value = context(doc, region_ids)
    if value['map_id'] != active['p2map_id'] or value['version'] != active['p2mapv_id']:
        raise c.ControlError('RecoveryMapDownloadIdentityMismatch')
    return value
