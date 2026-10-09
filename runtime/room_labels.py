"""Map-scoped aliases sourced from retained vendor rooms, with local owner overrides."""
import contextlib
import json
from pathlib import Path
import sqlite3
from roo_config import FINGERPRINT
ROOT=Path(__file__).resolve().parent

def names_by_map():
    folder=ROOT/'observer-data'
    path=folder/'room-labels.json'
    aliases=json.loads(path.read_text()).get('robots',{}).get(FINGERPRINT,{}).get('maps',{}) if path.exists() else {}
    database=folder/'observer.sqlite3'
    maps={}
    if database.exists():
        with contextlib.closing(sqlite3.connect(f'file:{database}?mode=ro',uri=True)) as db:
            for filename,layers in db.execute('SELECT filename,layers FROM maps ORDER BY id DESC LIMIT 30'):
                doc=json.loads((folder/filename).read_text())
                files=doc.get('files',{})
                meta=next((v for k,v in files.items() if k.split('/')[-1]=='metadata.json'),{})
                map_id=meta.get('id')
                if not map_id or map_id in maps:continue
                rooms={}
                for layer in json.loads(layers):
                    if layer.get('type')!='rooms' or not layer.get('retained'):continue
                    for feature in files.get(layer['filepath'],{}).get('features',[]):
                        rid=str(feature.get('id',''))
                        if not rid or feature.get('geometry',{}).get('type') not in ('Polygon','MultiPolygon'):continue
                        name=feature.get('properties',{}).get('name') or 'Room '+rid
                        rooms[rid]={'name':name,'vendor_region_id':rid}
                maps[map_id]=rooms
    for map_id,rooms in aliases.items():maps.setdefault(map_id,{}).update(rooms)
    return maps

def save_alias(map_id,region_id,name):
    import os
    from control_layer import ControlError,read_local,validate_retained_region
    if not isinstance(name,str) or not name.strip() or len(name)>80:raise ControlError('RoomNameRequired')
    state=read_local('/api/status')
    active=state.get('state',{}).get('data',{}).get('p2maps') or []
    if state.get('state_stale') or len(active)!=1 or active[0].get('p2map_id')!=map_id:raise ControlError('FreshActiveMapRequired')
    validate_retained_region(state,map_id,active[0]['p2mapv_id'],str(region_id))
    rooms=names_by_map().get(map_id,{})
    if any(str(rid)!=str(region_id) and room['name'].casefold()==name.strip().casefold() for rid,room in rooms.items()):raise ControlError('DuplicateRoomName')
    path=ROOT/'observer-data/room-labels.json'
    data=json.loads(path.read_text()) if path.exists() else {'robots':{}}
    selected=data.setdefault('robots',{}).setdefault(FINGERPRINT,{}).setdefault('maps',{}).setdefault(map_id,{})
    selected[str(region_id)]={'name':name.strip(),'vendor_region_id':str(region_id)}
    temp=path.with_suffix('.tmp')
    with temp.open('w') as out:json.dump(data,out);out.flush();os.fsync(out.fileno())
    temp.chmod(0o600);temp.replace(path)
    return {'map_id':map_id,**selected[str(region_id)],'robot_commands_sent':0}

def room_name(map_id,region_id,vendor_name=None):
    return names_by_map().get(map_id,{}).get(str(region_id),{}).get('name') or vendor_name
