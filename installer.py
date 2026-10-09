"""One guided account/device setup and local agent registration. Setup never moves a robot."""
import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import tomllib

SOURCE=Path(__file__).resolve().parent
APP=Path.home()/'.local/share/vacuumgpt'
LABEL='io.vacuumgpt.observer'
NAME='vacuumgpt'
logging.disable(logging.CRITICAL)
os.umask(0o077)
sys.path.insert(0,str(SOURCE/'runtime'))
sys.path.insert(0,str(SOURCE/'runtime/source-review/prime'))

def write_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with temp.open('w') as out:
        json.dump(data,out,indent=2);out.flush();os.fsync(out.fileno())
    temp.chmod(0o600);temp.replace(path)

async def devices(email,password,country):
    import aiohttp
    from roombapy_prime.auth import login,is_prime_sku
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        account=await login(session,email,password,country)
        return [dict(sku=r.sku,name=r.name or 'Roomba',fingerprint=hashlib.sha256(blid.encode()).hexdigest()[:16])
                for blid,r in account.robots.items() if is_prime_sku(r.sku)]

def connection_config(runtime):
    return {'command':str(runtime/'.venv/bin/python'),'args':[str(runtime/'observer_mcp.py')]}

def merge_json_config(path,desired):
    old=json.loads(path.read_text()) if path.exists() else {}
    entries=old.setdefault('mcpServers',{})
    if NAME in entries and entries[NAME]!=desired:raise RuntimeError('DifferentExistingConnector')
    if entries.get(NAME)==desired:return
    if path.exists():shutil.copy2(path,path.with_name(path.name+'.roo-backup-'+str(time.time_ns())))
    entries[NAME]=desired;write_json(path,old)

def register(client,runtime):
    desired=connection_config(runtime)
    if client=='codex':
        path=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'config.toml'
        existing=tomllib.loads(path.read_text()).get('mcp_servers',{}).get(NAME) if path.exists() else None
        if existing:
            if any(existing.get(k)!=v for k,v in desired.items()):raise RuntimeError('DifferentExistingConnector')
            return
        cli=shutil.which('codex')
        if not cli:raise RuntimeError('CodexCliNotFoundUseGeneratedConfig')
        if path.exists():shutil.copy2(path,path.with_name(path.name+'.roo-backup-'+str(time.time_ns())))
        subprocess.run([cli,'mcp','add',NAME,'--',desired['command'],*desired['args']],check=True,capture_output=True)
        installed=tomllib.loads(path.read_text()).get('mcp_servers',{}).get(NAME,{})
        if any(installed.get(k)!=v for k,v in desired.items()):raise RuntimeError('ConnectorRegistrationNotConfirmed')
    elif client=='claude':
        merge_json_config(Path.home()/'Library/Application Support/Claude/claude_desktop_config.json',desired)
    elif client=='cursor':merge_json_config(Path.home()/'.cursor/mcp.json',desired)
    write_json(APP/'connector.json',{'mcpServers':{NAME:desired}})
    if client=='codex':skill_root=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'skills'
    elif client=='claude':skill_root=Path.home()/'.claude/skills'
    elif client=='cursor':skill_root=Path.home()/'.cursor/skills'
    else:return
    skill=skill_root/'vacuumgpt'
    if skill.exists():
        if (skill/'SKILL.md').read_text()!=(SOURCE/'skills/vacuumgpt/SKILL.md').read_text():raise RuntimeError('DifferentExistingRoombaSkill')
    else:shutil.copytree(SOURCE/'skills/vacuumgpt',skill)

def install_service(runtime):
    path=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        existing=plistlib.loads(path.read_bytes())
        if existing.get('ProgramArguments')!=[str(runtime/'.venv/bin/python'),str(runtime/'roomba_observer.py')]:raise RuntimeError('DifferentExistingService')
        live=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{LABEL}'],capture_output=True)
        if live.returncode:
            subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True,capture_output=True)
        return
    logs=runtime/'observer-data';logs.mkdir(mode=0o700,exist_ok=True)
    config={'Label':LABEL,'ProgramArguments':[str(runtime/'.venv/bin/python'),str(runtime/'roomba_observer.py')],
            'WorkingDirectory':str(runtime),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'Umask':63,
            'StandardOutPath':str(logs/'service.log'),'StandardErrorPath':str(logs/'service-errors.log')}
    with path.open('xb') as out:plistlib.dump(config,out)
    path.chmod(0o600)
    result=subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],capture_output=True)
    if result.returncode:
        path.unlink();raise RuntimeError('ServiceStartupFailed')

def main(uv):
    if sys.platform!='darwin' or not sys.stdin.isatty():raise RuntimeError('UseLocalMacTerminal')
    print('VacuumGPT — connect your Roomba to your agent')
    print('Your iRobot login stays in macOS Keychain. Maps and history stay on this Mac.')
    print('The connector uses the iRobot cloud. It runs while this Mac is awake and online.')
    print('Setup sends no movement commands. Supports Prime/V4 robots; Combo 105 is hardware-tested.')
    if input('Continue? [Y/n] ').strip().lower() not in ('','y','yes'):return
    runtime=APP/'runtime'
    if (runtime/'robot.json').exists():
        print('Already connected. Updating agent registration only; robot and service unchanged.')
        client=input('Agent: 1 Codex, 2 Claude Desktop, 3 Cursor, 4 Other [1]: ').strip() or '1'
        install_service(runtime)
        register({'1':'codex','2':'claude','3':'cursor','4':'other'}[client],runtime)
        return
    with socket.socket() as probe:
        try:probe.bind(('127.0.0.1',8766))
        except OSError:raise RuntimeError('Port8766InUse') from None
    if (Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist').exists():raise RuntimeError('ExistingServiceUseDoctor')
    country=input('iRobot account country [US]: ').strip().upper() or 'US'
    if len(country)!=2 or not country.isalpha():raise RuntimeError('TwoLetterCountryRequired')
    email=input('iRobot email: ').strip();password=getpass.getpass('iRobot password: ')
    found=asyncio.run(devices(email,password,country))
    if not found:raise RuntimeError('NoSupportedPrimeRobotsOnAccount')
    for i,robot in enumerate(found,1):print(f"{i}. {robot['name']} ({robot['sku']})")
    index=int(input('Select your robot [1]: ').strip() or '1')-1
    if not 0<=index<len(found):raise RuntimeError('InvalidRobotSelection')
    robot={**found[index],'port':8766}
    direct=input('Allow your agent to perform cleaning commands you request? [Y/n] ').strip().lower() in ('','y','yes')
    recover=input('Enable bounded recovery of fault-free paused missions started through VacuumGPT? [y/N] ').strip().lower() in ('y','yes') if direct else False
    client=input('Agent: 1 Codex, 2 Claude Desktop, 3 Cursor, 4 Other [1]: ').strip() or '1'
    client={'1':'codex','2':'claude','3':'cursor','4':'other'}[client]
    shutil.copytree(SOURCE/'runtime',runtime,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    # A stable venv path keeps launchd and agent configs valid after deleting the download.
    subprocess.run([uv,'venv','--python','3.12',str(runtime/'.venv')],check=True)
    subprocess.run([uv,'pip','install','--python',str(runtime/'.venv/bin/python'),'-r',str(SOURCE/'requirements.lock')],check=True)
    from roo_credentials import store
    store(email,password,country);del password,email
    write_json(runtime/'robot.json',robot)
    write_json(runtime/'activation-result.json',{'writes_enabled':direct,'write_actions':['start','stop','pause','resume','dock','find'],'robot_commands_sent':0})
    write_json(runtime/'observer-data/control-policy.json',{'owner_approved':direct,'approved_at':time.time(),'target_sku':robot['sku'],'fingerprint':robot['fingerprint'],
        'direct_chat_commands':direct,'automatic_resume':recover,'desktop_notifications':True,'recovery_pause_seconds':45,'recovery_max_attempts':2,'recovery_cooldown_seconds':180,
        'default_room_mode':'vacuum','physical_room_clean_verified':False,'chat_wake_available':False,'experimental_geometry_commands':False})
    install_service(runtime)
    write_json(APP/'connector.json',{'mcpServers':{NAME:connection_config(runtime)}})
    try:register(client,runtime)
    except Exception as exc:print('Agent registration needs attention: '+type(exc).__name__+'. Import '+str(APP/'connector.json'))
    subprocess.run(['open','http://127.0.0.1:8766/'],check=False)
    print('Installed. The observer is connecting. Restart your agent, then ask: “What is my Roomba doing?”')
    print('Doctor: '+str(runtime/'.venv/bin/python')+' '+str(runtime/'roo_cli.py')+' doctor')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--uv',required=True);args=parser.parse_args()
    try:main(args.uv)
    except Exception as exc:
        print('Setup stopped: '+type(exc).__name__+'. No secret details printed.',file=sys.stderr);sys.exit(1)
