"""Local diagnostics and explicit connector removal. Never moves the robot."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
from roo_config import ROOT,BASE_URL,TARGET,FINGERPRINT

def doctor():
    import control_layer as c
    report={'configured':TARGET!='UNCONFIGURED','sku':TARGET,'dashboard':BASE_URL+'/', 'robot_commands_sent':0}
    try:
        state=c.read_local('/api/status')
        identity=(state.get('identity') or {}).get('data',{})
        report.update(observer_reachable=True,connected=state.get('connection',{}).get('data',{}).get('status')=='connected',
            state_stale=state.get('state_stale'),identity_matches=identity.get('device_fingerprint')==FINGERPRINT,
            mapped_rooms=sum(len(rooms) for rooms in state.get('room_names_by_map',{}).values()))
    except Exception as exc:report.update(observer_reachable=False,error_type=type(exc).__name__)
    print(json.dumps(report,indent=2))
    return 0 if report.get('connected') and not report.get('state_stale') and report.get('identity_matches') else 1

def uninstall(delete_credentials=False):
    if input('Remove the VacuumGPT connector and stop its observer? [y/N] ').strip().lower() not in ('y','yes'):return
    label='io.vacuumgpt.observer'
    subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],capture_output=True)
    (Path.home()/'Library/LaunchAgents'/f'{label}.plist').unlink(missing_ok=True)
    if delete_credentials:
        from roo_credentials import delete
        delete()
    print('Observer stopped. Maps and history remain in '+str(ROOT/'observer-data'))
    print('Remove vacuumgpt from your agent MCP settings and remove its vacuumgpt skill. Other connectors are untouched.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['doctor','uninstall']);parser.add_argument('--delete-credentials',action='store_true');args=parser.parse_args()
    raise SystemExit(doctor() if args.action=='doctor' else uninstall(args.delete_credentials))
