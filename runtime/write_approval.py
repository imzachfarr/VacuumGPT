from roo_config import TARGET, ROBOT_NAME
"""Short-lived, one-use, local approval for a specific action and mission."""
import json
import os
from pathlib import Path
import time
import uuid
import stat
from control_layer import ACTIONS, get_plan, prepare, ControlError

ROOT=Path(__file__).resolve().parent
PENDING=ROOT/".write-approval.json"

def plan_ticket(plan):
    uuid.UUID(plan['plan_id'])
    folder=ROOT/'observer-data/approvals';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    return folder/(plan['plan_id']+'.json')

def consume(action, plan=None):
    receipt=ROOT/"activation-result.json"
    setup=json.loads(receipt.read_text()) if receipt.exists() else {}
    if not setup.get("writes_enabled"):
        raise PermissionError("ControlledWriteSetupNotApproved")
    pending=plan_ticket(plan) if plan and plan_ticket(plan).exists() else PENDING
    claimed=pending.parent/f".write-approval-consumed-{uuid.uuid4()}.json"
    try:
        os.rename(pending,claimed)
    except FileNotFoundError:
        raise PermissionError("LocalActionApprovalRequired") from None
    try:
        info=claimed.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
            raise PermissionError('UnsafeApprovalFile')
        item=json.loads(claimed.read_text())
    finally:
        claimed.unlink(missing_ok=True)
    now=time.time()
    one_reboot = (action=='soft_reboot' and plan and plan.get('one_use_soft_reboot') is True and
        str(item.get('explicit_owner_request','')).startswith('owner-requested one-use soft reboot:'))
    if action not in setup.get('write_actions',[]) and not one_reboot and not (action in ('clean_room','clean_area','drive_garbage') and item.get('explicit_owner_request')):
        raise PermissionError('RegionalScopeApprovalRequired')
    if item.get("action")!=action or item.get("target_sku")!=TARGET or item.get("expires_at",0)<now or not 0<=now-item.get('approved_at',0)<=120:
        raise PermissionError("ApprovalMismatchOrExpired")
    if plan and (item.get('plan_id')!=plan['plan_id'] or item.get('intent_hash')!=plan['intent_hash'] or item.get('fingerprint')!=plan['fingerprint'] or item.get('mission_id')!=plan['mission_id']):
        raise PermissionError('ApprovalIntentMismatch')
    return item

def grant_explicit_request(plan,request_reference):
    """Issue one short-lived intent-bound ticket after a direct human action request.

    Called by trusted local execution or a persistently owner-approved chat policy,
    never by observer HTTP, vendor data, or a model-supplied approval flag.
    """
    if not request_reference:raise PermissionError('HumanRequestReferenceRequired')
    data={**plan,'approved_at':time.time(),'expires_at':time.time()+120,
          'explicit_owner_request':request_reference}
    temp=ROOT/f'.write-approval-new-{uuid.uuid4()}.json'
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:
        json.dump(data,output);output.flush();os.fsync(output.fileno())
    os.replace(temp,plan_ticket(plan))

def authorize(plan_id=None):
    import sys
    import urllib.request
    if not sys.stdin.isatty():
        raise SystemExit("Approve a specific action in your local Terminal.")
    receipt=ROOT/"activation-result.json"
    if not receipt.exists() or not json.loads(receipt.read_text()).get("writes_enabled"):
        raise SystemExit("Complete permanent controlled-access activation first.")
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(__import__('roo_config').BASE_URL+'/api/status',timeout=5) as response:
        status=json.load(response)
    if status.get("state_stale") or status.get("connection",{}).get("data",{}).get("status")!="connected":
        raise SystemExit("A fresh exact-device state is required before approval.")
    mission=status.get("state",{}).get("data",{}).get("cleanMissionStatus",{})
    plan=get_plan(plan_id) if plan_id else None
    action=plan['action'] if plan else input("Choose the specific action to approve (start / stop / pause / resume / dock / find): ").strip()
    if action not in ACTIONS:
        raise SystemExit("Choose a supported basic control.")
    plan=plan or prepare(action,status)
    if plan['mission_id']!=mission.get('missionId') or plan['phase']!=mission.get('phase'):
        raise ControlError('RobotStateChangedSincePlan')
    if mission.get('error') and action in ('start','resume','dock'):
        raise SystemExit('Movement approval refused while a robot fault is active.')
    print(f"Target: {ROBOT_NAME} ({TARGET}). Mission: {mission.get('missionId')}. Phase: {mission.get('phase')}.")
    print('Effect: '+plan['effect']+'; plan '+plan['plan_id'])
    print("Start/resume/dock cause movement; pause/stop interrupt cleaning; find sounds a chime. Supervise the robot.")
    print("This approval permits one attempt in the next 2 minutes for this same mission.")
    if input(f"Type {action.upper()} {TARGET} NOW to authorize: ")!=f"{action.upper()} {TARGET} NOW":
        return
    data={**plan,"approved_at":time.time(),"expires_at":time.time()+120,'explicit_owner_request':'native exact action approval:'+plan['plan_id']}
    temp=ROOT/f'.write-approval-new-{uuid.uuid4()}.json'
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:json.dump(data,output)
    os.replace(temp,plan_ticket(plan))
    print('One-use approval ready for plan '+plan['plan_id'])

if __name__=='__main__':
    import sys
    try:authorize(sys.argv[1] if len(sys.argv)==2 else None)
    except Exception as exc:print("Approval failed: "+type(exc).__name__)
