"""Private per-user Unix socket routing commands to the observer's single MQTT session."""
import asyncio
import json
import os
from pathlib import Path
import stat
import uuid
import control_layer as c

VOICE_SHADOW_FIELDS = {
    'rw-settings': ('audio', 'langs2', 'language', 'langs'),
    'ro-configinfo': ('sku', 'softwareVer', 'soundVer', 'cap', 'langs2', 'language', 'langs'),
    'ro-services': ('services',),
}

async def read_voice_diagnostics(robot):
    """Read a fixed set of audio metadata using the existing MQTT session."""
    answer = {'read_only': True, 'mqtt_owner': 'observer_session', 'shadows': {}}
    for name, fields in VOICE_SHADOW_FIELDS.items():
        try:
            async with asyncio.timeout(10):
                response = await robot.get_named_shadow(name)
            reported = response.payload.get('state', {}).get('reported', {})
            answer['shadows'][name] = {k: reported[k] for k in fields if k in reported}
        except Exception as exc:
            answer['shadows'][name] = {'error_type': type(exc).__name__}
    return answer

async def read_navigation_diagnostics(robot):
    answer={'read_only':True,'stream_ping_failures':getattr(robot,'_live_map_ping_failures',0)}
    try:
        init=await robot.get_live_map_stream()
        expected=robot._mqtt.livemap_topic(robot._irbt_topic_prefix)
        import hashlib
        answer.update(topic_matches_subscription=init.mqtt_topic==expected,topic_fingerprint=hashlib.sha256(init.mqtt_topic.encode()).hexdigest()[:16],initial_map_available=bool(init.initial_map_url))
        if init.initial_map_url:
            import aiohttp,io,tarfile
            async with aiohttp.ClientSession() as session:
                from roomba_observer import download_map
                raw=await download_map(session,init.initial_map_url)
            with tarfile.open(fileobj=io.BytesIO(raw),mode='r:*') as bundle:
                answer['live_map_files']=[m.name for m in bundle.getmembers() if m.isfile()][:40]
                answer['pose_layers']={}
                for m in bundle.getmembers():
                    if m.isfile() and m.size<100000 and any(x in m.name.lower() for x in ('pose','traject','metadata','borders','rooms')):
                        doc=json.load(bundle.extractfile(m))
                        answer['pose_layers'][m.name]=doc
    except Exception as exc:answer.update(error_type=type(exc).__name__,http_status=getattr(exc,'status',None))
    return answer

async def start(robot,blid,raw_publish):
    path=c.ROOT/'observer-data/command.sock'
    if path.exists():
        info=path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():raise c.ControlError('UnsafeCommandSocketPath')
        path.unlink()
    active=set()
    async def handle(reader,writer):
        task=None
        try:
            raw=await asyncio.wait_for(reader.readline(),5)
            if len(raw)>256:raise c.ControlError('CommandRequestTooLarge')
            request=json.loads(raw)
            if request == {'diagnostic': 'voice_read'}:
                if active:raise c.ControlError('ObserverActorBusy')
                task=asyncio.create_task(read_voice_diagnostics(robot))
            elif request == {'diagnostic':'fault_read'}:
                if active:raise c.ControlError('ObserverActorBusy')
                from recovery_diagnostics import read
                task=asyncio.create_task(read(robot))
            elif request == {'diagnostic':'navigation_read'}:
                if active:raise c.ControlError('ObserverActorBusy')
                task=asyncio.create_task(read_navigation_diagnostics(robot))
            else:
                if not isinstance(request,dict) or set(request)!={'plan_id'}:raise c.ControlError('OnlyPlanIdAccepted')
                uuid.UUID(request['plan_id'])
                from supervised_command import execute_on_service
                task=asyncio.create_task(execute_on_service(request['plan_id'],robot,blid,raw_publish))
            active.add(task);task.add_done_callback(active.discard)
            answer=await asyncio.shield(task)
        except Exception as exc:
            answer={'error_code':str(exc) if isinstance(exc,(c.ControlError,PermissionError)) else type(exc).__name__,'may_have_executed':False}
        try:
            writer.write((json.dumps(answer)+'\n').encode());await writer.drain()
        except (ConnectionError,BrokenPipeError):pass
        finally:
            writer.close()
            try:await writer.wait_closed()
            except (ConnectionError,BrokenPipeError):pass
    server=await asyncio.start_unix_server(handle,str(path),limit=512)
    path.chmod(0o600)
    return server,path,active

async def close(server,path,active):
    server.close();await server.wait_closed()
    for task in list(active):task.cancel()
    if active:await asyncio.gather(*list(active),return_exceptions=True)
    path.unlink(missing_ok=True)
