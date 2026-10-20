"""Support for SNMP Network Manager device trackers."""
import logging
import re
from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import device_registry as dr
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .topology import get_device_by_mac, format_friendly_name

_LOGGER = logging.getLogger(__name__)


_SWITCH_DEVICE_ID_CACHE: dict[str, str] = {}


def _find_switch_device_id(hass: HomeAssistant, switch_name: str) -> str | None:
    """Find the device ID of the parent switch in the device registry with caching."""
    if not switch_name or switch_name == "Unknown":
        return None
        
    normalized_switch_name = switch_name.lower().replace("-", "").replace(" ", "").replace("_", "")
    if normalized_switch_name in _SWITCH_DEVICE_ID_CACHE:
        return _SWITCH_DEVICE_ID_CACHE[normalized_switch_name]
        
    dev_reg = dr.async_get(hass)
    for dev in dev_reg.devices:
        if dev.name:
            norm_name = dev.name.lower().replace("-", "").replace(" ", "").replace("_", "")
            if normalized_switch_name == norm_name:
                for identifier in dev.identifiers:
                    if identifier[0] in ("snmp_switch_manager", "snmp_network_manager"):
                        # Cache it
                        _SWITCH_DEVICE_ID_CACHE[normalized_switch_name] = dev.id
                        return dev.id
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SNMP device tracker platform."""
    added_macs = set()

    # We do not proactively purge duplicate SNMP trackers so they remain registered under our integration.

    @callback
    def async_add_tracker(devices: list[dict]):
        """Add new device trackers dynamically when discovered."""
        new_entities = []
        dev_reg = dr.async_get(hass)
        for dev in devices:
            mac = dev["mac"].upper()
            if mac in added_macs:
                continue
            added_macs.add(mac)
            
            # Resolve name from registry to override old cached '10' names or generic IPs
            name = dev.get("name") or mac
            device_entry = get_device_by_mac(dev_reg, mac)
                
            if device_entry:
                potential_name = device_entry.name_by_user or device_entry.name
                if potential_name and not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', potential_name):
                    name = potential_name
                    
                # We register the SNMP device tracker even if another tracker already tracks it.
                
            if name == "10" or name == "192" or re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', name):
                name = dev.get("name") or mac
                
            dev["name"] = name
            new_entities.append(SnmpDeviceTracker(entry.entry_id, dev))
        if new_entities:
            async_add_entities(new_entities)

    # Listen for new devices discovered by the topology engine
    new_device_signal = f"snmp_network_manager_new_device_{entry.entry_id}"
    entry.async_on_unload(
        async_dispatcher_connect(hass, new_device_signal, async_add_tracker)
    )

    # Pre-populate device trackers from the last cached snapshot if available
    engine = hass.data[DOMAIN].get(entry.entry_id)
    if engine:
        try:
            snapshot = await engine.get_last_snapshot()
            if snapshot and snapshot.get("graph", {}).get("nodes"):
                initial_devices = []
                for node_id, node in snapshot["graph"]["nodes"].items():
                    if node.get("kind") == "endpoint" and "mac" in node.get("attrs", {}):
                        attrs = node["attrs"]
                        # Find which switch this endpoint is connected to by checking edges
                        switch_name = "Unknown"
                        port_name = "Unknown"
                        for edge in snapshot["graph"]["edges"]:
                            if edge.get("src") == node_id:
                                dst_node = snapshot["graph"]["nodes"].get(edge.get("dst"))
                                if dst_node:
                                    switch_name = dst_node.get("attrs", {}).get("name", "Unknown")
                                port_name = edge.get("attrs", {}).get("bridge_port", "Unknown")
                                break
                        
                        initial_devices.append({
                            "mac": attrs["mac"],
                            "name": attrs.get("name"),
                            "ip": attrs.get("ip", "Unknown"),
                            "switch_name": switch_name,
                            "port_name": port_name
                        })
                if initial_devices:
                    async_add_tracker(initial_devices)
        except Exception as e:
            _LOGGER.error("Failed to load initial device trackers from snapshot: %s", e)


class SnmpDeviceTracker(TrackerEntity):
    """Representation of a device tracked via SNMP."""

    def __init__(self, entry_id: str, device_info_dict: dict):
        """Initialize the tracker."""
        self._entry_id = entry_id
        self._mac = device_info_dict["mac"].upper()
        self._name = device_info_dict.get("name") or self._mac
        self._ip = device_info_dict.get("ip", "Unknown")
        self._switch_name = device_info_dict.get("switch_name", "Unknown")
        self._port_name = device_info_dict.get("port_name", "Unknown")
        self._state = "home"
        
        self._attr_unique_id = f"{entry_id}_tracker_{self._mac.replace(':', '').lower()}"
        self._attr_name = self._name

    @property
    def source_type(self) -> SourceType:
        """Return the source type of the device."""
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        """Return true if the device is connected to the network."""
        return self._state == "home"

    @property
    def extra_state_attributes(self) -> dict:
        """Return entity specific state attributes."""
        return {
            "mac": self._mac,
            "ip": self._ip,
            "switch": self._switch_name,
            "port": self._port_name,
        }

    @property
    def device_info(self) -> dr.DeviceInfo:
        """Link this entity to its device registry entry."""
        connections = {(dr.CONNECTION_NETWORK_MAC, self._mac.lower())}
        identifiers = {(DOMAIN, self._mac.lower())}
        
        info = dr.DeviceInfo(
            connections=connections,
            identifiers=identifiers,
            name=self._name,
        )
        
        switch_dev_id = _find_switch_device_id(self.hass, self._switch_name)
        if switch_dev_id:
            info["via_device_id"] = switch_dev_id
                    
        return info

    async def async_added_to_hass(self) -> None:
        """Register dispatcher updates and sanitize registry name."""
        await super().async_added_to_hass()
        
        # Format registry name dynamically (e.g., alexa-bridge -> Alexa Bridge)
        dev_reg = dr.async_get(self.hass)
        device = get_device_by_mac(dev_reg, self._mac)
            
        if device:
            registry_name = device.name_by_user or device.name
            is_mac_name = not registry_name or bool(re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', registry_name))
            
            if is_mac_name and self._name and not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', self._name):
                friendly_name = format_friendly_name(self._name)
                _LOGGER.info("Updating registry device name from '%s' to '%s'", registry_name, friendly_name)
                dev_reg.async_update_device(device.id, name=friendly_name)
            else:
                current_name = registry_name or self._name
                if current_name:
                    friendly_name = format_friendly_name(current_name)
                    if friendly_name != current_name and not device.name_by_user:
                        _LOGGER.info("Sanitizing registry name from '%s' to '%s'", current_name, friendly_name)
                        dev_reg.async_update_device(device.id, name=friendly_name)
                    else:
                        friendly_name = current_name
                    
                self._name = friendly_name
                self._attr_name = friendly_name
        
        @callback
        def async_update_state(devices: dict[str, dict]):
            """Update the tracker's status and properties dynamically."""
            dev = devices.get(self._mac)
            if dev:
                dev_state = dev.get("state", "down")
                if dev_state in ["down", "away"]:
                    self._state = "not_home"
                else:
                    self._state = "home"
                self._ip = dev.get("ip", self._ip)
                
                # Only update switch/port if they are active, preserving last known parent on offline
                new_switch = dev.get("switch_name", "Unknown")
                if new_switch != "Unknown":
                    self._switch_name = new_switch
                new_port = dev.get("port_name", "Unknown")
                if new_port != "Unknown":
                    self._port_name = new_port
                    
                if dev.get("name"):
                    self._name = dev["name"]
                    self._attr_name = dev["name"]
                    
                    # Update device registry name if it is a MAC address or if the DNS name has changed and the user hasn't overridden it
                    device = get_device_by_mac(dev_reg, self._mac)
                    if device:
                        registry_name = device.name_by_user or device.name
                        is_mac_name = not registry_name or bool(re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', registry_name))
                        
                        if is_mac_name:
                            if not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', dev["name"]):
                                _LOGGER.info("Updating registry device name on state update from '%s' to '%s'", registry_name, dev["name"])
                                dev_reg.async_update_device(device.id, name=dev["name"])
                        elif not device.name_by_user and dev["name"]:
                            # Verify new name is not a MAC or IP address
                            is_mac_or_ip = bool(re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', dev["name"])) or dev["name"] == dev.get("ip")
                            if not is_mac_or_ip:
                                friendly_name = format_friendly_name(dev["name"])
                                if friendly_name and friendly_name != device.name:
                                    _LOGGER.info(
                                        "Updating registry device name due to DNS change from '%s' to '%s'",
                                        device.name,
                                        friendly_name,
                                    )
                                    dev_reg.async_update_device(device.id, name=friendly_name)
            else:
                self._state = "not_home"
            self.async_write_ha_state()

        update_signal = f"snmp_network_manager_update_device_{self._entry_id}"
        self.async_on_remove(
            async_dispatcher_connect(self.hass, update_signal, async_update_state)
        )
