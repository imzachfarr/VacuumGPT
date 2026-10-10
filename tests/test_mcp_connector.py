"""Source-only MCP discovery and grant refusal, with all network IO blocked."""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PACKAGE = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = frozenset(('area_selection', 'chair_keepout', 'command_actor',
    'control_layer', 'dashboard_commands', 'direct_control', 'garbage_navigation',
    'local_schedules', 'map_recovery', 'mission_monitor', 'named_locations',
    'observer_mcp', 'observer_store', 'recovery_context', 'recovery_diagnostics',
    'recovery_store', 'roo_cli', 'roo_config', 'roo_credentials', 'room_labels',
    'roomba_observer', 'soft_reboot', 'spot_control', 'stationary_review',
    'supervised_command', 'write_approval'))


def copy_source_runtime(source, destination):
    """Copy approved code paths, never an installation's config or state."""
    destination.mkdir()
    paths = [source / (name + '.py') for name in RUNTIME_MODULES]
    vendor = source / 'source-review/prime/roombapy_prime'
    paths.extend(path for path in vendor.rglob('*') if path.is_file()
                 and '__pycache__' not in path.parts and
                 (path.suffix == '.py' or path.name in ('py.typed', 'vendor_reference.json')))
    paths.append(source / 'source-review/prime/LICENSE')
    for path in paths:
        if path.is_file():
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


class ConnectorTests(unittest.TestCase):
    def test_source_copy_excludes_identity_state_and_private_artifacts(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            folder = Path(folder)
            source = folder / 'source'
            source.mkdir()
            (source / 'observer_mcp.py').write_text('# fixture source only\n')
            for name in ('robot.json', 'activation-result.json', '.write-approval.json',
                         'observer-data/control.sqlite3', 'output/fault.json',
                         '.venv/private.py', 'private_probe.py'):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic private fixture')
            destination = folder / 'isolated'
            copy_source_runtime(source, destination)
            copied = {path.relative_to(destination).as_posix() for path in destination.rglob('*') if path.is_file()}
            self.assertEqual(copied, {'observer_mcp.py'})

    def test_stdio_discovery_local_rooms_and_unapproved_command_refusal(self):
        async def check():
            with tempfile.TemporaryDirectory(prefix='vacuumgpt-mcp-test-', dir='/tmp') as folder:
                folder = Path(folder)
                runtime = folder / 'runtime'
                copy_source_runtime(PACKAGE / 'runtime', runtime)
                (runtime / 'robot.json').write_text(json.dumps({'sku': 'OFFLINE-FIXTURE',
                    'fingerprint': '0000000000000000', 'name': 'Offline fixture robot', 'port': 0}))
                (runtime / 'activation-result.json').write_text(json.dumps({
                    'writes_enabled': False, 'write_actions': []}))
                data = runtime / 'observer-data'
                data.mkdir()
                (data / 'control-policy.json').write_text(json.dumps({
                    'direct_chat_commands': False, 'automatic_resume': False}))
                marker = folder / 'network-attempted'
                launcher = folder / 'isolated_server.py'
                launcher.write_text('''import pathlib, runpy, socket, sys
runtime = pathlib.Path(sys.argv[1])
marker = pathlib.Path(sys.argv[2])
def deny_network(*args, **kwargs):
    marker.write_text("blocked network attempt")
    raise AssertionError("NetworkForbiddenInOfflineMcpTest")
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.getaddrinfo = deny_network
sys.path[:0] = [str(runtime), str(runtime / "source-review/prime")]
import control_layer
def fixture_status(route):
    if route != "/api/status":
        raise AssertionError("UnexpectedOfflineStatusRoute")
    marker.with_name("fixture-status-read").write_text(route)
    return {"target_sku": control_layer.TARGET, "state_stale": False,
        "connection": {"data": {"status": "connected"}},
        "state": {"data": {"batPct": 100, "cleanMissionStatus": {
            "phase": "idle", "cycle": "none", "error": 0, "notReady": 0,
            "condNotReady": [], "missionId": "offline-fixture", "mssnStrtTm": 0,
            "nMssn": 0, "operatingMode": 2}}}}
control_layer.read_local = fixture_status
runpy.run_path(str(runtime / "observer_mcp.py"), run_name="__main__")
''')
                params = StdioServerParameters(command=sys.executable,
                    args=[str(launcher), str(runtime), str(marker)], cwd=str(runtime),
                    env={'PYTHONDONTWRITEBYTECODE': '1',
                         'PYTHONPATH': str(runtime) + ':' + str(runtime / 'source-review/prime')})
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        listing = await session.list_tools()
                        names = {tool.name for tool in listing.tools}
                        self.assertTrue({'roomba_status', 'roomba_name_room', 'roomba_command',
                            'roomba_clean_room', 'roomba_create_schedule', 'roomba_fault_diagnostics',
                            'roomba_recovery_status', 'roomba_retry_resume_after_brush_inspection'} <= names)
                        status = await session.call_tool('roomba_status', {})
                        self.assertFalse(status.isError)
                        status_data = status.structuredContent or json.loads(status.content[0].text)
                        self.assertEqual(status_data['robot_name'], 'Offline fixture robot')
                        self.assertEqual(status_data['target_sku'], 'OFFLINE-FIXTURE')
                        self.assertFalse(status_data['control_policy']['direct_chat_commands'])
                        rooms = await session.call_tool('roomba_rooms', {})
                        self.assertFalse(rooms.isError)
                        result = rooms.structuredContent or json.loads(rooms.content[0].text)
                        self.assertEqual(result['robot_name'], 'Offline fixture robot')
                        self.assertEqual(result['maps'], {})
                        blocked = await session.call_tool('roomba_command', {
                            'action': 'start', 'request_id': 'offline-package-test'})
                        self.assertTrue(blocked.isError)
                        self.assertIn('PersistentRoomControlGrantRequired',
                            '\n'.join(item.text for item in blocked.content if item.type == 'text'))
                self.assertFalse(marker.exists(), 'The isolated MCP attempted network access')
                self.assertEqual(marker.with_name('fixture-status-read').read_text(), '/api/status')
                self.assertFalse((data / 'command.sock').exists())
                self.assertEqual(json.loads((runtime / 'robot.json').read_text())['port'], 0)
        asyncio.run(check())
