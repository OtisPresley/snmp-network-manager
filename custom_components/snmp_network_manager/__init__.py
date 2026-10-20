"""The SNMP Network Manager integration."""

import asyncio
import logging
import re

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store
from homeassistant.helpers import device_registry as dr, entity_registry as er

import voluptuous as vol

from .const import DOMAIN
from .frontend import async_register_frontend
from .topology import (
    TopologyEngine,
    resolve_device_state,
    find_correlated_device,
    get_device_by_mac,
    get_device_config_entry_ids,
    get_all_device_entities,
    get_primary_device,
    normalize_name,
    normalize_mac,
)

_LOGGER = logging.getLogger(__name__)

@websocket_api.websocket_command({
    vol.Required("type"): "snmp_network_manager/get_topology",
})
@websocket_api.async_response
async def websocket_get_topology(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Handle get topology command."""
    _LOGGER.info("[SNMP Network Map] WebSocket command snmp_network_manager/get_topology called. msg: %s", msg)
    engine = None
    for entry_id, eng in hass.data[DOMAIN].items():
        if isinstance(eng, TopologyEngine):
            engine = eng
            break
            
    if not engine:
        _LOGGER.error("[SNMP Network Map] Topology engine not found in hass.data")
        connection.send_error(msg["id"], "not_found", "Topology engine not found")
        return
        
    try:
        snapshot = await engine.get_last_snapshot()
        _LOGGER.info("[SNMP Network Map] Retrieved last snapshot, nodes: %d, edges: %d", 
                     len(snapshot.get("graph", {}).get("nodes", {})), 
                     len(snapshot.get("graph", {}).get("edges", [])))
        
        # Enrich nodes with registry names and states on the server side
            
        # Load manual device mappings from engine store
        stored_data = await engine.store.async_load() or {}
        manual_device_mappings = stored_data.get("manual_device_mappings", {})
        
        # Enrich nodes with registry names and states on the server side
        try:
            dev_reg = dr.async_get(hass)
            ent_reg = er.async_get(hass)
            
            mac_to_device = {}
            name_to_device = {}
            name_to_switch = {}
            
            for device in dev_reg.devices:
                for conn in device.connections:
                    if conn[1]:
                        conn_val = conn[1].upper()
                        mac_to_device[normalize_mac(conn_val)] = device
                for ident in device.identifiers:
                    ident_val = ident[1].upper()
                    mac_to_device[normalize_mac(ident_val)] = device
                    if ident[0] == "snmp_switch_manager":
                        name_to_switch[device.name.lower()] = device
                if device.name:
                    name_to_device[normalize_name(device.name)] = device
            
            graph = snapshot.get("graph", {})
            nodes = graph.get("nodes", {})
            for node_id, node in nodes.items():
                attrs = node.setdefault("attrs", {})
                kind = node.get("kind")
                mac = attrs.get("mac", "").upper()
                name = attrs.get("name")
                
                matching_device = None
                normalized_mac = normalize_mac(mac) if mac else ""
                
                # Check manual device mappings override first
                if normalized_mac and normalized_mac in manual_device_mappings:
                    manual_device_id = manual_device_mappings[normalized_mac]
                    if manual_device_id:
                        manual_dev = dev_reg.async_get(manual_device_id)
                        matching_device = get_primary_device(dev_reg, ent_reg, manual_dev)
                else:
                    if kind == "device" and attrs.get("integration") == "snmp_switch_manager" and name:
                        matching_device = name_to_switch.get(name.lower())
                    else:
                        ip = attrs.get("ip", "Unknown")
                        matching_device = find_correlated_device(hass, mac, ip, name)
                
                if matching_device:
                    if matching_device.name_by_user:
                        attrs["name"] = matching_device.name_by_user
                    else:
                        resolved_name = attrs.get("name") or name
                        is_generic = not resolved_name or resolved_name == attrs.get("ip") or bool(re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', resolved_name))
                        if is_generic and matching_device.name:
                            attrs["name"] = matching_device.name
                    attrs["device_id"] = matching_device.id
                    
                    # Collect linked integrations (deduplicated by domain) for UI display
                    linked_integrations = []
                    seen_domains = set()
                    for entry_id in get_device_config_entry_ids(matching_device):
                        entry = hass.config_entries.async_get_entry(entry_id)
                        if entry and entry.domain not in seen_domains:
                            seen_domains.add(entry.domain)
                            linked_integrations.append({
                                "domain": entry.domain,
                                "title": entry.title
                            })
                    attrs["linked_integrations"] = linked_integrations
                    
                    dev_entities = get_all_device_entities(ent_reg, dev_reg, matching_device, include_disabled=False)
                    preferred_entity = None
                    if kind == "endpoint":
                        preferred_entity = next((e for e in dev_entities if e.domain == "device_tracker"), None)
                    else:
                        preferred_entity = dev_entities[0] if dev_entities else None
                        
                    if preferred_entity:
                        attrs["entity_id"] = preferred_entity.entity_id
                        
                    # Calculate real-time state using HA registry and refined asleep/away logic
                    attrs["state"] = resolve_device_state(hass, matching_device, dev_entities, attrs.get("state", "down"))
                            
                    if preferred_entity:
                        entity_state = hass.states.get(preferred_entity.entity_id)
                        if entity_state and "ip" in entity_state.attributes:
                            attrs["ip"] = entity_state.attributes["ip"]
                                
        except Exception as enrich_err:
            _LOGGER.error("[SNMP Network Map] Failed to enrich topology snapshot: %s", enrich_err)

        _LOGGER.info("[SNMP Network Map] Sending result snapshot to WebSocket connection")
        connection.send_result(msg["id"], snapshot)
    except Exception as e:
        _LOGGER.error("[SNMP Network Map] websocket_get_topology failed with exception: %s", e)
        connection.send_error(msg["id"], "error", str(e))


async def async_ping(ip: str) -> str:
    """Ping an IP address and return latency or status."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "1", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            out_str = stdout.decode("utf-8", errors="ignore")
            # Look for rtt min/avg/max/mdev
            match = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)/[\d\.]+/[\d\.]+ ms", out_str)
            if match:
                return f"{match.group(1)} ms"
            match2 = re.search(r"time=([\d\.]+)\s*ms", out_str)
            if match2:
                return f"{match2.group(1)} ms"
            return "Online"
        else:
            return "Timeout"
    except Exception as e:
        _LOGGER.error("Ping failed for %s: %s", ip, e)
        return "Failed"


@websocket_api.websocket_command({
    vol.Required("type"): "snmp_network_manager/exclude_device",
    vol.Required("exclude_type"): vol.In(["mac", "ip", "name"]),
    vol.Required("value"): vol.Coerce(str),
})
@websocket_api.async_response
async def websocket_exclude_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Exclude a device by MAC, IP, or Name by updating config entry options."""
    exclude_type = msg["exclude_type"]
    val_to_exclude = msg["value"].strip()
    if not val_to_exclude:
        connection.send_error(msg["id"], "invalid_value", "Value cannot be empty")
        return
        
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_found", "Config entry not found")
        return
    entry = entries[0]
    
    options = dict(entry.options)
    option_key = f"exclude_{exclude_type}s"
    current_val = options.get(option_key, "")
    
    items = [i.strip() for i in current_val.split(",") if i.strip()]
    match_val = val_to_exclude.upper() if exclude_type == "mac" else val_to_exclude.lower() if exclude_type == "name" else val_to_exclude
    
    already_exists = False
    for item in items:
        norm_item = item.upper() if exclude_type == "mac" else item.lower() if exclude_type == "name" else item
        if norm_item == match_val:
            already_exists = True
            break
            
    if not already_exists:
        items.append(val_to_exclude)
        options[option_key] = ",".join(items)
        hass.config_entries.async_update_entry(entry, options=options)
        
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command({
    vol.Required("type"): "snmp_network_manager/ping_device",
    vol.Required("ip"): vol.Coerce(str),
})
@websocket_api.async_response
async def websocket_ping_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Ping a device and return the RTT latency."""
    ip = msg["ip"].strip()
    if not ip or ip == "Unknown":
        connection.send_error(msg["id"], "invalid_ip", "Invalid or unknown IP address")
        return
        
    result = await async_ping(ip)
    connection.send_result(msg["id"], {"latency": result})


@websocket_api.websocket_command({
    vol.Required("type"): "snmp_network_manager/link_device",
    vol.Required("mac"): vol.Coerce(str),
    vol.Required("device_id"): vol.Any(None, vol.Coerce(str)),
})
@websocket_api.async_response
async def websocket_link_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Manually link or unlink a MAC endpoint to a Home Assistant Device ID."""
    mac = msg["mac"].strip().upper()
    device_id = msg["device_id"]
    
    engine = None
    for entry_id, eng in hass.data[DOMAIN].items():
        if isinstance(eng, TopologyEngine):
            engine = eng
            break
            
    if not engine:
        connection.send_error(msg["id"], "not_found", "Topology engine not found")
        return
        
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    
    # 1. Update immediate device registry references
    try:
        if device_id:
            # Merging: Link MAC to target device registry entry immediately
            target_device = dev_reg.async_get(device_id)
            if target_device:
                # Find old SNMP device
                old_device = get_device_by_mac(dev_reg, mac)
                
                if old_device and old_device.id != target_device.id:
                    _LOGGER.info("Manual link merging: Reassigning all entities from device %s (MAC: %s) to target device %s", old_device.id, mac, target_device.id)
                    old_entities = er.async_entries_for_device(ent_reg, old_device.id, include_disabled_entities=True)
                    for entity in old_entities:
                        ent_reg.async_update_entity(entity.entity_id, device_id=target_device.id)
                    
                    dev_reg.async_remove_device(old_device.id)
                
                dev_reg.async_update_device(
                    target_device.id,
                    merge_connections={(dr.CONNECTION_NETWORK_MAC, mac.lower())},
                    merge_identifiers={(DOMAIN, mac.lower())},
                )
        else:
            # Unmerging: Find if MAC is currently merged into any non-pure-SNMP target device and remove linkages
            target_device = None
            for dev in list(dev_reg.devices):
                has_conn = any(c[1] and c[1].upper() == mac for c in dev.connections)
                has_ident = any(i[0] == DOMAIN and i[1] and i[1].upper() == mac for i in dev.identifiers)
                if has_conn or has_ident:
                    is_pure_snmp = dev.identifiers and all(ident[0] in (DOMAIN, "snmp_switch_manager") for ident in dev.identifiers)
                    if not is_pure_snmp:
                        target_device = dev
                        break
            
            if target_device:
                _LOGGER.info("Manual link unmerging: Removing MAC %s connections/identifiers from target device %s", mac, target_device.id)
                new_connections = {c for c in target_device.connections if not (c[1] and c[1].upper() == mac)}
                new_identifiers = {i for i in target_device.identifiers if not (i[0] == DOMAIN and i[1] and i[1].upper() == mac)}
                
                # Check if there are other SNMP entities for this device before removing config entry id
                remaining_entities = [
                    e for e in er.async_entries_for_device(ent_reg, target_device.id, include_disabled_entities=True)
                    if e.platform == DOMAIN
                ]
                
                dev_reg.async_update_device(
                    target_device.id,
                    new_connections=new_connections,
                    new_identifiers=new_identifiers,
                )
                # Move SNMP device tracker/sensor entities out of the target device in the entity registry
                for entity in remaining_entities:
                    ent_reg.async_update_entity(entity.entity_id, device_id=None)
    except Exception as err:
        _LOGGER.error("Failed to update registry during manual linkage change: %s", err)
        
    # Update store
    stored_data = await engine.store.async_load() or {}
    manual_device_mappings = stored_data.setdefault("manual_device_mappings", {})
    
    # Normalize existing manual mappings in the store to ensure consistency
    normalized_mappings = {
        k.replace(":", "").replace("-", "").upper(): v
        for k, v in manual_device_mappings.items()
    }
    
    mac_key = mac.replace(":", "").replace("-", "").upper()
    if not device_id:
        normalized_mappings[mac_key] = "unlinked"
    else:
        normalized_mappings[mac_key] = device_id
        
    stored_data["manual_device_mappings"] = normalized_mappings
    await engine.store.async_save(stored_data)
    
    # Reload config entry to apply immediately
    entries = hass.config_entries.async_entries(DOMAIN)
    if entries:
        hass.async_create_task(hass.config_entries.async_reload(entries[0].entry_id))
        
    connection.send_result(msg["id"], {"success": True})


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the SNMP Network Manager component."""
    await async_register_frontend(hass)
    websocket_api.async_register_command(hass, websocket_get_topology)
    websocket_api.async_register_command(hass, websocket_exclude_device)
    websocket_api.async_register_command(hass, websocket_ping_device)
    websocket_api.async_register_command(hass, websocket_link_device)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SNMP Network Manager from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Check if dependency is loaded
    if "snmp_switch_manager" not in hass.config.components:
        _LOGGER.warning(
            "SNMP Switch Manager integration not detected. SNMP Network Manager may not function correctly."
        )

    # Register WebSocket commands (safe to call multiple times)
    websocket_api.async_register_command(hass, websocket_get_topology)
    websocket_api.async_register_command(hass, websocket_exclude_device)
    websocket_api.async_register_command(hass, websocket_ping_device)
    websocket_api.async_register_command(hass, websocket_link_device)

    engine = TopologyEngine(hass, entry.entry_id)
    # Load manual device mappings from store on startup
    stored_data = await engine.store.async_load() or {}
    engine.manual_device_mappings = stored_data.get("manual_device_mappings", {})
    
    hass.data[DOMAIN][entry.entry_id] = engine

    # Forward setup to platforms (e.g., sensor, device_tracker)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "device_tracker"])

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    _LOGGER.info("SNMP Network Manager options updated, reloading config entry.")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "device_tracker"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry and delete store file."""
    store = Store(hass, 1, "snmp_network_topology_db")
    try:
        await store.async_remove()
        _LOGGER.info("Removed SNMP Network Map database file.")
    except Exception as e:
        _LOGGER.error("Failed to remove SNMP Network Map database file: %s", e)
