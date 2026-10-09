import copy
import json
from unittest.mock import patch
import unittest
import test_spot
from test_spot import feature,box
import named_locations as locations
import control_layer as c

class LocationTests(unittest.TestCase):
    setUp=test_spot.SpotTests.setUp
    tearDown=test_spot.SpotTests.tearDown
    def test_exact_location_persists_and_replacement_is_one_pin(self):
        with patch('direct_control.run') as command:
            r=locations.save('map','v1',1,[1.23456789,2.3456789])
            self.assertEqual(r['location']['point'],[1.23456789,2.3456789])
            self.assertTrue(r['navigation_available']);self.assertEqual(r['location']['robot_commands_sent'],0)
            self.assertEqual(locations.get()['location'],r['location'])
            r2=locations.save('map','v1',1,[2,3]);self.assertEqual(r2['location']['revision'],2)
            with c.database() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM named_locations').fetchone()[0],1)
            command.assert_not_called()
    def test_invalid_location_never_replaces_saved_point(self):
        locations.save('map','v1',1,[1,1])
        for point in ([99,99],[float('nan'),1],[True,1],[1]):
            with self.assertRaises(c.ControlError):locations.save('map','v1',1,point)
        self.doc['files']['policyZones.geojson']['features']=[feature(box(4,4,2))]
        with self.assertRaisesRegex(c.ControlError,'LocationInsideKeepOut'):locations.save('map','v1',1,[5,5])
        with self.assertRaisesRegex(c.ControlError,'LocationRequiresCurrentHouseMap'):locations.save('wrong','v1',1,[1,1])
        with self.assertRaisesRegex(c.ControlError,'LocationSnapshotMismatch'):locations.save('map','wrong',1,[1,1])
        self.assertEqual(locations.get()['location']['point'],[1,1])
