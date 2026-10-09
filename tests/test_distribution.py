import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import control_layer as c
import room_labels

PACKAGE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('roo_installer',PACKAGE/'installer.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class DistributionTests(unittest.TestCase):
    def test_account_discovery_selects_prime_without_robot_creation(self):
        from roombapy_prime import auth,prime_factory
        account=SimpleNamespace(robots={'fake-prime':SimpleNamespace(name='My robot',sku='Y311620'),'fake-classic':SimpleNamespace(name='Older robot',sku='R980020')})
        with patch.object(auth,'login',AsyncMock(return_value=account)),patch.object(prime_factory.PrimeFactory,'create_prime_robot') as create:
            found=asyncio.run(installer.devices('fixture','fixture','US'))
        create.assert_not_called();self.assertEqual(len(found),1);self.assertEqual(found[0]['name'],'My robot');self.assertNotIn('blid',found[0])
    def test_json_registration_preserves_other_connectors_and_backs_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'mcp.json';path.write_text(json.dumps({'theme':'dark','mcpServers':{'other':{'command':'other'}}}))
            desired={'command':'/fixture/python','args':['/fixture/server.py']}
            installer.merge_json_config(path,desired)
            result=json.loads(path.read_text());self.assertEqual(result['theme'],'dark');self.assertEqual(result['mcpServers']['other'],{'command':'other'})
            self.assertEqual(len(list(Path(directory).glob('*roo-backup*'))),1)
            installer.merge_json_config(path,desired)
            with self.assertRaisesRegex(RuntimeError,'DifferentExistingConnector'):installer.merge_json_config(path,{'command':'different'})
            self.assertEqual(json.loads(path.read_text()),result)
    def test_public_policy_blocks_experimental_movement_before_status_read(self):
        with patch.object(c,'policy',return_value={}),patch.object(c,'read_local') as read:
            for action in ('clean_area','drive_garbage'):
                with self.assertRaisesRegex(c.ControlError,'ExperimentalGeometryCommandsDisabled'):c.prepare(action)
            read.assert_not_called()
    def test_room_alias_rejects_missing_current_geometry_without_saving(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(room_labels,'ROOT',Path(directory)),patch.object(c,'read_local',return_value={'state_stale':True}):
            with self.assertRaises(c.ControlError):room_labels.save_alias('map','10','Kitchen')
            self.assertFalse((Path(directory)/'observer-data/room-labels.json').exists())
    def test_room_alias_persists_for_selected_robot_only(self):
        state={'state_stale':False,'state':{'data':{'p2maps':[{'p2map_id':'my-map','p2mapv_id':'v1'}]}}}
        with tempfile.TemporaryDirectory() as directory,patch.object(room_labels,'ROOT',Path(directory)),patch.object(room_labels,'FINGERPRINT','my-fingerprint'),patch.object(c,'read_local',return_value=state),patch.object(c,'validate_retained_region') as validate:
            (Path(directory)/'observer-data').mkdir()
            result=room_labels.save_alias('my-map','7','Kitchen')
            self.assertEqual(result['robot_commands_sent'],0);validate.assert_called_once_with(state,'my-map','v1','7')
            self.assertEqual(room_labels.names_by_map()['my-map']['7']['name'],'Kitchen')
            with patch.object(room_labels,'FINGERPRINT','other-fingerprint'):self.assertEqual(room_labels.names_by_map(),{})
    def test_guided_install_saves_selected_identity_without_credentials_in_files(self):
        import roo_credentials
        with tempfile.TemporaryDirectory() as directory:
            app=Path(directory)/'app'
            answers=iter(['','US','fixture@example.invalid','1','','n','4'])
            with patch.object(installer,'APP',app),patch.object(installer.sys.stdin,'isatty',return_value=True),patch('builtins.input',side_effect=lambda _:next(answers)),patch.object(installer.getpass,'getpass',return_value='fixture-secret-password'),patch.object(installer,'devices',AsyncMock(return_value=[{'sku':'Y311620','fingerprint':'fixture-fingerprint','name':'Own robot'}])),patch.object(roo_credentials,'store') as store,patch.object(installer,'install_service') as service,patch.object(installer,'register') as register,patch.object(installer.subprocess,'run') as process:
                installer.main('/fixture/uv')
            store.assert_called_once_with('fixture@example.invalid','fixture-secret-password','US')
            service.assert_called_once();register.assert_called_once()
            self.assertFalse(json.loads((app/'runtime/observer-data/control-policy.json').read_text())['automatic_resume'])
            identity=json.loads((app/'runtime/robot.json').read_text());self.assertEqual(identity['fingerprint'],'fixture-fingerprint')
            connector=json.loads((app/'connector.json').read_text());self.assertIn(str(app/'runtime'),connector['mcpServers']['vacuumgpt']['command'])
            for path in app.rglob('*.json'):self.assertNotIn('fixture-secret-password',path.read_text())
            self.assertEqual(process.call_count,3)
