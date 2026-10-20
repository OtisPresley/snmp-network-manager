"""Support for SNMP Network Manager sensors."""
import logging
import os

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .topology import TopologyEngine

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    from homeassistant.helpers import entity_registry as er
    ent_reg = er.async_get(hass)
    for entity_entry in list(ent_reg.entities.values()):
        if entity_entry.entity_id.startswith("sensor.snmp_network_topology_"):
            try:
                _LOGGER.info("Cleaning up duplicate topology sensor entity from registry: %s", entity_entry.entity_id)
                ent_reg.async_remove(entity_entry.entity_id)
            except Exception as e:
                _LOGGER.warning("Failed to clean up duplicate entity %s: %s", entity_entry.entity_id, e)

    engine = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SnmpNetworkTopologySensor(hass, engine, entry.entry_id)])


class SnmpNetworkTopologySensor(SensorEntity):
    """Sensor that exposes the topology snapshot status."""

    _attr_name = "SNMP Network Topology"

    def __init__(self, hass: HomeAssistant, engine: TopologyEngine, entry_id: str):
        """Initialize the sensor."""
        self.hass = hass
        self.engine = engine
        self._entry_id = entry_id
        self._attr_unique_id = "snmp_network_topology_sensor"
        self._attr_native_value = "Initialized"
        self._attr_extra_state_attributes = {}
        self._update_task = None

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await super().async_added_to_hass()
        
        # Load the last snapshot from store to set the initial state quickly
        try:
            snapshot = await self.engine.get_last_snapshot()
            if snapshot and snapshot.get("graph", {}).get("nodes"):
                self._attr_native_value = f"Active ({len(snapshot['graph']['nodes'])} nodes)"
                self._attr_extra_state_attributes = {
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "collected_at": snapshot.get("collected_at"),
                    "stats": snapshot.get("stats")
                }
                self.async_write_ha_state()
                _LOGGER.info("Successfully loaded initial cached topology state")
        except Exception as e:
            _LOGGER.error("Failed to load initial cached topology: %s", e)
            
        # Schedule the first update 10 seconds after startup to not impact HA boot
        self._schedule_next_update(10)

    def _calculate_next_update_delay(self) -> float:
        """Calculate the next update delay dynamically based on system load and config."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        scan_interval = entry.options.get("scan_interval", 60) if entry else 60
        
        try:
            load_1m, _, _ = os.getloadavg()
            cpu_count = os.cpu_count() or 1
            is_busy = load_1m > (cpu_count * 0.75)
        except Exception:
            load_1m = 0.0
            is_busy = False
            
        if is_busy:
            delay = max(scan_interval, 600)
            _LOGGER.info("System is busy (load: %.2f). Throttling background polling to %ds", load_1m, delay)
        else:
            delay = scan_interval
            _LOGGER.debug("System is healthy. Setting background monitoring polling to %ds", delay)
                
        return delay

    def _schedule_next_update(self, delay: float) -> None:
        """Schedule the next update using async_call_later."""
        from homeassistant.helpers.event import async_call_later
        
        async def trigger_update(_now):
            if self.hass.is_running:
                self.async_schedule_update_ha_state(True)
                
        async_call_later(self.hass, delay, trigger_update)

    async def async_update(self) -> None:
        """Update the sensor data."""
        if not self.hass.is_running:
            _LOGGER.info("Skipping topology update because Home Assistant is still starting up")
            self._schedule_next_update(10)
            return
            
        # Prevent starting concurrent update tasks
        if self._update_task and not self._update_task.done():
            _LOGGER.debug("Topology update already in progress, skipping schedule")
            return
            
        _LOGGER.info("Scheduling background topology snapshot update")
        self._update_task = self.hass.async_create_background_task(
            self._async_background_update(),
            "snmp_topology_update"
        )

    async def _async_background_update(self) -> None:
        """Run the actual topology fetch in the background."""
        try:
            snapshot = await self.engine.fetch_topology()
            
            # DO NOT store the full graph in attributes to avoid 16KB limit
            self._attr_extra_state_attributes = {
                "snapshot_id": snapshot.get("snapshot_id"),
                "collected_at": snapshot.get("collected_at"),
                "stats": snapshot.get("stats")
            }
            self._attr_native_value = f"Active ({len(snapshot['graph']['nodes'])} nodes)"
            _LOGGER.info("Background topology update completed successfully")
        except Exception as e:
            _LOGGER.error("Failed background topology update: %s", e)
            self._attr_native_value = "Error"
        finally:
            self.async_write_ha_state()
            # Schedule the next update dynamically
            delay = self._calculate_next_update_delay()
            self._schedule_next_update(delay)

    @property
    def should_poll(self) -> bool:
        """Disable HA automatic polling as we handle scheduling dynamically."""
        return False
