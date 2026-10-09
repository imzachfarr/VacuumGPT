"""One user-requested additive chair keep-out edit, over HTTPS only."""
import asyncio,contextlib,hashlib,json,math,time,uuid
from pathlib import Path
import aiohttp
import control_layer as c
from roomba_observer import credentials,download_map
from observer_store import Store,sanitize
from roombapy_prime.auth import login
from roombapy_prime.prime_factory import PrimeFactory
from roombapy_prime.models.geometry import Polygon
from roombapy_prime.models.map_bundle import PolicyZoneFeature
from roombapy_prime.models.map_editing import SetVirtualWallsV1,VirtualWallRectangleV1,policy_zone_to_virtual_wall,MapEditResult

def walls_from(files,detail):
    policy=next((v for k,v in files.items() if k.split('/')[-1]=='policyZones.geojson'),None)
    if policy is None:
        if detail.get('geojson_details',{}).get('zones')!=[]:raise c.ControlError('CompleteExistingBoundariesRequired')
        return []
    walls=[]
    for feature in policy.get('features',[]):
        typed=PolicyZoneFeature.from_json(feature)
        wall=policy_zone_to_virtual_wall(typed)
        if wall is None:raise c.ControlError('UnrecognizedPolicyZoneCannotBeDropped')
        geometry=feature.get('geometry',{})
        if geometry.get('type')=='Polygon':
            rings=geometry.get('coordinates',[])
            if len(rings)!=1 or len(rings[0]) not in (4,5):raise c.ControlError('ExistingBoundaryNotLosslesslyRepresentable')
        elif geometry.get('type')=='LineString':
            if len(geometry.get('coordinates',[]))!=2:raise c.ControlError('ExistingBoundaryNotLosslesslyRepresentable')
        else:raise c.ControlError('UnsupportedExistingBoundary')
        walls.append(wall)
    return walls

def same_wall(a,b):
    return len(a)==len(b) and a[:2]==b[:2] and all(abs(x-y)<=.0001 for x,y in zip(a[2:],b[2:]))

async def bundle(robot,session,mid,version):
    detail=await robot._rest.get_map_version(mid,version)
    link=await robot.get_map_geojson_link(mid,version)
    data=await download_map(session,link['map_url']);store=Store(c.ROOT/'observer-data')
    store.map_bundle(data,'chair_keepout_verification')
    digest=json.loads(store.db.execute("SELECT data FROM events WHERE kind='map_retained' ORDER BY id DESC LIMIT 1").fetchone()[0])['digest']
    row=store.db.execute('SELECT filename FROM maps WHERE digest=?',(digest,)).fetchone()
    files=json.loads((c.ROOT/'observer-data'/row[0]).read_text())['files'];store.db.close()
    return files,detail

async def execute(plan):
    email,password,country=credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        account=await login(session,email,password,country)
        ids=[b for b,r in account.robots.items() if r.sku==c.TARGET and hashlib.sha256(b.encode()).hexdigest()[:16]==c.FINGERPRINT]
        if len(ids)!=1:raise c.ControlError('ExactDeviceRequired')
        robot=await PrimeFactory.create_prime_robot(session,email,password,country,blid=ids[0],login_result=account,auto_refresh=False)
        maps=await robot.get_active_map_versions();active=next(m for m in maps if m.get('p2map_id')==plan['map_id'])
        version=active['active_p2mapv_id'];files,detail=await bundle(robot,session,plan['map_id'],version)
        walls=walls_from(files,detail);existing=[w.to_json() for w in walls]
        ids_used={str(w.wall_id) for w in walls};identifier=next(str(n) for n in range(1,10000) if str(n) not in ids_used)
        added=VirtualWallRectangleV1(identifier,Polygon(coordinates=[plan['ring']]))
        cmd=SetVirtualWallsV1(walls=walls+[added]);body={'edit_cmd':cmd.to_v1_command_body(),'response_type':'link'}
        c.record(plan['plan_id'],'map_edit_prepared',{'action':'keep_out','map_id':plan['map_id'],'map_version':version,'existing_boundaries':existing,'added_boundary':added.to_json(),'may_have_executed':False})
        original=robot._rest._request;sent=False
        allowed_url=robot._rest._http_base_auth+'/v1/p2maps/'+plan['map_id']+'/versions'
        async def guarded(method,url,*args,**kwargs):
            nonlocal sent
            if method=='GET':return await original(method,url,*args,**kwargs)
            if method!='POST' or url!=allowed_url or kwargs.get('body')!=body or sent:raise c.ControlError('UnapprovedMapEditBlocked')
            sent=True;c.record(plan['plan_id'],'dispatching',{'action':'keep_out','outcome':'unknown','may_have_executed':True,'existing_boundaries_count':len(walls),'added_boundary':added.to_json()})
            kwargs['_retry']=False
            return await original(method,url,*args,**kwargs)
        robot._rest._request=guarded
        response=await robot.edit_map(plan['map_id'],cmd)
        parsed=MapEditResult.from_json(response)
        if parsed.is_error:
            c.record(plan['plan_id'],'complete',{'action':'keep_out','outcome':'provider_rejected','error_code':parsed.error_code,'may_have_executed':True,'provider_acknowledged':False,'keepout_verified':False})
            return c.result(plan['plan_id'])
        new_version=parsed.map_version_id
        if not new_version:
            maps=await robot.get_active_map_versions();new_version=next(m['active_p2mapv_id'] for m in maps if m.get('p2map_id')==plan['map_id'])
        after_files,after_detail=await bundle(robot,session,plan['map_id'],new_version)
        after=[w.to_json() for w in walls_from(after_files,after_detail)]
        verified=any(same_wall(added.to_json(),w) for w in after) and all(any(same_wall(w,v) for v in after) for w in existing)
        c.record(plan['plan_id'],'complete',{'action':'keep_out','outcome':'verified' if verified else 'accepted_unverified','provider_acknowledged':True,'keepout_verified':verified,'may_have_executed':True,'map_version':new_version,'added_boundary_id':identifier,'existing_boundaries_preserved':all(any(same_wall(w,v) for v in after) for w in existing),'center':plan['center'],'half_width_map_units':plan['half_width'],'mqtt_connections_opened':0,'movement_commands_sent':0})
        return c.result(plan['plan_id'])

def ban_point(center,map_id,owner_request):
    x,y=center;h=.25
    if not all(type(v) in (int,float) and math.isfinite(v) for v in (x,y)):raise c.ControlError('FiniteCoordinatesRequired')
    plan={'plan_id':str(uuid.uuid4()),'action':'keep_out','target_sku':c.TARGET,'fingerprint':c.FINGERPRINT,'map_id':map_id,
        'center':[x,y],'half_width':h,'ring':[[x-h,y-h],[x+h,y-h],[x+h,y+h],[x-h,y+h],[x-h,y-h]],
        'created_at':time.time(),'expires_at':time.time()+120,'owner_request':owner_request}
    with contextlib.closing(c.database()) as db,db:db.execute('INSERT INTO plans VALUES(?,?,?)',(plan['plan_id'],plan['created_at'],json.dumps(plan)))
    with c.exclusive_writer():
        try:return asyncio.run(execute(plan))
        except Exception as exc:
            prior=c.result(plan['plan_id']) or {}
            c.record(plan['plan_id'],'failed',{'action':'keep_out','error_type':type(exc).__name__,'may_have_executed':prior.get('may_have_executed',False),'keepout_verified':False,'outcome':'unknown' if prior.get('may_have_executed') else 'refused'})
            return c.result(plan['plan_id'])

def main():
    path=c.ROOT/'chair-keepout-plan.json'
    if path.exists():plan=json.loads(path.read_text())
    else:
        capture=json.loads((c.ROOT/'chair-location-capture.json').read_text())
        if capture.get('pose_stale') or capture.get('state_stale'):raise c.ControlError('FreshCapturedLocationRequired')
        pose=capture['pose']['data'];point=pose['points'][-1];x,y=point['x'],point['y'];h=.25
        if not all(math.isfinite(v) for v in (x,y)):raise c.ControlError('FiniteCoordinatesRequired')
        plan={'plan_id':str(uuid.uuid4()),'action':'keep_out','target_sku':c.TARGET,'fingerprint':c.FINGERPRINT,'map_id':pose['map_id'],'captured_map_version':pose['map_version'],'center':[x,y],'half_width':h,'ring':[[x-h,y-h],[x+h,y-h],[x+h,y+h],[x-h,y+h],[x-h,y-h]],'created_at':time.time(),'expires_at':time.time()+1800,'owner_request':'Ban this exact chair location'}
        path.write_text(json.dumps(plan,indent=2));path.chmod(0o600)
        with contextlib.closing(c.database()) as db,db:db.execute('INSERT INTO plans VALUES(?,?,?)',(plan['plan_id'],plan['created_at'],json.dumps(plan)))
    with c.exclusive_writer():
        prior=c.result(plan['plan_id'])
        if prior and prior.get('may_have_executed'):answer={**prior,'duplicate_request':True}
        else:
            try:answer=asyncio.run(execute(plan))
            except Exception as exc:
                import traceback
                locations=[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
                prior=c.result(plan['plan_id']) or {};c.record(plan['plan_id'],'failed',{'action':'keep_out','error_type':type(exc).__name__,'error_locations':locations,'may_have_executed':prior.get('may_have_executed',False),'outcome':'unknown' if prior.get('may_have_executed') else 'refused','keepout_verified':False});answer=c.result(plan['plan_id'])
    (c.ROOT/'chair-keepout-result.json').write_text(json.dumps(answer,indent=2));print(json.dumps(answer,indent=2))
if __name__=='__main__':main()
