"""Roomba MCP: SDK stdio lifecycle, bounded reads, explicit supervised command plans."""
import asyncio
import json
import logging
from pathlib import Path
import subprocess
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from typing import Annotated
import control_layer as control
from room_labels import names_by_map

ROOT=Path(__file__).resolve().parent
logging.disable(logging.CRITICAL)
mcp=FastMCP('VacuumGPT',instructions='VacuumGPT connects to the owner-selected Prime robot. Use robot_name from status for the device; never rename it. Read roomba_status first. When control_policy.direct_chat_commands is true, fulfill explicit human commands with roomba_command or roomba_clean_room and a stable unique request_id; persistent owner approval is already complete, so do not request another Terminal approval. Room cleaning requires an explicit named room; start means whole-house. Respect active missions and never silently replace one. The prepare/request-approval/control flow is only for installations without the persistent grant. Read roomba_notifications for interruptions and recovery outcomes. Never retry an uncertain command or infer coverage from travel. Automatic recovery must never resume a faulted robot or start a fresh mission. The explicitly human-confirmed inspected-brush tool permits one standard resume attempt on error2 only; it remains subject to onboard protections and does not guarantee fault clearing or saved planner continuity. For an explicitly owner-confirmed cleared route, roomba_retry_dock_after_clearance allows a bounded standard dock attempt on stuck navigation error 1010 only; remote fault clearance remains unverified. Read roomba_fault_diagnostics to distinguish fresh robot reports from cached shadow reads and inspect the device command echo. A soft reboot ends the native mission and is not an automatic recovery grant or a way to restore the onboard planner. No generic fault-clear tool exists.')
READ=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
WRITE=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True)
LOCAL=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False)
Action=Literal['start','stop','pause','resume','dock','find','clean_room']

async def safe(fn,*args):
    try:return await asyncio.to_thread(fn,*args)
    except Exception as exc:
        code=str(exc) if isinstance(exc,control.ControlError) else type(exc).__name__
        raise ToolError(code) from None

@mcp.tool(annotations=READ)
async def roomba_status()->dict:
    """Read Roomba's exact-device status, freshness, mission, map availability and room aliases."""
    data=await safe(control.read_local,'/api/status')
    data['robot_name']=__import__('roo_config').ROBOT_NAME
    data['control_policy']={**control.policy(),'supported_actions':list(control.ACTIONS),'regional_payload_supported':True}
    return data

@mcp.tool(annotations=READ)
async def roomba_fault_diagnostics()->dict:
    """Read fixed exact-device mission/error metadata, filtered ro-stats, sanitized lastCommand and recovery_assessment through the observer session. Reports whether an inspected normal resume is a candidate, without granting it. Ended cycles cannot be treated as retained missions. GET freshness alone does not prove a fresh robot report. Recognized native brush faults and motor current/speed are not exposed by these shadows; no override or unfinished-only fault recovery is verified. Never infer unfinished coverage from missionId."""
    from recovery_diagnostics import read_via_actor
    return await safe(read_via_actor)

@mcp.tool(annotations=READ)
async def roomba_recovery_status()->dict:
    """Read native recovery eligibility, durable attempts and preserved room intent. A stopped cycle or reboot cannot restore unfinished coverage; unknown pause origin and active faults block automatic resume. Queued work survives a Mac restart; possibly dispatched work is never replayed."""
    import mission_monitor
    import recovery_store
    snapshot = await safe(control.read_local, '/api/status')
    current = snapshot.get('state', {}).get('data', {})
    mission_id = current.get('cleanMissionStatus', {}).get('missionId')
    job = await safe(control.job, mission_id) if mission_id else None
    allowed, reason = await safe(mission_monitor.recovery_allowed, current, job, control.policy(), __import__('time').time())
    attempts = await safe(recovery_store.list_attempts, 20, mission_id)
    return {'robot_name': __import__('roo_config').ROBOT_NAME, 'mission_id': mission_id, 'eligible_now': allowed,
            'reason': reason, 'state_stale': snapshot.get('state_stale'),
            'job': job, 'attempts': attempts, 'native_planner_restore_supported': False,
            'unfinished_area_reconstruction_verified': False,
            'automatic_fault_reset_enabled': False}

@mcp.tool(annotations=READ)
def roomba_rooms()->dict:
    """Read map-scoped room names and verified vendor region IDs. Companion labels only."""
    return {'robot_name':__import__('roo_config').ROBOT_NAME,'fingerprint':control.FINGERPRINT,'maps':names_by_map()}

@mcp.tool(annotations=READ)
async def roomba_resolve_room(name:str,map_id:str)->dict:
    """Resolve an exact room alias within a map. Does not schedule or clean a room."""
    return await safe(control.resolve_room,name,map_id)

@mcp.tool(annotations=READ)
async def roomba_recent_trajectory(limit:Annotated[int,Field(ge=1,le=300)]=100,mission_id:str|None=None)->dict:
    """Read bounded travel and gap records; these are separate from vendor cleaning coverage."""
    rows=await safe(control.read_local,'/api/trajectory')
    filtered=[r for r in rows if mission_id is None or r.get('mission')==mission_id]
    return {'events':filtered[-limit:],'returned':min(len(filtered),limit),'matching_in_recent_window':len(filtered),'coverage_inferred':False}

@mcp.tool(annotations=READ)
async def roomba_map(id:Annotated[int,Field(ge=1)])->dict:
    """Read retained map metadata, available layer types, aliases and geometry feature counts. Full geometry is shown in the local dashboard."""
    doc=await safe(control.read_local,'/api/map/'+str(id))
    files=doc.get('files',{})
    return {'snapshot_id':id,'metadata':next((v for k,v in files.items() if k.split('/')[-1]=='metadata.json'),{}),
            'layers':[{'file':k,'type':v.get('type'),'feature_count':len(v.get('features',[]))} for k,v in files.items() if isinstance(v,dict) and v.get('type')=='FeatureCollection'],
            'room_names':doc.get('companion_room_names',{}),'dashboard_url':__import__('roo_config').BASE_URL+'/'}

@mcp.tool(annotations=LOCAL)
async def roomba_prepare_command(action:Action,room:str|None=None,mode:Literal['vacuum','vacuum_and_mop']='vacuum')->dict:
    """Prepare a basic control plan bound to Roomba's current mission and phase. Does not execute. start means whole-house cleaning."""
    plan=await safe(control.prepare,action,None,room,mode)
    return {**plan,'executed':False,'next_step':'Call roomba_request_approval with this plan_id, then roomba_control after the owner approves in local Terminal.'}

def request_approval(plan_id):
    plan=control.get_plan(plan_id)
    if control.result(plan_id):raise control.ControlError('PlanAlreadyAttempted')
    # Generated wrapper contains only fixed workspace paths and a validated UUID; no credentials.
    import shlex
    wrapper=ROOT/'observer-data'/('approve-'+plan_id+'.command')
    wrapper.write_text('#!/bin/zsh\n'+shlex.join([str(ROOT/'.venv/bin/python'),str(ROOT/'write_approval.py'),plan_id])+"\nread -r 'roo_done?Press Enter to close: '\n")
    wrapper.chmod(0o700)
    process=subprocess.run(['/usr/bin/open','-a','Terminal',str(wrapper)],capture_output=True,timeout=10)
    if process.returncode:raise control.ControlError('LocalApprovalWindowUnavailable')
    return {'plan_id':plan_id,'action':plan['action'],'approval_window_opened':True,'executed':False,'approval_required':True}

@mcp.tool(annotations=LOCAL)
async def roomba_request_approval(plan_id:str)->dict:
    """Open the owner's local Terminal approval window for one prepared plan. This never grants approval or sends a robot command."""
    return await safe(request_approval,plan_id)

def run_command(plan_id):
    return control.launch_writer(plan_id)

@mcp.tool(annotations=WRITE)
async def roomba_control(plan_id:str)->dict:
    """Execute one owner-approved command plan. Durable duplicate suppression; no blind retry. PUBACK does not prove the robot acted. Approval expires in 120 seconds."""
    answer=await safe(run_command,plan_id)
    if answer.get('stage')=='failed' or answer.get('error_code')=='WriterTimeout':
        raise ToolError(json.dumps(answer))
    return answer

@mcp.tool(annotations=READ)
async def roomba_command_result(plan_id:str)->dict:
    """Read durable command outcome after cancellation, timeout or reconnection; never resend."""
    await safe(control.get_plan,plan_id,True)
    answer=await safe(control.result,plan_id) or {'plan_id':plan_id,'attempted':False}
    answer['audit']=await safe(control.audit,plan_id)
    return answer

@mcp.tool(annotations=READ)
async def roomba_command_history(limit:Annotated[int,Field(ge=1,le=100)]=20)->dict:
    """Read bounded supervised command audit history. Contains no credentials."""
    return {'attempts':await safe(control.history,limit)}

@mcp.tool(annotations=WRITE)
async def roomba_clean_room(room:str,request_id:str,mode:Literal['default','vacuum','vacuum_and_mop']='default')->dict:
    """Clean exactly the user-requested named room after the persistent owner grant. Use one unique request_id per human request and reuse it on transport retries. No whole-house fallback. Never retry unknown delivery with a new ID."""
    from direct_control import run
    return await safe(run,'clean_room',request_id,room,mode)

@mcp.tool(annotations=WRITE)
async def roomba_command(action:Literal['start','stop','pause','resume','dock','find'],request_id:str)->dict:
    """Send the explicitly user-requested control under the persistent owner grant. start means WHOLE HOUSE and requires that explicit intent. pause/stop/dock inhibit recovery. Resume preserves the same mission; never start a new mission to recover."""
    from direct_control import run
    return await safe(run,action,request_id)

@mcp.tool(annotations=WRITE)
async def roomba_retry_dock_after_clearance(request_id:str,path_clear_confirmed:bool=False)->dict:
    """Explicit owner-requested standard dock retry while navigation fault 1010 remains set. Require the human to confirm the route is clear and dock is accessible. This sends dock, not a fault-clear/reset command; acceptance on faulted Roomba is UNVERIFIED. Only fresh stuck/1010 with no readiness blockers; one attempt per mission, 180-second cooldown, at most two in six hours. Never use for brush, wheel, cliff, lift or heat faults. Reuse request_id to inspect the result, never retry unknown delivery."""
    if path_clear_confirmed is not True:return {'error_code':'ExplicitOwnerPathClearConfirmationRequired','may_have_executed':False}
    from direct_control import run
    return await safe(run,'dock',request_id,owner_navigation_retry=True)

@mcp.tool(annotations=WRITE)
async def roomba_retry_resume_after_brush_inspection(request_id:str,brush_checked_free:bool=False)->dict:
    """One standard resume attempt only after the human confirms main brush inspection finds no obstruction. Exact fresh stuck error2 with a complete retained clean-cycle anchor, zero readiness blockers, battery>=25, one claim per mission and at most one in six hours. Ended/none cycles are refused before publish to prevent a new whole-house clean. It does not reset/clear errors or bypass onboard protection. Acceptance and saved planner continuity are unverified until same mission runs fault-free. Never fallback to start; unknown delivery is never retried."""
    if brush_checked_free is not True:return {'error_code':'ExplicitPhysicalBrushInspectionRequired','may_have_executed':False}
    from direct_control import run
    return await safe(run,'resume',request_id,owner_brush_retry=True)

@mcp.tool(annotations=READ)
async def roomba_notifications(after:Annotated[int,Field(ge=0)]=0,limit:Annotated[int,Field(ge=1,le=100)]=50)->dict:
    """Read durable Roomba interruption/fault/recovery events. Save next_cursor to avoid duplicate notices. This tool does not wake an idle chat by itself."""
    return await safe(control.notices,after,limit)

@mcp.tool(annotations=WRITE)
async def roomba_clean_rooms(rooms:list[str],request_id:str,ordered:bool=True,mode:Literal['default','vacuum','vacuum_and_mop']='default',suction:Annotated[int,Field(ge=1,le=4)]|None=None,passes:Annotated[int,Field(ge=1,le=2)]|None=None)->dict:
    """Clean selected rooms in one mission with requested order, suction and one/two passes. Hardware adherence to order/settings remains unverified. Respect active missions; never substitute whole-house cleaning."""
    from direct_control import run
    return await safe(run,'clean_room',request_id,None,mode,rooms,ordered,suction,passes)

@mcp.tool(annotations=LOCAL)
async def roomba_create_schedule(rooms:list[str],local_time:str,weekdays:list[int],timezone_name:str,ordered:bool=True,mode:Literal['default','vacuum','vacuum_and_mop']='default',suction:int|None=None,passes:int|None=None)->dict:
    """Save an explicitly requested local recurring room clean. weekdays: 0 Monday through 6 Sunday; HH:MM in the user's IANA timezone. Runs require this Mac awake. Busy/faulted/stale/map-changed and missed runs are skipped, never replace a mission. Do not invent a schedule."""
    import local_schedules
    return await safe(local_schedules.create,rooms,local_time,weekdays,timezone_name,mode,ordered,suction,passes)

@mcp.tool(annotations=READ)
async def roomba_schedules()->dict:
    """List local room schedules; does not create or run one."""
    import local_schedules
    return {'schedules':await safe(local_schedules.list_schedules)}

@mcp.tool(annotations=LOCAL)
async def roomba_set_schedule_enabled(schedule_id:str,enabled:bool)->dict:
    """Enable or pause an existing schedule at the owner's request."""
    import local_schedules
    return await safe(local_schedules.set_enabled,schedule_id,enabled)

@mcp.tool(annotations=LOCAL)
async def roomba_delete_schedule(schedule_id:str)->dict:
    """Delete a requested local schedule, preserving execution history."""
    import local_schedules
    return await safe(local_schedules.delete,schedule_id)

@mcp.tool(annotations=LOCAL)
async def roomba_save_area(name:str,map_id:str,map_version:str,points:list[list[float]])->dict:
    """Save a map-bound polygon draft in original vendor coordinates. Sends no robot command. Ad-hoc cleaning is gated until exact-device boundary behavior is verified; never replace an area with an entire room."""
    import area_selection
    return await safe(area_selection.save,name,map_id,map_version,points)

@mcp.tool(annotations=READ)
async def roomba_areas()->dict:
    """Read saved map-bound area drafts and their dispatch availability."""
    import area_selection
    return {'areas':await safe(area_selection.list_areas)}

@mcp.tool(annotations=READ)
async def roomba_garbage_location()->dict:
    """Read the owner-placed garbage-can approach point in vendor map coordinates. Navigation remains unverified; reading sends no robot command."""
    import named_locations
    return await safe(named_locations.get)

@mcp.tool(annotations=READ)
async def roomba_stuck_reviews()->dict:
    """Read persistent low-motion reviews. More than 20 seconds near one point triggers review, not an automatic permanent ban."""
    import stationary_review
    return {'reviews':await safe(stationary_review.reviews)}

@mcp.tool(annotations=WRITE)
async def roomba_review_stuck(incident_id:str,classification:Literal['temporary','permanent','not_stuck'])->dict:
    """Record the user's obstacle decision. Only an explicit permanent classification sends one additive keep-out edit; preserve existing boundaries, report verification honestly, never retry unknown delivery."""
    import stationary_review
    return await safe(stationary_review.classify,incident_id,classification)

@mcp.resource('roomba://notifications')
def notification_resource()->str:
    return json.dumps(control.notices())

@mcp.tool(annotations=LOCAL)
async def roomba_name_room(map_id:str,region_id:str,name:str)->dict:
    """Save an owner-requested local room alias after validating current vendor geometry. Does not edit the vendor map or move the robot."""
    from room_labels import save_alias
    return await safe(save_alias,map_id,region_id,name)

if __name__=='__main__':mcp.run(transport='stdio')
