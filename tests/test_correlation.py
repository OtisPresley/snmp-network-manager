import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock voluptuous
mock_vol = MagicMock()
mock_vol.Required = lambda x, **kwargs: x
mock_vol.Optional = lambda x, **kwargs: x
mock_vol.Any = lambda *x, **kwargs: MagicMock()
mock_vol.Coerce = lambda x: x
sys.modules['voluptuous'] = mock_vol

# Define mock classes to put in sys.modules before any imports
class MockHomeAssistant:
    def __init__(self):
        self.states = MagicMock()
        self.config = MagicMock()
        self.config.components = []
    
    def is_running(self) -> bool:
        return True

    async def async_add_executor_job(self, target, *args):
        return target(*args)

class MockStore:
    def __init__(self, *args, **kwargs):
        pass
    async def async_load(self):
        return {}
    async def async_save(self, data):
        pass

mock_dr = MagicMock()
mock_er = MagicMock()

mock_storage = MagicMock()
mock_storage.Store = MockStore

mock_cv = MagicMock()
mock_cv.config_entry_only_config_schema = lambda domain: MagicMock()

# Setup modules using types.ModuleType for full Python import machinery compatibility
ha = types.ModuleType("homeassistant")
ha_core = types.ModuleType("homeassistant.core")
ha_core.HomeAssistant = MockHomeAssistant

ha_components = types.ModuleType("homeassistant.components")
ha_components_ws = types.ModuleType("homeassistant.components.websocket_api")
ha_components_ws.websocket_command = lambda schema: lambda f: f
ha_components_ws.async_response = lambda f: f
ha_components_ws.ActiveConnection = MagicMock

ha_components_http = types.ModuleType("homeassistant.components.http")
ha_components_http.StaticPathConfig = MagicMock

ha_config_entries = types.ModuleType("homeassistant.config_entries")
ha_config_entries.ConfigEntry = MagicMock

ha_helpers = types.ModuleType("homeassistant.helpers")
ha_helpers.device_registry = mock_dr
ha_helpers.entity_registry = mock_er
ha_helpers.storage = mock_storage
ha_helpers.config_validation = mock_cv

ha_helpers_dr = types.ModuleType("homeassistant.helpers.device_registry")
ha_helpers_er = types.ModuleType("homeassistant.helpers.entity_registry")
ha_helpers_storage = types.ModuleType("homeassistant.helpers.storage")
ha_helpers_storage.Store = MockStore
ha_helpers_cv = mock_cv

sys.modules['homeassistant'] = ha
sys.modules['homeassistant.core'] = ha_core
sys.modules['homeassistant.components'] = ha_components
sys.modules['homeassistant.components.websocket_api'] = ha_components_ws
sys.modules['homeassistant.components.http'] = ha_components_http
sys.modules['homeassistant.config_entries'] = ha_config_entries
sys.modules['homeassistant.helpers'] = ha_helpers
sys.modules['homeassistant.helpers.device_registry'] = ha_helpers_dr
sys.modules['homeassistant.helpers.entity_registry'] = ha_helpers_er
sys.modules['homeassistant.helpers.storage'] = ha_helpers_storage
sys.modules['homeassistant.helpers.config_validation'] = ha_helpers_cv

from custom_components.snmp_network_manager.topology import (  # noqa: E402
    find_correlated_device,
    get_all_device_entities,
    resolve_device_state,
)

class MockDevice:
    def __init__(self, id, name, connections=None, identifiers=None, manufacturer=None, model=None, composite_device_id=None, composite_primary_config_entry=None):
        self.id = id
        self.name = name
        self.connections = connections or set()
        self.identifiers = identifiers or set()
        self.manufacturer = manufacturer
        self.model = model
        self.config_entries = set()
        self.name_by_user = None
        self.composite_device_id = composite_device_id
        self.composite_primary_config_entry = composite_primary_config_entry

class MockEntity:
    def __init__(self, entity_id, device_id, platform, unique_id, domain=None, device_class=None):
        self.entity_id = entity_id
        self.device_id = device_id
        self.platform = platform
        self.unique_id = unique_id
        self.domain = domain or entity_id.split(".")[0]
        self.device_class = device_class

class MockDeviceRegistryItems(dict):
    def __iter__(self):
        return iter(self.values())


class TestCorrelation(unittest.TestCase):
    def setUp(self):
        self.hass = MockHomeAssistant()
        
        # Reset mock registries
        self.devices = MockDeviceRegistryItems()
        self.entities = {}
        
        # Configure registry mocks
        self.dev_reg = MagicMock()
        self.dev_reg.devices = self.devices
        self.dev_reg.async_get = lambda dev_id: self.devices.get(dev_id)
        self.dev_reg.async_get_devices_for_composite_device_id = lambda comp_id: [d for d in self.devices.values() if getattr(d, 'composite_device_id', None) == comp_id or d.id == comp_id]
        mock_dr.async_get.return_value = self.dev_reg
        
        self.ent_reg = MagicMock()
        self.ent_reg.entities = self.entities
        self.ent_reg.async_get = lambda ent_id: self.entities.get(ent_id)
        self.ent_reg.async_entries_for_device = lambda did, include_disabled_entities=False: [e for e in self.entities.values() if e.device_id == did]
        mock_er.async_entries_for_device = lambda ent_reg, did, include_disabled_entities=False: [e for e in self.entities.values() if e.device_id == did]
        mock_er.async_get.return_value = self.ent_reg

    def test_correlation_by_mac_connection(self):
        # Device with MAC connection
        device = MockDevice("dev_1", "Test Device", connections={("mac", "aa:bb:cc:dd:ee:ff")})
        self.devices["dev_1"] = device
        
        res = find_correlated_device(self.hass, "aa:bb:cc:dd:ee:ff", "192.168.1.50", "Test-Device")
        self.assertEqual(res, device)

    def test_correlation_by_mac_identifier(self):
        # Device with MAC identifier
        device = MockDevice("dev_2", "Test Device 2", identifiers={("unifi", "aa:bb:cc:dd:ee:02")})
        self.devices["dev_2"] = device
        
        res = find_correlated_device(self.hass, "aa:bb:cc:dd:ee:02", "192.168.1.51", "Test-Device-2")
        self.assertEqual(res, device)

    def test_correlation_by_name_exact(self):
        # Device with matching normalized name
        device = MockDevice("dev_3", "My Living Room Speaker")
        self.devices["dev_3"] = device
        
        res = find_correlated_device(self.hass, None, None, "my-living-room-speaker")
        self.assertEqual(res, device)

    def test_dynamic_heuristic_substring_match(self):
        # Device representing a cloud device with a serial number in identifiers
        # E.g. Ecobee thermostat with identifier "123456789012"
        ecobee_device = MockDevice(
            "dev_ecobee", 
            "Downstairs Thermostat", 
            identifiers={("ecobee", "123456789012")}
        )
        self.devices["dev_ecobee"] = ecobee_device
        
        # SNMP name contains the serial number: "ecobee-123456789012"
        res = find_correlated_device(self.hass, None, None, "ecobee-123456789012")
        self.assertEqual(res, ecobee_device)

    def test_dynamic_heuristic_short_identifier_ignored(self):
        # Short identifier that could cause false positives (e.g. "123")
        false_device = MockDevice(
            "dev_false", 
            "False Match Device", 
            identifiers={("other_integration", "123")}
        )
        self.devices["dev_false"] = false_device
        
        res = find_correlated_device(self.hass, None, None, "ecobee-123")
        self.assertIsNone(res)

    def test_composite_device_resolution(self):
        # An empty split child device matched by MAC
        child_dev = MockDevice(
            "child_dev",
            "Hallway 1",
            identifiers={("snmp_network_manager", "78:1c:3c:d0:21:b4"), ("xsense", "SERIAL123")},
            manufacturer="X-Sense",
            model="SC07-WX",
            composite_device_id="comp_123",
            composite_primary_config_entry="entry_xsense",
        )
        child_dev.config_entries = {"entry_unifi"}
        
        # Primary device holding the actual entities
        primary_dev = MockDevice(
            "primary_dev",
            "Hallway 1",
            identifiers={("xsense", "SERIAL123")},
            manufacturer="X-Sense",
            model="SC07-WX",
            composite_device_id="comp_123",
            composite_primary_config_entry="entry_xsense",
        )
        primary_dev.config_entries = {"entry_xsense"}
        
        self.devices["child_dev"] = child_dev
        self.devices["primary_dev"] = primary_dev
        
        # Add entities to primary device
        battery_ent = MockEntity("sensor.hallway_1_battery", "primary_dev", "xsense", "u_batt", device_class="battery")
        self.entities[battery_ent.entity_id] = battery_ent
        
        # Correlating by MAC should return the primary device with entities
        res = find_correlated_device(self.hass, "78:1c:3c:d0:21:b4", "10.0.12.155", "Hallway 1")
        self.assertEqual(res.id, "primary_dev")
        
        # And get_all_device_entities should include entities from primary_dev
        all_ents = get_all_device_entities(self.ent_reg, self.dev_reg, child_dev)
        self.assertEqual(len(all_ents), 1)
        self.assertEqual(all_ents[0].entity_id, "sensor.hallway_1_battery")

    def test_resolve_device_state_battery_device(self):
        # Battery-powered device tracked by HA as available
        dev = MockDevice("dev_battery", "Hallway 1")
        battery_ent = MockEntity("sensor.hallway_1_battery", "dev_battery", "xsense", "u_batt", device_class="battery")
        
        mock_batt_state = MagicMock()
        mock_batt_state.state = "100"
        mock_batt_state.attributes = {"device_class": "battery"}
        self.hass.states.get = lambda eid: mock_batt_state
        
        state = resolve_device_state(self.hass, dev, [battery_ent], "down")
        self.assertEqual(state, "asleep")

    def test_resolve_device_state_unavailable_device(self):
        # Device whose entities are all unavailable in HA
        dev = MockDevice("dev_unavail", "Disconnected Device")
        unavail_ent = MockEntity("sensor.test_sensor", "dev_unavail", "test", "u_unavail")
        
        mock_unavail_state = MagicMock()
        mock_unavail_state.state = "unavailable"
        mock_unavail_state.attributes = {}
        self.hass.states.get = lambda eid: mock_unavail_state
        
        state = resolve_device_state(self.hass, dev, [unavail_ent], "down")
        self.assertEqual(state, "down")

    def test_resolve_device_state_standby_device(self):
        # TV / media player reporting off / standby in HA
        dev = MockDevice("dev_tv", "Living Room TV")
        tv_ent = MockEntity("media_player.living_room_tv", "dev_tv", "sony", "u_tv")
        
        mock_tv_state = MagicMock()
        mock_tv_state.state = "off"
        mock_tv_state.attributes = {}
        self.hass.states.get = lambda eid: mock_tv_state
        
        state = resolve_device_state(self.hass, dev, [tv_ent], "down")
        self.assertEqual(state, "asleep")

    def test_resolve_device_state_active_ac_device(self):
        # Smart switch / plug actively on in HA
        dev = MockDevice("dev_plug", "Kitchen Plug")
        plug_ent = MockEntity("switch.kitchen_plug", "dev_plug", "tplink", "u_plug")
        
        mock_plug_state = MagicMock()
        mock_plug_state.state = "on"
        mock_plug_state.attributes = {}
        self.hass.states.get = lambda eid: mock_plug_state
        
        state = resolve_device_state(self.hass, dev, [plug_ent], "down")
        self.assertEqual(state, "up")

    def test_correlation_waterguru_avoid_hacs(self):
        # Dummy HACS repository device with exact name "WaterGuru"
        hacs_dev = MockDevice("hacs_dev", "WaterGuru", identifiers={("hacs", "123456")})
        # Real hardware device with manufacturer "WaterGuru" and name "WaterGuru Pool"
        hw_dev = MockDevice(
            "waterguru_pool",
            "WaterGuru Pool",
            identifiers={("waterguru", "pod_uuid_123")},
            manufacturer="WaterGuru",
            model="SENSE",
        )
        self.devices["hacs_dev"] = hacs_dev
        self.devices["waterguru_pool"] = hw_dev

        # Endpoint with name "WaterGuru" should ignore the HACS device and match the real pool hardware device
        matched = find_correlated_device(self.hass, None, None, "WaterGuru")
        self.assertIsNotNone(matched)
        self.assertEqual(matched.id, "waterguru_pool")

        # Battery entity on the hardware device
        battery_ent = MockEntity("sensor.waterguru_pool_battery", "waterguru_pool", "waterguru", "u_bat", device_class="battery")
        mock_state = MagicMock()
        mock_state.state = "85"
        mock_state.attributes = {"device_class": "battery", "battery_level": 85}
        self.hass.states.get = lambda eid: mock_state

        # When offline on SNMP, the battery device should resolve to "asleep"
        state = resolve_device_state(self.hass, matched, [battery_ent], "down")
        self.assertEqual(state, "asleep")

    def test_correlation_lock_fuzzy_matching(self):
        # Hardware lock device in HA named "Front Door"
        lock_dev = MockDevice(
            "front_door_lock",
            "Front Door",
            identifiers={("schlage", "schlage_uuid_123")},
            manufacturer="Schlage",
            model="be499WB2",
        )
        self.devices["front_door_lock"] = lock_dev

        # Network node named "Front Door Lock" should match HA device "Front Door"
        matched = find_correlated_device(self.hass, None, None, "Front Door Lock")
        self.assertIsNotNone(matched)
        self.assertEqual(matched.id, "front_door_lock")

    def test_mobile_phone_online_on_wifi(self):
        # Mobile phone connected to Wi-Fi
        phone_dev = MockDevice("dev_phone", "Jamie's Phone", model="Pixel 10 Pro XL")
        gps_tracker = MockEntity("device_tracker.jamie_s_phone", "dev_phone", "mobile_app", "u_gps")
        wifi_sensor = MockEntity("sensor.jamie_s_phone_wi_fi_connection", "dev_phone", "mobile_app", "u_wifi")

        def mock_get(eid):
            m = MagicMock()
            if eid == "device_tracker.jamie_s_phone":
                m.state = "home"
                m.attributes = {"source_type": "gps"}
            elif eid == "sensor.jamie_s_phone_wi_fi_connection":
                m.state = "otispresley.com"
                m.attributes = {}
            return m

        self.hass.states.get = mock_get
        state = resolve_device_state(self.hass, phone_dev, [gps_tracker, wifi_sensor], "up")
        self.assertEqual(state, "up")

    def test_mobile_phone_away_when_wifi_off_on_cellular_at_home(self):
        # Phone at home via GPS, but Wi-Fi turned off (<not connected>) and on cellular
        phone_dev = MockDevice("dev_phone", "Jamie's Phone", model="Pixel 10 Pro XL")
        gps_tracker = MockEntity("device_tracker.jamie_s_phone", "dev_phone", "mobile_app", "u_gps")
        wifi_sensor = MockEntity("sensor.jamie_s_phone_wi_fi_connection", "dev_phone", "mobile_app", "u_wifi")
        net_sensor = MockEntity("sensor.jamie_s_phone_network_type", "dev_phone", "mobile_app", "u_net")

        def mock_get(eid):
            m = MagicMock()
            if eid == "device_tracker.jamie_s_phone":
                m.state = "home"
                m.attributes = {"source_type": "gps"}
            elif eid == "sensor.jamie_s_phone_wi_fi_connection":
                m.state = "<not connected>"
                m.attributes = {}
            elif eid == "sensor.jamie_s_phone_network_type":
                m.state = "cellular"
                m.attributes = {}
            return m

        self.hass.states.get = mock_get
        # Even if SNMP cache is down or up, phone reports disconnected Wi-Fi / cellular -> away
        state = resolve_device_state(self.hass, phone_dev, [gps_tracker, wifi_sensor, net_sensor], "down")
        self.assertEqual(state, "away")

    def test_mobile_phone_away_via_router_tracker(self):
        # Phone at home via GPS, but UniFi router tracker shows not_home
        phone_dev = MockDevice("dev_phone", "Jamie's Phone", model="Pixel 10 Pro XL")
        gps_tracker = MockEntity("device_tracker.jamie_s_phone", "dev_phone", "mobile_app", "u_gps")
        unifi_tracker = MockEntity("device_tracker.pixel_10_pro_xl_2", "dev_phone", "unifi", "u_unifi")

        def mock_get(eid):
            m = MagicMock()
            if eid == "device_tracker.jamie_s_phone":
                m.state = "home"
                m.attributes = {"source_type": "gps"}
            elif eid == "device_tracker.pixel_10_pro_xl_2":
                m.state = "not_home"
                m.attributes = {"source_type": "router"}
            return m

        self.hass.states.get = mock_get
        state = resolve_device_state(self.hass, phone_dev, [gps_tracker, unifi_tracker], "down")
        self.assertEqual(state, "away")

    def test_mobile_phone_away_via_gps(self):
        # Phone physically away from home (GPS not_home)
        phone_dev = MockDevice("dev_phone", "Jamie's Phone", model="Pixel 10 Pro XL")
        gps_tracker = MockEntity("device_tracker.jamie_s_phone", "dev_phone", "mobile_app", "u_gps")

        def mock_get(eid):
            m = MagicMock()
            if eid == "device_tracker.jamie_s_phone":
                m.state = "not_home"
                m.attributes = {"source_type": "gps"}
            return m

        self.hass.states.get = mock_get
        state = resolve_device_state(self.hass, phone_dev, [gps_tracker], "down")
        self.assertEqual(state, "away")

    def test_mobile_phone_down_when_unavailable(self):
        # Phone completely unavailable (powered off / dead battery)
        phone_dev = MockDevice("dev_phone", "Jamie's Phone", model="Pixel 10 Pro XL")
        gps_tracker = MockEntity("device_tracker.jamie_s_phone", "dev_phone", "mobile_app", "u_gps")

        def mock_get(eid):
            m = MagicMock()
            m.state = "unavailable"
            m.attributes = {"source_type": "gps"}
            return m

        self.hass.states.get = mock_get
        state = resolve_device_state(self.hass, phone_dev, [gps_tracker], "down")
        self.assertEqual(state, "down")

    def test_read_local_arp_executor(self):
        import asyncio
        from unittest.mock import patch, mock_open
        from custom_components.snmp_network_manager.topology import read_local_arp

        sample_arp = "IP address HW type Flags HW address Mask Device\n10.0.12.50 0x1 0x2 AA:BB:CC:DD:EE:FF * eth0\n"
        with patch("builtins.open", mock_open(read_data=sample_arp)):
            arp = asyncio.run(self.hass.async_add_executor_job(read_local_arp))
            self.assertEqual(arp.get("AA:BB:CC:DD:EE:FF"), "10.0.12.50")


if __name__ == "__main__":
    unittest.main()



