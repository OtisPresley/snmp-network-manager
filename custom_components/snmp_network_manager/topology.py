"""Topology Engine for SNMP Network Manager."""
import logging
import time
import socket
import re
import asyncio
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

# OIDs
OID_LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"
OID_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2" # dot1dTpFdbPort (Bridge-MIB)
OID_Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2" # dot1qTpFdbPort (Q-BRIDGE-MIB)
OID_ARP_MAC = "1.3.6.1.2.1.4.22.1.2" # ipNetToMediaPhysAddress (Index: if.IP, Val: MAC)
OID_BASE_MAC = "1.3.6.1.2.1.17.1.1.0" # dot1dBaseBridgeAddress

_DNS_CACHE = {}
_DNS_CACHE_TTL = 3600  # 1 hour

STORAGE_KEY = "snmp_network_topology_db"
STORAGE_VERSION = 1

def normalize_mac(mac: str) -> str:
    """Normalize a MAC address string by stripping delimiters and uppercasing."""
    if not mac:
        return ""
    return mac.replace(":", "").replace("-", "").upper()

def is_access_point_name(name: str) -> bool:
    """Check if a device name indicates an access point."""
    if not name:
        return False
    norm = name.lower()
    return norm.startswith("ap ") or norm == "ap" or name.startswith("AP") or norm.startswith("ap-") or norm.startswith("ap_")

def extract_ip_from_entities(hass: HomeAssistant, entities: list) -> str | None:
    """Extract primary IP address from entity state attributes if available."""
    for ent in entities:
        state_val = hass.states.get(ent.entity_id)
        if state_val:
            ip_attr = state_val.attributes.get("ip") or state_val.attributes.get("ip_address") or state_val.attributes.get("host")
            if ip_attr and ip_attr != "Unknown":
                return ip_attr
    return None

def normalize_name(name):
    """Normalize names for comparison."""
    if not name:
        return ""
    return re.sub(r'[^a-z0-9]', '', name.lower())

def format_friendly_name(n: str) -> str:
    """Format a friendly name from a host or domain name."""
    if not n:
        return ""
    if re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', n):
        return n.upper()
    if "-" in n or "_" in n or n.islower():
        formatted = n.replace("-", " ").replace("_", " ").title()
        formatted = re.sub(r"([A-Za-z])'S\b", r"\1's", formatted)
        return formatted
    return n

def is_ip_address(s: str) -> bool:
    """Check if a string is a standard IPv4 address."""
    if not s or not isinstance(s, str):
        return False
    return bool(re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', s.strip()))

def to_bytes(val) -> bytes:
    """Safely convert any type to bytes for MAC address parsing in Python 3."""
    if val is None:
        return b""
    if isinstance(val, bytes):
        return val
    if isinstance(val, str):
        return bytes([ord(c) for c in val])
    if isinstance(val, (list, tuple)):
        return bytes(val)
    try:
        return bytes(val)
    except Exception:
        return b""

def parse_mac(val) -> str:
    """Parse a MAC address from various SNMP formats."""
    if not val:
        return ""
    if isinstance(val, bytes):
        if len(val) == 6:
            return ":".join([f"{b:02X}" for b in val])
        try:
            val = val.decode("utf-8", errors="ignore")
        except Exception:
            pass
            
    val_str = str(val).strip()
    cleaned = re.sub(r'[^A-Fa-f0-9]', '', val_str)
    if len(cleaned) == 12:
        return ":".join([cleaned[i:i+2].upper() for i in range(0, 12, 2)])
        
    try:
        char_bytes = bytes([ord(c) for c in val_str])
        if len(char_bytes) == 6:
            return ":".join([f"{b:02X}" for b in char_bytes])
    except Exception:
        pass
        
    return ""


def get_device_by_mac(dev_reg: dr.DeviceRegistry, mac: str) -> dr.DeviceEntry | None:
    """Find a device entry by MAC connection without using deprecated async_get_device."""
    if not mac:
        return None
    clean_mac = mac.replace(":", "").replace("-", "").upper()
    for dev in dev_reg.devices:
        for c_type, c_val in dev.connections:
            if c_type == dr.CONNECTION_NETWORK_MAC and c_val:
                if c_val.replace(":", "").replace("-", "").upper() == clean_mac:
                    return dev
    return None


def get_device_config_entry_ids(dev) -> list[str]:
    """Get config entry IDs for a device supporting both modern and legacy HA."""
    if not dev:
        return []
    entry_id = getattr(dev, "config_entry_id", None)
    if entry_id:
        return [entry_id]
    if not hasattr(type(dev), "config_entry_id"):
        entries = getattr(dev, "config_entries", None)
        if entries:
            return list(entries)
    return []


def get_composite_devices(dev_reg, dev) -> list:
    """Return all device entries belonging to the same composite device as the given device."""
    if not dev:
        return []
    comp_id = getattr(dev, "composite_device_id", None)
    if not comp_id:
        return [dev]
    if hasattr(dev_reg, "async_get_devices_for_composite_device_id"):
        comp_devices = dev_reg.async_get_devices_for_composite_device_id(comp_id)
        if comp_devices:
            return list(comp_devices)
    devices = dev_reg.devices.values() if hasattr(dev_reg.devices, "values") else dev_reg.devices
    return [d for d in devices if getattr(d, "composite_device_id", None) == comp_id or d.id == comp_id]


def get_primary_device(dev_reg, ent_reg, dev):
    """Return the primary or most populated device entry in a composite group."""
    if not dev:
        return dev
    comp_devices = get_composite_devices(dev_reg, dev)
    if len(comp_devices) <= 1:
        return dev
    # 1. Prefer device matching composite_primary_config_entry
    primary_config_entry = getattr(dev, "composite_primary_config_entry", None)
    if primary_config_entry:
        for d in comp_devices:
            if primary_config_entry in get_device_config_entry_ids(d):
                return d
    # 2. Prefer device with the most non-disabled entities
    best_dev = dev
    best_count = 0
    if ent_reg:
        def _get_entries(d_id):
            if hasattr(er, "async_entries_for_device"):
                return er.async_entries_for_device(ent_reg, d_id, include_disabled_entities=False)
            if hasattr(ent_reg, "async_entries_for_device"):
                return ent_reg.async_entries_for_device(d_id, include_disabled_entities=False)
            return []
        best_count = len(_get_entries(dev.id))
        for d in comp_devices:
            count = len(_get_entries(d.id))
            if count > best_count:
                best_dev = d
                best_count = count
    return best_dev


def get_all_device_entities(ent_reg, dev_reg, dev, include_disabled: bool = False) -> list:
    """Get all entity entries for a device, including any composite sibling devices."""
    if not dev or not ent_reg:
        return []
    comp_devices = get_composite_devices(dev_reg, dev) if dev_reg else [dev]
    entities = []
    seen = set()
    for d in comp_devices:
        if hasattr(er, "async_entries_for_device"):
            d_entries = er.async_entries_for_device(ent_reg, d.id, include_disabled_entities=include_disabled)
        elif hasattr(ent_reg, "async_entries_for_device"):
            d_entries = ent_reg.async_entries_for_device(d.id, include_disabled_entities=include_disabled)
        else:
            d_entries = []
        for ent in d_entries:
            if ent.entity_id not in seen:
                seen.add(ent.entity_id)
                entities.append(ent)
    return entities


def is_access_point_device(dev) -> bool:
    """Determine if a Home Assistant device entry represents a wireless access point."""
    if not dev:
        return False
    model = (dev.model or "").lower()
    manuf = (dev.manufacturer or "").lower()
    name = (dev.name_by_user or dev.name or "").lower()

    # Generic keywords indicating access point or wireless mesh node
    ap_keywords = [
        "access point", "accesspoint", "wap", "a.p.", "wireless-ap", "wireless ap",
        "mesh", "extender", "repeater", "booster", "satellite", "airport"
    ]
    if any(k in model or k in name or k in manuf for k in ap_keywords):
        return True

    # Word-boundary check for standalone "ap" or common AP prefixes (eap, uap, wap, etc.)
    words = set(re.findall(r'[a-z0-9]+', f"{model} {name}"))
    if any(w in words for w in ("ap", "eap", "uap", "wap", "aimesh", "deco", "orbi")):
        return True

    return False


def is_switch_device(name: str, sys_descr: str, dev) -> bool:
    """Determine if a device is a switch (and therefore not a primary gateway)."""
    name_lower = name.lower() if name else ""
    if "switch" in name_lower or "stack" in name_lower:
        return True

    if sys_descr:
        descr_lower = str(sys_descr).lower()
        if "switch" in descr_lower or "stack" in descr_lower or "layer 3" in descr_lower:
            return True

    if dev:
        model = dev.model.lower() if dev.model else ""
        manuf = dev.manufacturer.lower() if dev.manufacturer else ""
        dev_n = dev.name.lower() if dev.name else ""
        if "switch" in model or "switch" in manuf or "switch" in dev_n:
            return True

    return False


def is_hardware_integration_device(dev) -> bool:
    """Check if a device has identifiers from a physical hardware integration (ignoring SNMP and package managers)."""
    if not dev:
        return False
    idents = getattr(dev, "identifiers", None)
    if not idents:
        return True
    ignored = {"snmp_network_manager", "snmp_switch_manager", "hacs"}
    return any(ident[0] not in ignored for ident in idents)


def get_name_words(val: str) -> set:
    """Extract lowercase alphanumeric word tokens of length >= 2 from a name."""
    if not val:
        return set()
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", str(val).lower())
    return {w for w in cleaned.split() if len(w) > 1}


def find_correlated_device(hass: HomeAssistant, mac: str, ip: str, name: str):
    """Find a matching Home Assistant device for an SNMP endpoint."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    
    normalized_mac = mac.replace(":", "").replace("-", "").upper() if mac else ""
    
    def _has_rich_entities(device_id):
        if not device_id or not ent_reg:
            return False
        d_entries = []
        if hasattr(er, "async_entries_for_device"):
            d_entries = er.async_entries_for_device(ent_reg, device_id, include_disabled_entities=False)
        elif hasattr(ent_reg, "async_entries_for_device"):
            d_entries = ent_reg.async_entries_for_device(device_id, include_disabled_entities=False)
        return any(
            getattr(e, "domain", None) != "device_tracker"
            and getattr(e, "platform", None) not in ("snmp_network_manager", "snmp_switch_manager", "hacs")
            for e in d_entries
        )

    roaming_keywords = {"laptop", "phone", "ipad", "iphone", "macbook", "workstation", "pc", "desktop", "tablet", "computer"}

    def _find_by_name(node_name, require_rich=False):
        if not node_name:
            return None
        t_name = normalize_name(node_name)
        if len(t_name) <= 2:
            return None
        t_words = get_name_words(node_name)
        is_roam = any(k in t_words for k in roaming_keywords)

        # 4a. Exact name match against physical hardware devices
        for dev in dev_reg.devices:
            dev_name = dev.name_by_user or dev.name
            if dev_name and normalize_name(dev_name) == t_name:
                if is_hardware_integration_device(dev):
                    if not require_rich or _has_rich_entities(dev.id):
                        return dev

        if not is_roam:
            # 4b. Manufacturer match + name containment (e.g. node 'WaterGuru' and dev 'WaterGuru Pool', manuf 'WaterGuru')
            for dev in dev_reg.devices:
                if not is_hardware_integration_device(dev):
                    continue
                manuf = normalize_name(dev.manufacturer)
                dev_name = dev.name_by_user or dev.name
                if manuf and manuf == t_name and len(t_name) >= 4:
                    if dev_name and t_name in normalize_name(dev_name):
                        if not require_rich or _has_rich_entities(dev.id):
                            return dev

            # 4c. Multi-word token subset match (e.g. dev 'Front Door' [2 words] in node 'Front Door Lock' [3 words])
            fuzzy_matches = []
            for dev in dev_reg.devices:
                if not is_hardware_integration_device(dev):
                    continue
                dev_name = dev.name_by_user or dev.name
                dev_words = get_name_words(dev_name)
                if len(dev_words) >= 2 and dev_words.issubset(t_words):
                    if not require_rich or _has_rich_entities(dev.id):
                        fuzzy_matches.append((dev, len(dev_words)))
                elif len(t_words) >= 2 and t_words.issubset(dev_words):
                    if not require_rich or _has_rich_entities(dev.id):
                        fuzzy_matches.append((dev, len(t_words)))

            if fuzzy_matches:
                fuzzy_matches.sort(key=lambda x: x[1], reverse=True)
                return fuzzy_matches[0][0]

        return None

    def _finalize_device(dev):
        if not dev:
            return None
        primary = get_primary_device(dev_reg, ent_reg, dev)
        # If the matched device has no rich entities (e.g. only bare device_tracker from UniFi),
        # prefer a hardware device matching by name that has rich entities (sensors, battery, locks, etc.)
        if primary and name and not _has_rich_entities(primary.id):
            rich_dev = _find_by_name(name, require_rich=True)
            if rich_dev:
                return get_primary_device(dev_reg, ent_reg, rich_dev)
        return primary

    # 1. Search by MAC connection or identifier first
    if normalized_mac:
        for dev in dev_reg.devices:
            for conn in dev.connections:
                if conn[1] and conn[1].replace(":", "").replace("-", "").upper() == normalized_mac:
                    if is_hardware_integration_device(dev):
                        # Check name compatibility to prevent incorrect merges of roaming adapters
                        if name and dev.name:
                            n1 = normalize_name(name)
                            n2 = normalize_name(dev.name)
                            # Only enforce name check for personal roaming devices (laptops, phones, tablets, etc.)
                            is_roaming = any(k in n1 or k in n2 for k in roaming_keywords)
                            if is_roaming:
                                if len(n1) > 2 and len(n2) > 2 and n1 not in n2 and n2 not in n1:
                                    continue
                        return _finalize_device(dev)
            for ident in dev.identifiers:
                if ident[1] and ident[1].replace(":", "").replace("-", "").upper() == normalized_mac:
                    if is_hardware_integration_device(dev):
                        # Check name compatibility to prevent incorrect merges of roaming adapters
                        if name and dev.name:
                            n1 = normalize_name(name)
                            n2 = normalize_name(dev.name)
                            # Only enforce name check for personal roaming devices (laptops, phones, tablets, etc.)
                            is_roaming = any(k in n1 or k in n2 for k in roaming_keywords)
                            if is_roaming:
                                if len(n1) > 2 and len(n2) > 2 and n1 not in n2 and n2 not in n1:
                                    continue
                        return _finalize_device(dev)

    # 2. Search by Entity Registry unique_id containing the MAC (other integrations)
    if normalized_mac:
        for entity in ent_reg.entities.values():
            if entity.platform != "snmp_network_manager" and entity.unique_id:
                uniq = entity.unique_id.replace(":", "").replace("-", "").upper()
                if normalized_mac in uniq:
                    if entity.device_id:
                        dev = dev_reg.async_get(entity.device_id)
                        if dev and is_hardware_integration_device(dev):
                            # Check name compatibility to prevent incorrect merges of roaming adapters
                            if name and dev.name:
                                n1 = normalize_name(name)
                                n2 = normalize_name(dev.name)
                                # Only enforce name check for personal roaming devices (laptops, phones, tablets, etc.)
                                is_roaming = any(k in n1 or k in n2 for k in roaming_keywords)
                                if is_roaming:
                                    if len(n1) > 2 and len(n2) > 2 and n1 not in n2 and n2 not in n1:
                                        continue
                            return _finalize_device(dev)

    # 3. Search by IP address
    ip_to_search = ip if (ip and ip != "Unknown") else None
                
    if ip_to_search:
        # Scan states for matching IP (either state value or attributes)
        for state in hass.states.async_all():
            is_match = state.state == ip_to_search
            
            if not is_match:
                ip_attr = state.attributes.get("ip") or state.attributes.get("ip_address") or state.attributes.get("host")
                if ip_attr == ip_to_search:
                    is_match = True
                    
            if is_match:
                entry = ent_reg.async_get(state.entity_id)
                if entry and entry.device_id:
                    dev = dev_reg.async_get(entry.device_id)
                    if dev and is_hardware_integration_device(dev):
                        return _finalize_device(dev)

    # 4. Search by normalized name match
    if name:
        matched_by_name = _find_by_name(name)
        if matched_by_name:
            return _finalize_device(matched_by_name)
                            
        # 5. Dynamic heuristic: match normalized identifier value substring inside normalized SNMP name
        target_name = normalize_name(name)
        for dev in dev_reg.devices:
            if not is_hardware_integration_device(dev):
                continue
            for ident in dev.identifiers:
                domain_name = ident[0]
                ident_val = str(ident[1])
                if domain_name in ("snmp_network_manager", "snmp_switch_manager", "hacs"):
                    continue
                norm_ident = normalize_name(ident_val)
                # Require minimum length of 6 characters to prevent false matches on short integers/booleans
                if len(norm_ident) >= 6 and norm_ident in target_name:
                    return _finalize_device(dev)
                            
    return None


def resolve_device_state(hass: HomeAssistant, matching_device, dev_entities, current_snmp_state: str) -> str:
    """Resolve the real-time state of a device based on HA registry and SNMP status."""
    if not matching_device:
        return current_snmp_state

    integrations = set()
    for entry_id in get_device_config_entry_ids(matching_device):
        entry = hass.config_entries.async_get_entry(entry_id) if hass else None
        if entry:
            integrations.add(entry.domain)

    # Portable classification
    is_mobile_phone = False
    is_laptop_tablet = False
    is_portable = "mobile_app" in integrations

    dev_model = (matching_device.model or "").lower()
    dev_mfg = (matching_device.manufacturer or "").lower()
    dev_name_lower = (matching_device.name_by_user or matching_device.name or "").lower()

    mobile_keywords = ["phone", "iphone", "android", "mobile", "watch", "pixel", "galaxy"]
    portable_stationary_keywords = ["tablet", "ipad", "laptop", "macbook", "notebook", "chromebook", "surface", "kindle", "e-reader"]

    if any(kw in dev_model or kw in dev_mfg or kw in dev_name_lower for kw in mobile_keywords):
        is_mobile_phone = True
    if any(kw in dev_model or kw in dev_mfg or kw in dev_name_lower for kw in portable_stationary_keywords):
        is_laptop_tablet = True

    # A device is portable if it is mobile or a laptop/tablet or has mobile_app
    is_portable = is_portable or is_mobile_phone or is_laptop_tablet

    # Determine if device is battery-powered based on HA entities/attributes
    is_battery_powered = False
    for ent in dev_entities:
        ent_dc = getattr(ent, "device_class", None) or getattr(ent, "original_device_class", None)
        t_key = getattr(ent, "translation_key", None) or ""
        if ent_dc == "battery" or t_key in ("battery", "battery_level") or "battery" in ent.entity_id.lower():
            is_battery_powered = True
            break
        ent_state = hass.states.get(ent.entity_id) if hass else None
        if ent_state:
            attrs = ent_state.attributes
            if (
                attrs.get("device_class") == "battery"
                or "battery" in attrs
                or "battery_level" in attrs
                or "battery_state" in attrs
                or "battery_percentage" in attrs
            ):
                is_battery_powered = True
                break

    ha_available = False
    has_home_tracker = False
    has_gps_away_tracker = False
    has_network_away_tracker = False
    is_standby = False

    for ent in dev_entities:
        if getattr(ent, "platform", None) == "snmp_network_manager":
            continue
        ent_state = hass.states.get(ent.entity_id) if hass else None
        if ent_state:
            if ent_state.state not in ["unavailable", "unknown"]:
                ha_available = True
                domain = getattr(ent, "domain", None) or ent.entity_id.split(".")[0]
                if domain in ("media_player", "vacuum", "climate", "switch", "light"):
                    if ent_state.state in ["off", "standby", "idle", "paused"]:
                        is_standby = True
            if getattr(ent, "domain", None) == "device_tracker":
                source_type = ent_state.attributes.get("source_type")
                if ent_state.state == "home":
                    has_home_tracker = True
                elif ent_state.state in ["not_home", "away"]:
                    if source_type == "gps":
                        has_gps_away_tracker = True
                    else:
                        has_network_away_tracker = True

    # State transitions based on Home Assistant tracked state and SNMP status
    if current_snmp_state == "up" or has_home_tracker:
        return "up"
    elif is_portable:
        if has_gps_away_tracker:
            return "away"
        elif is_mobile_phone and not is_laptop_tablet:
            # For a pure mobile phone/watch, a network tracker showing not_home or being offline on SNMP is enough to mark it Away
            if has_network_away_tracker or (not has_home_tracker and current_snmp_state != "up"):
                return "away"
            elif ha_available:
                return "asleep"
            else:
                return "down"
        else:
            # Laptops/tablets: if HA tracks them as available/reporting, they are Asleep; otherwise Offline (down)
            if ha_available:
                return "asleep"
            else:
                return "down"
    else:
        # Stationary devices:
        # When ha_available is True: battery-powered devices and devices in standby (off/idle) are Asleep; active devices are Up.
        # When ha_available is False: Home Assistant cannot communicate with the device, so it is Offline (down).
        if ha_available:
            if is_battery_powered or is_standby:
                return "asleep"
            else:
                return "up"
        else:
            return "down"

def read_local_arp() -> dict:
    """Read host ARP cache from /proc/net/arp as fallback."""
    mac_to_ip = {}
    try:
        with open("/proc/net/arp", "r") as f:
            lines = f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    ip = parts[0]
                    mac = parts[3].upper()
                    if mac != "00:00:00:00:00:00":
                        mac_to_ip[mac] = ip
    except Exception:
        pass
    return mac_to_ip

class TopologyEngine:
    """Manages topology discovery and graph construction."""

    def __init__(self, hass: HomeAssistant, entry_id: str = None):
        """Initialize the engine."""
        self.hass = hass
        self.entry_id = entry_id
        self.store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.unsupported_fdb_devices = set()
        self.last_api_request_time = 0

    async def _async_resolve_dns(self, ip: str, mac_to_name: dict, mac_upper: str) -> str:
        """Resolve IP address to hostname via reverse DNS with caching."""
        if not ip or ip == "Unknown":
            return "Unknown"
        now = time.time()
        cached_name, cached_time = _DNS_CACHE.get(ip, (None, 0))
        if cached_name and (now - cached_time < _DNS_CACHE_TTL):
            dns_name = cached_name
        else:
            try:
                name_res = await self.hass.async_add_executor_job(
                    socket.gethostbyaddr, ip
                )
                dns_name = name_res[0]
                _DNS_CACHE[ip] = (dns_name, now)
            except Exception:
                _DNS_CACHE[ip] = (None, now)
                dns_name = mac_to_name.get(mac_upper, ip)

        if not dns_name or dns_name == ip:
            dns_name = mac_to_name.get(mac_upper, ip)

        return dns_name or "Unknown"

    def _apply_node_deduplication(self, existing_node: dict, new_ip: str, new_name: str, mac: str, context: str = "Discovery") -> None:
        """Apply standardized deduplication rules when merging node updates."""
        existing_attrs = existing_node.setdefault("attrs", {})
        existing_ip = existing_attrs.get("ip", "Unknown")
        existing_name = existing_attrs.get("name", "")

        # Rule 2: MAC & IP match, Name different -> update name
        if existing_ip != "Unknown" and existing_ip == new_ip:
            if existing_name != new_name and new_name:
                _LOGGER.debug("Deduplication [%s Rule 2]: updating name of %s from '%s' to '%s'", context, mac, existing_name, new_name)
                existing_attrs["name"] = format_friendly_name(new_name)

        # Rule 3: MAC & Name match, IP different -> update IP
        if existing_name and existing_name == new_name:
            if existing_ip != new_ip and new_ip != "Unknown":
                _LOGGER.debug("Deduplication [%s Rule 3]: updating IP of %s from '%s' to '%s'", context, mac, existing_ip, new_ip)
                existing_attrs["ip"] = new_ip

        # Active IP drift update
        if new_ip != "Unknown" and new_ip != existing_ip:
            _LOGGER.debug("Deduplication [%s IP update]: updating IP of MAC %s from '%s' to '%s'", context, mac, existing_ip, new_ip)
            existing_attrs["ip"] = new_ip

    async def get_switch_clients(self):
        """Get all clients from snmp_switch_manager."""
        clients = []
        entries = self.hass.config_entries.async_entries("snmp_switch_manager")
        for entry in entries:
            runtime = getattr(entry, "runtime_data", None)
            if runtime and hasattr(runtime, "client"):
                clients.append((entry.title, runtime.client, entry))
        return clients

    async def fetch_topology(self):
        """Fetch topology data from all switches."""
        clients = await self.get_switch_clients()
        
        # Fetch all switch data in parallel concurrently
        tasks = [self._async_fetch_switch_raw_data(name, client) for name, client, entry in clients]
        raw_results = await asyncio.gather(*tasks)
        
        switch_data = {}
        for (name, client, entry), data in zip(clients, raw_results):
            if data:
                switch_data[name] = data
        
        # Load stored data for persistence and skip lists
        stored_data = await self.store.async_load() or {}
        raw_mappings = stored_data.get("manual_device_mappings", {})
        self.manual_device_mappings = {
            k.replace(":", "").replace("-", "").upper(): v
            for k, v in raw_mappings.items()
        }
        
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)

        nodes = {}
        edges = []
        
        # Populate switch interface MACs from Device Registry connections first
        known_macs = set()
        for dev in dev_reg.devices:
            is_switch = False
            for entry_id in get_device_config_entry_ids(dev):
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry and entry.domain == "snmp_switch_manager":
                    is_switch = True
                    break
            if is_switch:
                for conn_type, conn_val in dev.connections:
                    if conn_type == dr.CONNECTION_NETWORK_MAC:
                        known_macs.add(conn_val.upper())
        
        # Fallback MAC-to-IP from host's ARP table /proc/net/arp
        mac_to_ip = read_local_arp()
        mac_to_name = {}
        active_macs = set()
        
        # Deduplication sets (Using normalized names)
        seen_names = set()
        seen_ips = set()

        # 1. Fetch ARP and identify switches
        for name, client, entry in clients:
            seen_names.add(normalize_name(name))
            
            data = switch_data.get(name) or {}
            arp_entries = data.get("arp_mac") or []
            if not arp_entries:
                arp_entries = data.get("arp_phys") or []
            _LOGGER.info("Found %d ARP entries on %s", len(arp_entries), name)
            
            for oid_str, val in arp_entries:
                parts = oid_str.split(".")
                if len(parts) >= 4:
                    ip_parts = parts[-4:]
                    ip = ".".join(ip_parts)
                    
                    mac = parse_mac(val)
                    if mac:
                        mac_upper = mac.upper()
                        mac_to_ip[mac_upper] = ip
                        active_macs.add(mac_upper)

            # Fetch base MAC and add to known_macs
            base_mac_val = data.get("base_mac")
            if base_mac_val:
                base_mac = parse_mac(base_mac_val)
                if base_mac:
                    known_macs.add(base_mac.upper())
                
            # Fetch all network interface MACs for this switch using ifPhysAddress
            if_macs = data.get("if_macs") or []
            parsed_macs = []
            for oid_str, val in if_macs:
                if_mac = parse_mac(val)
                if if_mac:
                    if_mac_upper = if_mac.upper()
                    known_macs.add(if_mac_upper)
                    parsed_macs.append(if_mac_upper)
            _LOGGER.info("Fetched %d interface MACs for %s: %s", len(parsed_macs), name, parsed_macs)

        # Map to track which LLDP neighbor is on which switch port
        port_to_neighbor = {}

        # 2. Fetch LLDP and FDB
        for name, client, entry in clients:
            _LOGGER.info("Fetching topology from %s", name)
            data = switch_data.get(name) or {}
            
            switch_id = f"device:{name}"
            host_ip = getattr(client, "host", None) or (entry.data or {}).get("host") or "Unknown"
            is_ap_device = is_access_point_name(name)
            nodes[switch_id] = {
                "kind": "device", 
                "attrs": {
                    "name": name,
                    "ip": host_ip,
                    "integration": "snmp_switch_manager",
                    "device_slug": name.lower().replace("-", "_").replace(" ", "_").replace(".", "_")
                }
            }
            if is_ap_device:
                nodes[switch_id]["attrs"]["is_ap"] = True
            
            # Find HA device for the switch itself (Optional now as we set integration directly)
            matching_dev = None
            try:
                short_name_clean = normalize_name(name)
                for dev in dev_reg.devices:
                    if dev.name:
                        dev_name_clean = normalize_name(dev.name)
                        if dev_name_clean == short_name_clean or dev_name_clean == short_name_clean.replace("switch", "") or short_name_clean == dev_name_clean.replace("switch", ""):
                            matching_dev = dev
                            nodes[switch_id]["attrs"]["device_id"] = dev.id
                            
                            dev_model = (dev.model or "").lower()
                            dev_manuf = (dev.manufacturer or "").lower()
                            dev_name = (dev.name or "").lower()

                            # Check if this device registry entry suggests it's an AP
                            if is_access_point_device(dev):
                                nodes[switch_id]["attrs"]["is_ap"] = True

                            # Check if this device registry entry suggests it's a gateway
                            if any(k in dev_manuf or k in dev_model or k in dev_name for k in ["router", "gateway", "firewall"]):
                                nodes[switch_id]["attrs"]["is_gateway"] = True
                            
                            entities = er.async_entries_for_device(ent_reg, dev.id)
                            if entities:
                                nodes[switch_id]["attrs"]["entity_id"] = entities[0].entity_id
                            break
            except Exception as e:
                _LOGGER.error("Error finding switch device: %s", e)
            
            # Check if this switch/device is actually a gateway dynamically
            is_gateway = False
            try:
                import ipaddress
                
                # Check 1: Check client name
                name_lower = name.lower()
                if "gateway" in name_lower or "router" in name_lower or "firewall" in name_lower:
                    is_gateway = True
                    
                # Check 2: SNMP SysDescr
                if not is_gateway:
                    sys_descr = data.get("sys_descr")
                    if sys_descr:
                        sys_descr_str = str(sys_descr).lower()
                        if any(k in sys_descr_str for k in ["router", "gateway", "firewall"]):
                            is_gateway = True
                            
                # Check 3: Check IP addresses (public IP presence)
                if not is_gateway:
                    ip_entries = data.get("ip_addrs") or []
                    for oid_str, val in ip_entries:
                        # 3a. Try parsing IP from OID suffix (OID index represents IP)
                        parts = oid_str.split(".")
                        if len(parts) >= 4:
                            ip_str = ".".join(parts[-4:])
                            try:
                                ip_obj = ipaddress.ip_address(ip_str)
                                if not ip_obj.is_private and not ip_obj.is_loopback and not ip_obj.is_link_local and not ip_obj.is_unspecified and not ip_obj.is_multicast:
                                    is_gateway = True
                                    break
                            except ValueError:
                                pass
                        
                        # 3b. Fallback: Parse IP from the value itself
                        if not is_gateway and val:
                            try:
                                val_str = None
                                if isinstance(val, bytes) and len(val) == 4:
                                    val_str = ".".join(str(b) for b in val)
                                else:
                                    val_str = str(val).strip()
                                if val_str:
                                    ip_obj = ipaddress.ip_address(val_str)
                                    if not ip_obj.is_private and not ip_obj.is_loopback and not ip_obj.is_link_local and not ip_obj.is_unspecified and not ip_obj.is_multicast:
                                        is_gateway = True
                                        break
                            except ValueError:
                                continue

                # Check 4: SNMP ipForwarding OID (1 = forwarding/router)
                if not is_gateway:
                    ip_fwd = data.get("ip_forwarding")
                    if ip_fwd == "1" or ip_fwd == 1:
                        is_gateway = True
                        
                # Overrides: switches with "switch" in the name/metadata are not gateways
                sys_descr_val = data.get("sys_descr")
                if is_gateway and is_switch_device(name, sys_descr_val, matching_dev):
                    is_gateway = False
            except Exception as gw_err:
                _LOGGER.debug("Error during dynamic gateway check for %s: %s", name, gw_err)
                
            if is_gateway:
                nodes[switch_id]["attrs"]["is_gateway"] = True
            else:
                nodes[switch_id]["attrs"]["is_gateway"] = False
            
            # Fetch ifName to map ifIndex to physical name
            ifname_map = {}
            ifnames = data.get("if_names") or []
            for oid_str, val in ifnames:
                parts = oid_str.split(".")
                if parts:
                    idx = int(parts[-1])
                    ifname_map[idx] = str(val).strip()
                
            # Fetch interface speeds
            ifspeed_map = {}
            ifhighspeeds = data.get("if_highspeeds") or []
            for oid_str, val in ifhighspeeds:
                parts = oid_str.split(".")
                if parts:
                    idx = int(parts[-1])
                    ifspeed_map[idx] = int(val)
                
            if not ifspeed_map:
                ifspeeds = data.get("if_speeds") or []
                for oid_str, val in ifspeeds:
                    parts = oid_str.split(".")
                    if parts:
                        idx = int(parts[-1])
                        ifspeed_map[idx] = int(int(val) / 1000000)
                
            # Fetch LLDP
            try:
                lldp_caps = {}
                lldp_caps_list = data.get("lldp_caps") or []
                for oid_str, val in lldp_caps_list:
                    parts = oid_str.split(".")
                    if len(parts) >= 2:
                        try:
                            l_port = int(parts[-2])
                            r_idx = int(parts[-1])
                            lldp_caps[(l_port, r_idx)] = val
                        except Exception:
                            pass

                def is_wlan_ap(val) -> bool:
                    try:
                        val_bytes = bytes(val)
                        if val_bytes:
                            # In standard LLDP System Capabilities Enabled bit map, bit 3 (0x10) is WLAN AP
                            return bool(val_bytes[0] & 0x10)
                    except Exception:
                        pass
                    return False

                lldp_names = data.get("lldp_names") or []
                for oid_str, val in lldp_names:
                    parts = oid_str.split(".")
                    rem_name = str(val).strip()
                    
                    if rem_name and len(parts) >= 3:
                        try:
                            local_port = int(parts[-2])
                            rem_index = int(parts[-1])
                            
                            short_name = rem_name.split(".")[0]
                            rem_node_id = f"device:{short_name}"
                            
                            nodes[rem_node_id] = {"kind": "device", "attrs": {"name": short_name}}
                            seen_names.add(normalize_name(short_name))
                            
                            # HA Correlation for APs (Search by name)
                            is_ap_by_name = is_access_point_name(short_name)
                            is_ap = is_ap_by_name
                            
                            caps_val = lldp_caps.get((local_port, rem_index))
                            if caps_val and is_wlan_ap(caps_val):
                                is_ap = True
                                _LOGGER.info("Identified %s as an AP via LLDP capabilities", short_name)
                                
                            if is_ap:
                                nodes[rem_node_id]["attrs"]["is_ap"] = True
                                short_name_clean = normalize_name(short_name)
                                for dev in dev_reg.devices:
                                    if dev.name:
                                        dev_name_clean = normalize_name(dev.name)
                                        if dev_name_clean == short_name_clean:
                                            nodes[rem_node_id]["attrs"]["device_id"] = dev.id
                                            
                                            # Better entity selection
                                            entities = er.async_entries_for_device(ent_reg, dev.id)
                                            if entities:
                                                active_entities = [e for e in entities if not e.disabled_by and not e.hidden_by]
                                                
                                                preferred = None
                                                # Look for restart button for APs
                                                for e in active_entities:
                                                    if e.domain == "button" and ("restart" in e.entity_id or "reboot" in e.entity_id):
                                                        preferred = e
                                                        break
                                                        
                                                # Fallback to switch or control
                                                if not preferred:
                                                    for e in active_entities:
                                                        if e.domain in ["switch", "light", "media_player"]:
                                                            preferred = e
                                                            break
                                                            
                                                if not preferred and active_entities:
                                                    preferred = active_entities[0]
                                                    
                                                if not preferred and entities:
                                                    preferred = entities[0]
                                                    
                                                if preferred:
                                                    nodes[rem_node_id]["attrs"]["entity_id"] = preferred.entity_id
                                            break
                            
                            local_port_name = ifname_map.get(local_port, str(local_port))
                            port_to_neighbor[(switch_id, local_port_name)] = rem_node_id
                            
                            port_name_val = ifname_map.get(local_port, f"Port {local_port}")
                            edge_attrs = {
                                "bridge_port": port_name_val,
                                "port_name": port_name_val
                            }
                            speed = ifspeed_map.get(local_port)
                            if speed is not None:
                                edge_attrs["speed"] = speed
                                
                            _LOGGER.debug("Created LLDP edge: %s -> %s on port %s", switch_id, rem_node_id, local_port_name)
                            edges.append({
                                "kind": "lldp",
                                "src": switch_id,
                                "dst": rem_node_id,
                                "attrs": edge_attrs
                            })
                        except ValueError:
                            continue
            except Exception as e:
                _LOGGER.error("Failed to process LLDP from %s: %s", name, e)
                
            # Fetch ifOperStatus to check if ports are actually up
            ifstatus_map = {}
            ifstatus = data.get("if_operstatus") or []
            for oid_str, val in ifstatus:
                parts = oid_str.split(".")
                if parts:
                    idx = int(parts[-1])
                    ifstatus_map[idx] = int(val)
                
            # Fetch dot1dBasePortIfIndex to map Bridge Port to ifIndex
            baseport_map = {}
            baseports = data.get("baseports") or []
            for oid_str, val in baseports:
                parts = oid_str.split(".")
                if parts:
                    idx = int(parts[-1])
                    baseport_map[idx] = int(val)
                
            # Fetch FDB
            try:
                if name in self.unsupported_fdb_devices:
                    _LOGGER.debug("Skipping FDB poll for known unsupported device %s", name)
                    continue
                    
                fdb_entries = data.get("q_fdb")
                using_q_mib = True
                
                if not fdb_entries:
                    _LOGGER.info("Q-BRIDGE-MIB returned no FDB entries for %s, falling back to Bridge-MIB", name)
                    fdb_entries = data.get("fdb") or []
                    using_q_mib = False
                    
                if not fdb_entries:
                    _LOGGER.info("Device %s returned no FDB entries. Skipping in future polls.", name)
                    self.unsupported_fdb_devices.add(name)
                    
                for oid_str, val in fdb_entries:
                    parts = oid_str.split(".")
                    
                    if using_q_mib and len(parts) >= 7:
                        mac_parts = parts[-6:]
                    elif not using_q_mib and len(parts) >= 6:
                        mac_parts = parts[-6:]
                    else:
                        continue
                        
                    try:
                        mac = ":".join([f"{int(p):02X}" for p in mac_parts])
                        mac_upper = mac.upper()
                        active_macs.add(mac_upper)
                        if mac_upper in known_macs:
                            continue
                            
                        port = int(val)
                        if_idx = baseport_map.get(port)
                        
                        if if_idx is not None:
                            port_name = ifname_map.get(if_idx, str(port))
                        else:
                            port_name = ifname_map.get(port, str(port))
                        
                        endpoint_id = f"endpoint:{mac_upper}"
                        ip = mac_to_ip.get(mac_upper, "Unknown")
                        
                        # Perform Reverse DNS lookup
                        dns_name = await self._async_resolve_dns(ip, mac_to_name, mac_upper)
                            
                        # Try to find existing device in HA registry to get its name
                        ha_device = get_device_by_mac(dev_reg, mac)

                        # format_friendly_name defined at module level

                        short_name = ""
                        if ha_device and ha_device.name_by_user:
                            short_name = format_friendly_name(ha_device.name_by_user)
                            
                        if not short_name:
                            # Prefer a newly resolved, valid DNS name if available
                            if dns_name and dns_name != ip and dns_name != "Unknown" and not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', dns_name) and not is_ip_address(dns_name):
                                short_name = format_friendly_name(dns_name.split(".")[0])
                            elif ha_device and ha_device.name:
                                short_name = format_friendly_name(ha_device.name)
                            else:
                                short_name = mac
                        
                        # Cross-board Deduplication (by endpoint_id/MAC to preserve Multi-NIC endpoints)
                        if endpoint_id not in nodes:
                            if short_name:
                                seen_names.add(normalize_name(short_name))
                            if ip != "Unknown":
                                seen_ips.add(ip)
                                
                            nodes[endpoint_id] = {
                                "kind": "endpoint", 
                                "attrs": {
                                    "mac": mac,
                                    "ip": ip,
                                    "name": short_name
                                }
                            }
                        
                        # HA Correlation: Find device by MAC or manual mapping/IP attribute fallbacks
                        try:
                            device = None
                            mac_key = mac.replace(":", "").replace("-", "").upper()
                            is_explicitly_unlinked = False
                            if hasattr(self, "manual_device_mappings") and mac_key in self.manual_device_mappings:
                                manual_device_id = self.manual_device_mappings[mac_key]
                                if manual_device_id == "unlinked" or not manual_device_id:
                                    is_explicitly_unlinked = True
                                else:
                                    device = dev_reg.async_get(manual_device_id)
                                    
                            if not device and not is_explicitly_unlinked:
                                device = find_correlated_device(self.hass, mac, ip, short_name)
                                
                            if device and endpoint_id in nodes:
                                nodes[endpoint_id]["attrs"]["device_id"] = device.id
                                if device.name and not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', device.name):
                                    nodes[endpoint_id]["attrs"]["name"] = format_friendly_name(device.name_by_user or device.name)
                                    
                                # Sync the primary IP of the device from its HA entity states
                                dev_entities = er.async_entries_for_device(ent_reg, device.id)
                                primary_ip = extract_ip_from_entities(self.hass, dev_entities)
                                if primary_ip:
                                    nodes[endpoint_id]["attrs"]["ip"] = primary_ip
                                    ip = primary_ip
                                    
                                # Check if it is an AP
                                if is_access_point_device(device):
                                    nodes[endpoint_id]["attrs"]["is_ap"] = True
                                    _LOGGER.debug("Identified %s as an AP", device.name)
                                
                                entities = er.async_entries_for_device(ent_reg, device.id)
                                if entities:
                                    active_entities = [e for e in entities if not e.disabled_by and not e.hidden_by]
                                    
                                    preferred = None
                                    # Look for preferred control domains
                                    preferred_domains = ["media_player", "switch", "light", "camera", "climate", "vacuum", "cover", "lock", "remote", "fan"]
                                    for e in active_entities:
                                        if e.domain in preferred_domains:
                                            preferred = e
                                            break
                                            
                                    if not preferred and active_entities:
                                        preferred = active_entities[0]
                                        
                                    if not preferred and entities:
                                        preferred = entities[0]
                                        
                                    if preferred:
                                        nodes[endpoint_id]["attrs"]["entity_id"] = preferred.entity_id
                                        nodes[endpoint_id]["attrs"]["domain"] = preferred.domain
                        except Exception as e:
                            _LOGGER.error("Error during device correlation for %s: %s", mac, e)
                        
                        target_id = switch_id
                            
                        neighbor_id = port_to_neighbor.get((switch_id, port_name))
                        if neighbor_id:
                            # Check if the neighbor is an AP
                            neighbor_node = nodes.get(neighbor_id)
                            is_ap_neighbor = False
                            if neighbor_node:
                                is_ap_neighbor = neighbor_node.get("attrs", {}).get("is_ap", False)
                            
                            if is_ap_neighbor:
                                # Map the client directly to the AP instead of the switch
                                target_id = neighbor_id
                            else:
                                # Switch-to-switch trunk: skip
                                continue
                            
                        edge_attrs = {"bridge_port": port_name}
                        lookup_idx = baseport_map[port] if port in baseport_map else port
                        speed = ifspeed_map.get(lookup_idx)
                        if speed is not None:
                            edge_attrs["speed"] = speed
                            
                        _LOGGER.debug("Created FDB edge: %s -> %s on port %s (%s)", endpoint_id, target_id, port_name, name)
                        edges.append({
                            "kind": "fdb",
                            "src": endpoint_id,
                            "dst": target_id,
                            "attrs": edge_attrs
                        })
                    except (ValueError, IndexError):
                        continue
            except Exception as e:
                _LOGGER.error("Failed to fetch FDB from %s: %s", name, e)
                
        _LOGGER.info("PORT TO NEIGHBOR MAP: %s", {f"{k[0]}:{k[1]}": v for k, v in port_to_neighbor.items()})

        # Persistence: Use stored nodes and edges
        stored_nodes = stored_data.get("nodes", {})
        stored_edges = stored_data.get("last_snapshot", {}).get("graph", {}).get("edges", [])
        
        # Deduplicate current edges by signature (src, dst, kind)
        seen_edges_signatures = set()
        deduplicated_edges = []
        for edge in edges:
            if "src" in edge and "dst" in edge:
                sig = (edge["src"], edge["dst"], edge.get("kind", ""))
                if sig not in seen_edges_signatures:
                    seen_edges_signatures.add(sig)
                    deduplicated_edges.append(edge)
        edges = deduplicated_edges
        
        # Track which endpoints have newly discovered edges in this active poll
        endpoints_with_new_edges = set()
        for edge in edges:
            if edge.get("kind") == "fdb" and edge.get("src"):
                endpoints_with_new_edges.add(edge["src"])

        # Keep connecting lines for nodes that weren't seen in this poll, avoiding duplicates
        for s_edge in stored_edges:
            if s_edge.get("src") and s_edge.get("dst"):
                # If this is an FDB edge, and we already found a new edge for this endpoint in this poll,
                # skip restoring the stale historical edge (unless it is a historical WiFi edge)!
                if s_edge.get("kind") == "fdb":
                    is_wifi = s_edge.get("attrs", {}).get("type") == "wifi"
                    if s_edge["src"] in endpoints_with_new_edges and not is_wifi:
                        continue
                    
                    dst_id = s_edge.get("dst", "")
                    if dst_id.startswith("device:"):
                        dst_name = dst_id.split(":", 1)[1]
                        if dst_name in switch_data:
                            # Verify if the bridge_port is valid for this switch
                            s_data = switch_data[dst_name]
                            valid_ports = set()
                            if_names = s_data.get("if_names") or []
                            if isinstance(if_names, (list, tuple)):
                                for item in if_names:
                                    if isinstance(item, (list, tuple)) and len(item) == 2:
                                        valid_ports.add(str(item[1]).strip().lower())
                            
                            bridge_port = s_edge.get("attrs", {}).get("bridge_port")
                            if bridge_port and str(bridge_port).strip().lower() not in valid_ports:
                                _LOGGER.info(
                                    "Discarding invalid historical edge: %s -> %s on port %s (port not valid on switch)",
                                    s_edge.get("src"), dst_id, bridge_port
                                )
                                continue
                    
                sig = (s_edge["src"], s_edge["dst"], s_edge.get("kind", ""))
                if sig not in seen_edges_signatures:
                    seen_edges_signatures.add(sig)
                    s_edge_copy = dict(s_edge)
                    s_edge_copy["attrs"] = dict(s_edge.get("attrs") or {})
                    s_edge_copy["attrs"]["is_historical"] = True
                    edges.append(s_edge_copy)
        
        # Startup Deduplication by MAC Address
        stored_nodes_by_mac = {}
        cleaned_stored_nodes = {}
        for nid, node in stored_nodes.items():
            mac = node.get("attrs", {}).get("mac", "").upper()
            if not mac:
                cleaned_stored_nodes[nid] = node
                continue
                
            if mac not in stored_nodes_by_mac:
                stored_nodes_by_mac[mac] = (nid, node)
                cleaned_stored_nodes[nid] = node
            else:
                # Found duplicate MAC in startup database
                existing_nid, existing_node = stored_nodes_by_mac[mac]
                new_ip = node.get("attrs", {}).get("ip", "Unknown")
                new_name = node.get("attrs", {}).get("name", "")
                
                # Apply standardized deduplication rules
                self._apply_node_deduplication(existing_node, new_ip, new_name, mac, context="Startup")
                
                if node.get("kind") == "device" and existing_node.get("kind") != "device":
                    existing_node["kind"] = "device"
                    
                # Remap edges pointing to the duplicate node
                for edge in edges:
                    if edge.get("src") == nid:
                        edge["src"] = existing_nid
                    if edge.get("dst") == nid:
                        edge["dst"] = existing_nid
                        
        stored_nodes = cleaned_stored_nodes

        # Merge with stored nodes
        merged_nodes = {}
        
        # Add all stored nodes as "down" initially
        nodes_to_ping = []
        for node_id, node in stored_nodes.items():
            mac = node.get("attrs", {}).get("mac", "")
            mac_upper = mac.upper()
            
            # If the stored node has no IP or Unknown IP, try to resolve it from mac_to_ip
            current_ip = node.get("attrs", {}).get("ip", "Unknown")
            if (not current_ip or current_ip == "Unknown") and mac_upper in mac_to_ip:
                node["attrs"]["ip"] = mac_to_ip[mac_upper]
                _LOGGER.debug("Resolved stored node %s IP to %s from ARP table", node_id, mac_to_ip[mac_upper])
                current_ip = mac_to_ip[mac_upper]
                
            # Resolve/Update friendly name from DNS if the IP is known
            current_name = node.get("attrs", {}).get("name", "")
            if current_ip != "Unknown":
                dns_name = await self._async_resolve_dns(current_ip, mac_to_name, mac_upper)
                
                ha_device = get_device_by_mac(dev_reg, mac)
                if ha_device and ha_device.name_by_user:
                    node["attrs"]["name"] = format_friendly_name(ha_device.name_by_user)
                elif dns_name != current_ip and current_ip != "Unknown" and not is_ip_address(dns_name):
                    friendly_name = format_friendly_name(dns_name.split(".")[0])
                    is_mac_or_empty = not current_name or bool(re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', current_name)) or is_ip_address(current_name) or current_name in ["10", "192"]
                    if is_mac_or_empty or friendly_name != current_name:
                        node["attrs"]["name"] = friendly_name
                        _LOGGER.debug("Resolved stored node %s name to %s from reverse DNS", node_id, friendly_name)
                elif ha_device and ha_device.name:
                    is_mac_or_empty = not current_name or bool(re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', current_name)) or is_ip_address(current_name) or current_name in ["10", "192"]
                    if is_mac_or_empty:
                        node["attrs"]["name"] = format_friendly_name(ha_device.name)

            if node["kind"] == "endpoint" or node.get("attrs", {}).get("is_ap"):
                ip = node["attrs"].get("ip")
                if ip and ip != "Unknown" and mac_upper not in active_macs:
                    nodes_to_ping.append((node_id, ip))
            merged_nodes[node_id] = {
                **node,
                "attrs": {**node.get("attrs", {}), "state": "down"}
            }
            
        if nodes_to_ping:
            _LOGGER.info("Pinging %d stored endpoints to verify state...", len(nodes_to_ping))
            # Run pings in parallel
            results = await asyncio.gather(*(self._async_ping_device(ip) for node_id, ip in nodes_to_ping))
            for (node_id, ip), success in zip(nodes_to_ping, results):
                if success:
                    merged_nodes[node_id]["attrs"]["state"] = "up"
                    _LOGGER.debug("Pinger verified %s is UP", ip)
            
        # Apply active presence from ARP and FDB walks to update state
        for node_id, node in merged_nodes.items():
            if node.get("kind") == "endpoint":
                mac = node.get("attrs", {}).get("mac", "").upper()
                if mac in active_macs:
                    node["attrs"]["state"] = "up"
            
        # Overwrite with newly discovered nodes (mark as "up") with discovery deduplication
        for node_id, node in nodes.items():
            mac = node.get("attrs", {}).get("mac", "").upper()
            
            existing_nid = None
            if mac:
                if node_id in merged_nodes:
                    existing_nid = node_id
                else:
                    for nid, m_node in merged_nodes.items():
                        if m_node.get("attrs", {}).get("mac", "").upper() == mac:
                            existing_nid = nid
                            break
            
            if existing_nid:
                existing_node = merged_nodes[existing_nid]
                new_ip = node.get("attrs", {}).get("ip", "Unknown")
                new_name = node.get("attrs", {}).get("name", "")
                
                # Apply standardized deduplication rules
                self._apply_node_deduplication(existing_node, new_ip, new_name, mac, context="Discovery")
                existing_name = existing_node["attrs"].get("name", "")

                ha_device = get_device_by_mac(dev_reg, mac)
                if ha_device and ha_device.name_by_user:
                    user_name = format_friendly_name(ha_device.name_by_user)
                    if existing_name != user_name:
                        _LOGGER.debug("Deduplication Discovery [User Override]: updating name of MAC %s from '%s' to '%s'", mac, existing_name, user_name)
                        existing_node["attrs"]["name"] = user_name
                        existing_name = user_name
                elif new_name and new_name != existing_name:
                    is_current_generic = not existing_name or bool(re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', existing_name)) or is_ip_address(existing_name) or existing_name in ["10", "192"]
                    if is_current_generic or (not (ha_device and ha_device.name_by_user)):
                        _LOGGER.debug("Deduplication Discovery [Name update]: updating name of MAC %s from '%s' to '%s'", mac, existing_name, new_name)
                        existing_node["attrs"]["name"] = format_friendly_name(new_name)
                        existing_name = existing_node["attrs"]["name"]
                
                existing_node["attrs"]["state"] = "up"
                # Merge other attributes
                for k, v in node.get("attrs", {}).items():
                    if k not in ["name", "ip", "state"]:
                        existing_node["attrs"][k] = v
                if node.get("kind") == "device" and existing_node.get("kind") != "device":
                    existing_node["kind"] = "device"
            else:
                merged_nodes[node_id] = {
                    **node,
                    "attrs": {**node.get("attrs", {}), "state": "up"}
                }
            
        # Delete switch interface MACs from merged_nodes and edges
        for nid in list(merged_nodes.keys()):
            node = merged_nodes[nid]
            mac = node.get("attrs", {}).get("mac", "")
            if mac and mac.upper() in known_macs:
                _LOGGER.debug("Cleaning up switch interface node %s", nid)
                del merged_nodes[nid]
        edges = [edge for edge in edges if edge["src"] in merged_nodes and edge["dst"] in merged_nodes]

        # Forcefully tag switches to ensure pop-up works even for stored nodes
        for node_id, node in merged_nodes.items():
            if node.get("kind") == "device":
                name = node.get("attrs", {}).get("name", "")
                is_ap_by_name = is_access_point_name(name) or node.get("attrs", {}).get("is_ap")
                if name and not is_ap_by_name and not node.get("attrs", {}).get("is_gateway"):
                    node["attrs"]["integration"] = "snmp_switch_manager"
                    node["attrs"]["device_slug"] = name.lower().replace("-", "_").replace(" ", "_").replace(".", "_")
            
        # 4. Ping unconnected devices that have IPs to wake them up
        unconnected_ips = []
        for node_id, node in merged_nodes.items():
            if node["kind"] == "endpoint":
                # Check if this node has edges
                has_edge = False
                for edge in edges:
                    if edge["src"] == node_id or edge["dst"] == node_id:
                        has_edge = True
                        break
                if not has_edge:
                    ip = node["attrs"].get("ip")
                    if ip and ip != "Unknown":
                        unconnected_ips.append(ip)
                        
        if unconnected_ips:
            _LOGGER.info("Pinging %d unconnected devices to wake them up...", len(unconnected_ips))
            for ip in unconnected_ips:
                self.hass.async_create_background_task(
                    self._async_ping_device(ip),
                    f"ping_{ip}"
                )
                

            
        # 5. Generic Wireless Client Association: Map WiFi clients using live HA states across any router/AP integration
        wireless_overrides = 0
        try:
            # Build set of all known infrastructure MACs (switches, APs, gateways) to never duplicate as endpoints
            infra_macs = set()
            for nid, node in merged_nodes.items():
                if node.get("kind") == "device" or node.get("attrs", {}).get("is_ap") or node.get("attrs", {}).get("is_gateway"):
                    m = node.get("attrs", {}).get("mac")
                    if m:
                        infra_macs.add(m.replace(":", "").replace("-", "").upper())
            for m in known_macs:
                infra_macs.add(m.replace(":", "").replace("-", "").upper())

            for state in self.hass.states.async_all():
                if state.entity_id.startswith("device_tracker."):
                    if state.state not in ["home", "up"]:
                        continue

                    is_wired = (
                        state.attributes.get("is_wired") is True
                        or state.attributes.get("connection_type") == "wired"
                        or state.attributes.get("network_type") == "wired"
                    )
                    if is_wired:
                        continue

                    # Look for AP MAC, BSSID, or AP name across any integration
                    ap_mac = (
                        state.attributes.get("ap_mac")
                        or state.attributes.get("bssid")
                        or state.attributes.get("ap_bssid")
                        or state.attributes.get("ap_mac_address")
                        or state.attributes.get("node_mac")
                    )
                    ap_name = (
                        state.attributes.get("access_point")
                        or state.attributes.get("ap_name")
                        or state.attributes.get("connected_to")
                        or state.attributes.get("mesh_node")
                    )

                    # Only process entities that explicitly report an AP association
                    if not ap_mac and not ap_name:
                        continue

                    client_mac = state.attributes.get("mac")
                    if not client_mac:
                        continue

                    clean_c_mac = client_mac.replace(":", "").replace("-", "").upper()
                    # Do not treat APs or switches as wireless clients
                    if clean_c_mac in infra_macs:
                        continue

                    # Find the AP device in HA
                    ap_device = None
                    if ap_mac:
                        ap_device = get_device_by_mac(dev_reg, ap_mac)
                        if not ap_device:
                            # Match first 5 bytes prefix (common for multi-BSSID radios)
                            clean_ap_mac = ap_mac.replace(":", "").replace("-", "").lower()
                            if len(clean_ap_mac) >= 10:
                                ap_prefix = clean_ap_mac[:10]
                                for dev in dev_reg.devices:
                                    for conn in dev.connections:
                                        if conn[0] == dr.CONNECTION_NETWORK_MAC and conn[1]:
                                            c_mac = conn[1].replace(":", "").replace("-", "").lower()
                                            if c_mac.startswith(ap_prefix):
                                                ap_device = dev
                                                break
                                    if ap_device:
                                        break
                    if not ap_device and ap_name:
                        norm_ap_name = normalize_name(ap_name)
                        for dev in dev_reg.devices:
                            if dev.name and normalize_name(dev.name) == norm_ap_name:
                                ap_device = dev
                                break

                    ap_node_id = None
                    if ap_device:
                        # Find AP node in merged_nodes by device_id
                        for nid, node in merged_nodes.items():
                            if node.get("attrs", {}).get("device_id") == ap_device.id:
                                ap_node_id = nid
                                node.setdefault("attrs", {})["state"] = "up"
                                node["attrs"]["is_ap"] = True
                                break

                    # If not matched by device_id, match by MAC directly in merged_nodes
                    if not ap_node_id and ap_mac:
                        clean_ap_mac = ap_mac.replace(":", "").replace("-", "").upper()
                        for nid, node in merged_nodes.items():
                            n_mac = (node.get("attrs", {}).get("mac") or "").replace(":", "").replace("-", "").upper()
                            if n_mac and n_mac == clean_ap_mac:
                                ap_node_id = nid
                                node.setdefault("attrs", {})["state"] = "up"
                                node["attrs"]["is_ap"] = True
                                break

                    # If not matched by MAC, match by name directly in merged_nodes
                    if not ap_node_id and ap_name:
                        norm_ap_name = normalize_name(ap_name)
                        for nid, node in merged_nodes.items():
                            n_name = normalize_name(node.get("attrs", {}).get("name", ""))
                            if n_name == norm_ap_name or (n_name and norm_ap_name and n_name.replace("ap", "") == norm_ap_name.replace("ap", "")):
                                ap_node_id = nid
                                node.setdefault("attrs", {})["state"] = "up"
                                node["attrs"]["is_ap"] = True
                                break

                    if not ap_node_id:
                        clean_ap_mac = ap_mac.replace(":", "").replace("-", "").upper() if ap_mac else ""
                        ap_node_id = f"device:ap:{clean_ap_mac}" if clean_ap_mac else f"device:{normalize_name(ap_device.name if ap_device else (ap_name or 'AP'))}"
                        merged_nodes[ap_node_id] = {
                            "kind": "device",
                            "attrs": {
                                "name": (ap_device.name if ap_device else ap_name) or "Wireless Access Point",
                                "is_ap": True,
                                "device_id": ap_device.id if ap_device else None,
                                "mac": ap_mac.upper() if ap_mac else "",
                                "ip": "Unknown",
                                "state": "up"
                            }
                        }

                    # Check if client already exists in merged_nodes by normalized MAC
                    client_node_id = None
                    for nid, node in merged_nodes.items():
                        n_mac = (node.get("attrs", {}).get("mac") or "").replace(":", "").replace("-", "").upper()
                        if n_mac and n_mac == clean_c_mac:
                            client_node_id = nid
                            break

                    if not client_node_id:
                        formatted_mac = parse_mac(client_mac) or client_mac.upper()
                        client_node_id = f"endpoint:{formatted_mac}"
                        client_name = state.attributes.get("host_name") or state.attributes.get("friendly_name") or formatted_mac
                        merged_nodes[client_node_id] = {
                            "kind": "endpoint",
                            "attrs": {
                                "name": format_friendly_name(client_name),
                                "mac": formatted_mac,
                                "ip": state.attributes.get("ip") or "Unknown",
                                "state": "up"
                            }
                        }
                    else:
                        merged_nodes[client_node_id].setdefault("attrs", {})["state"] = "up"

                    active_macs.add(clean_c_mac)

                    if ap_node_id and client_node_id in merged_nodes:
                        # Authoritative live wireless connection: remove existing edges for this client
                        edges = [edge for edge in edges if edge.get("src") != client_node_id]
                        edges.append({
                            "kind": "fdb",
                            "src": client_node_id,
                            "dst": ap_node_id,
                            "attrs": {"type": "wifi"}
                        })
                        wireless_overrides += 1
                        _LOGGER.debug("Mapped wireless client %s to AP %s via integration attribute", client_mac, ap_node_id)

            if wireless_overrides > 0:
                _LOGGER.info("Successfully mapped %d wireless clients to APs using live state data", wireless_overrides)
        except Exception as e:
            _LOGGER.error("Error during generic wireless mapping: %s", e)
            
        # 5.5 AP Uplink/Mesh Overrides: Use _uplink_mac sensors from HA to map AP parent uplinks
        try:
            for nid, node in list(merged_nodes.items()):
                if node.get("attrs", {}).get("is_ap"):
                    dev_id = node.get("attrs", {}).get("device_id")
                    if not dev_id:
                        # Fallback search by name
                        name = node.get("attrs", {}).get("name", "")
                        if name:
                            norm_name = name.lower().replace(" ", "").replace("-", "")
                            for dev in dev_reg.devices:
                                if dev.name and dev.name.lower().replace(" ", "").replace("-", "") == norm_name:
                                    dev_id = dev.id
                                    break
                                    
                    if dev_id:
                        dev_entities = er.async_entries_for_device(ent_reg, dev_id)
                        for ent_entry in dev_entities:
                            if ent_entry.entity_id.endswith("_uplink_mac"):
                                state_val = self.hass.states.get(ent_entry.entity_id)
                                if state_val and state_val.state and state_val.state not in ["unknown", "unavailable"]:
                                    parent_mac = state_val.state.upper().replace(":", "").replace("-", "")
                                    if len(parent_mac) == 12:
                                        # Find the parent AP or switch node by MAC
                                        parent_nid = None
                                        for possible_nid, possible_node in merged_nodes.items():
                                            pmac = possible_node.get("attrs", {}).get("mac", "").upper().replace(":", "").replace("-", "")
                                            if pmac == parent_mac:
                                                parent_nid = possible_nid
                                                break
                                                
                                        if parent_nid and parent_nid != nid:
                                            # Remove existing uplink edges where this AP is the source
                                            edges = [edge for edge in edges if edge["src"] != nid]
                                            
                                            # Add the new parent edge
                                            is_parent_ap = merged_nodes[parent_nid].get("attrs", {}).get("is_ap", False)
                                            edges.append({
                                                "kind": "fdb" if is_parent_ap else "lldp",
                                                "src": nid if is_parent_ap else parent_nid,
                                                "dst": parent_nid if is_parent_ap else nid,
                                                "attrs": {
                                                    "type": "mesh" if is_parent_ap else "ethernet",
                                                    "bridge_port": "Mesh Uplink" if is_parent_ap else "Uplink"
                                                }
                                            })
                                            _LOGGER.info("Overrode AP %s uplink parent to %s via uplink_mac sensor", nid, parent_nid)
                                            break
        except Exception as mesh_err:
            _LOGGER.error("Error during AP mesh override: %s", mesh_err)

        # 5.6 Infrastructure Endpoint Purge: Ensure no AP or Switch MAC ever exists as an endpoint
        infra_macs_all = set()
        for nid, node in merged_nodes.items():
            if node.get("kind") == "device" or node.get("attrs", {}).get("is_ap") or node.get("attrs", {}).get("is_gateway"):
                m = node.get("attrs", {}).get("mac")
                if m:
                    infra_macs_all.add(m.replace(":", "").replace("-", "").upper())
        for nid in list(merged_nodes.keys()):
            node = merged_nodes[nid]
            if node.get("kind") == "endpoint":
                m = (node.get("attrs", {}).get("mac") or "").replace(":", "").replace("-", "").upper()
                name = normalize_name(node.get("attrs", {}).get("name", ""))
                is_infra = m in infra_macs_all or (name and (name.startswith("ap") or name.startswith("switch") or "gateway" in name))
                if is_infra:
                    _LOGGER.info("Purging duplicate endpoint node %s (%s) representing infrastructure device", nid, node.get("attrs", {}).get("name"))
                    del merged_nodes[nid]
                    edges = [e for e in edges if e.get("src") != nid and e.get("dst") != nid]

        # 7. Rename ALL AP nodes to "device:ap:..." to satisfy card layout first
        for nid, node in list(merged_nodes.items()):
            if node["attrs"].get("is_ap"):
                node["kind"] = "device"
                ap_mac_val = node["attrs"].get("mac")
                if ap_mac_val:
                    clean_ap_mac = ap_mac_val.upper().replace(":", "").replace("-", "")
                    new_nid = f"device:ap:{clean_ap_mac}"
                    if nid != new_nid:
                        _LOGGER.debug("Renaming AP node %s to %s to satisfy card layout", nid, new_nid)
                        merged_nodes[new_nid] = node
                        del merged_nodes[nid]
                        
                        # Remap edges for the renamed node
                        for edge in edges:
                            if edge["src"] == nid:
                                edge["src"] = new_nid
                            if edge["dst"] == nid:
                                edge["dst"] = new_nid

        # 6. Strict Deduplication for Parent Devices (Switches, APs, Gateways)
        # Multi-NIC Endpoints retain separate nodes, but parent infrastructure nodes are consolidated into 1 single node.
        parent_groups = {}
        for nid, node in list(merged_nodes.items()):
            attrs = node.get("attrs", {})
            is_parent = (
                node.get("kind") == "device" or
                attrs.get("is_ap") or
                attrs.get("is_gateway") or
                attrs.get("integration") == "snmp_switch_manager"
            )
            if is_parent:
                dev_id = attrs.get("device_id")
                mac_clean = attrs.get("mac", "").upper().replace(":", "").replace("-", "") if attrs.get("mac") else ""
                name_clean = normalize_name(attrs.get("name", "")) if attrs.get("name") else ""
                
                # Deduplication key for parent infrastructure
                group_key = dev_id or (f"mac:{mac_clean}" if mac_clean else (f"name:{name_clean}" if name_clean else None))
                if group_key:
                    parent_groups.setdefault(group_key, []).append(nid)

        for group_key, nids in parent_groups.items():
            if len(nids) > 1:
                # Prefer device:ap:... for APs, device:... for switches/gateways
                best_nid = None
                for nid in nids:
                    if nid.startswith("device:ap:"):
                        best_nid = nid
                        break
                if not best_nid:
                    for nid in nids:
                        if nid.startswith("device:"):
                            best_nid = nid
                            break
                if not best_nid:
                    best_nid = nids[0]

                best_node = merged_nodes[best_nid]
                for nid in nids:
                    if nid != best_nid and nid in merged_nodes:
                        _LOGGER.info("Consolidating duplicate parent device node %s into canonical node %s", nid, best_nid)
                        duplicate_node = merged_nodes.pop(nid)
                        # Merge attributes into canonical best_node
                        for k, v in duplicate_node.get("attrs", {}).items():
                            if v and not best_node.get("attrs", {}).get(k):
                                best_node["attrs"][k] = v
                        # Remap all connected edges
                        for edge in edges:
                            if edge["src"] == nid:
                                edge["src"] = best_nid
                            if edge["dst"] == nid:
                                edge["dst"] = best_nid

        # Ensure all configured switches have their correct IP address set
        for name, client, entry in clients:
            switch_id = f"device:{name}"
            if switch_id in merged_nodes:
                attrs = merged_nodes[switch_id].setdefault("attrs", {})
                current_ip = attrs.get("ip")
                configured_ip = getattr(client, "host", None) or (entry.data or {}).get("host")
                if configured_ip and (not current_ip or current_ip == "Unknown"):
                    attrs["ip"] = configured_ip
                                
        # 7.5 HA Device Correlation and State Resolution (Option A & Asleep State)
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        options = entry.options if entry else {}

        for node_id, node in merged_nodes.items():
            attrs = node.setdefault("attrs", {})
            mac = attrs.get("mac", "").upper()
            name = attrs.get("name")
            ip = attrs.get("ip", "Unknown")
            
            matching_device = None
            mac_key = mac.replace(":", "").replace("-", "").upper() if mac else ""
            
            # Check manual device mappings override first
            is_explicitly_unlinked = False
            if mac_key and mac_key in self.manual_device_mappings:
                manual_device_id = self.manual_device_mappings[mac_key]
                if manual_device_id == "unlinked" or not manual_device_id:
                    is_explicitly_unlinked = True
                else:
                    manual_dev = dev_reg.async_get(manual_device_id)
                    matching_device = get_primary_device(dev_reg, ent_reg, manual_dev)
            
            if not matching_device and not is_explicitly_unlinked:
                matching_device = find_correlated_device(self.hass, mac, ip, name)
                                
            # Disassociate stale connection on incompatible devices to prevent automatic HA registry merge
            if mac and name:
                normalized_mac = mac.replace(":", "").replace("-", "").upper()
                old_device = get_device_by_mac(dev_reg, mac)
                
                if old_device and old_device.name and (not matching_device or matching_device.id != old_device.id):
                    n1 = normalize_name(name)
                    n2 = normalize_name(old_device.name)
                    # Only enforce name check for personal roaming devices (laptops, phones, tablets, etc.)
                    roaming_keywords = ["laptop", "phone", "ipad", "iphone", "macbook", "workstation", "pc", "desktop", "tablet", "computer"]
                    is_roaming = any(k in n1 or k in n2 for k in roaming_keywords)
                    if is_roaming and self.entry_id in get_device_config_entry_ids(old_device):
                        if len(n1) > 2 and len(n2) > 2 and n1 not in n2 and n2 not in n1:
                            _LOGGER.info(
                                "Disassociating stale connection/identifier for MAC %s from incompatible device %s (%s)",
                                mac, old_device.name, old_device.id
                            )
                            new_connections = {c for c in old_device.connections if c[1].replace(":", "").replace("-", "").upper() != normalized_mac}
                            new_identifiers = {i for i in old_device.identifiers if i[1].replace(":", "").replace("-", "").upper() != normalized_mac}
                            if not new_connections and not new_identifiers:
                                _LOGGER.info("Removing empty orphaned device %s (%s)", old_device.name, old_device.id)
                                dev_reg.async_remove_device(old_device.id)
                            else:
                                dev_reg.async_update_device(old_device.id, new_connections=new_connections, new_identifiers=new_identifiers)
                        
            if not matching_device and name:
                # Match by normalized name
                target_name = normalize_name(name)
                for dev in dev_reg.devices:
                    if dev.name and normalize_name(dev.name) == target_name:
                        matching_device = get_primary_device(dev_reg, ent_reg, dev)
                        break
                        
            if matching_device:
                if matching_device.name_by_user:
                    attrs["name"] = format_friendly_name(matching_device.name_by_user)
                elif matching_device.name:
                    attrs["name"] = format_friendly_name(matching_device.name)
                attrs["device_id"] = matching_device.id

                # Sync the primary IP of the device from its HA entity states
                dev_entities = get_all_device_entities(ent_reg, dev_reg, matching_device, include_disabled=True)
                primary_ip = extract_ip_from_entities(self.hass, dev_entities)
                if primary_ip:
                    attrs["ip"] = primary_ip
                dev_entities = get_all_device_entities(ent_reg, dev_reg, matching_device, include_disabled=False)
                attrs["state"] = resolve_device_state(self.hass, matching_device, dev_entities, attrs.get("state", "down"))
            
            # Update last_seen tracking
            state_val = attrs.get("state", "down")
            if state_val in ["up", "asleep", "away"]:
                attrs["last_seen"] = time.time()
            else:
                if "last_seen" not in attrs:
                    attrs["last_seen"] = time.time()

        # 7.6 Apply Config Flow Exclusions
        import ipaddress
        
        exclude_macs = {m.strip().upper().replace(":", "").replace("-", "") for m in options.get("exclude_macs", "").split(",") if m.strip()}
        
        exclude_ips = []
        exclude_subnets = []
        for val in options.get("exclude_ips", "").split(","):
            val = val.strip()
            if not val:
                continue
            if "/" in val:
                try:
                    exclude_subnets.append(ipaddress.ip_network(val, strict=False))
                except ValueError:
                    _LOGGER.warning("Invalid excluded IP subnet: %s", val)
            else:
                try:
                    exclude_ips.append(ipaddress.ip_address(val))
                except ValueError:
                    _LOGGER.warning("Invalid excluded IP address: %s", val)
                    
        exclude_names = [n.strip().lower() for n in options.get("exclude_names", "").split(",") if n.strip()]
        
        def is_excluded(mac_val, ip_val, name_val) -> bool:
            if mac_val:
                norm_mac = mac_val.upper().replace(":", "").replace("-", "")
                if norm_mac in exclude_macs:
                    return True
            if ip_val and ip_val != "Unknown":
                try:
                    addr = ipaddress.ip_address(ip_val)
                    if addr in exclude_ips:
                        return True
                    for net in exclude_subnets:
                        if addr in net:
                            return True
                except ValueError:
                    pass
            if name_val:
                n_lower = name_val.lower()
                for pattern in exclude_names:
                    if pattern in n_lower:
                        return True
            return False

        excluded_nids = set()
        for nid, node in list(merged_nodes.items()):
            attrs = node.get("attrs", {})
            mac_val = attrs.get("mac")
            ip_val = attrs.get("ip")
            name_val = attrs.get("name")
            
            if is_excluded(mac_val, ip_val, name_val):
                _LOGGER.info("Excluding node %s (MAC: %s, IP: %s, Name: %s) based on configuration rules", nid, mac_val, ip_val, name_val)
                excluded_nids.add(nid)
                if nid in merged_nodes:
                    del merged_nodes[nid]
                    
        if excluded_nids:
            edges = [edge for edge in edges if edge["src"] not in excluded_nids and edge["dst"] not in excluded_nids]

        # 7.7 Clean up stale down devices
        stale_days = options.get("stale_days", 30)
        if stale_days > 0:
            stale_threshold_seconds = stale_days * 86400
            current_time = time.time()
            stale_nids = set()
            for nid, node in list(merged_nodes.items()):
                attrs = node.get("attrs", {})
                if attrs.get("state") == "down":
                    last_seen = attrs.get("last_seen", current_time)
                    if (current_time - last_seen) > stale_threshold_seconds:
                        _LOGGER.info("Removing stale node %s (offline for > %d days)", nid, stale_days)
                        stale_nids.add(nid)
                        if nid in merged_nodes:
                            del merged_nodes[nid]
            if stale_nids:
                edges = [edge for edge in edges if edge["src"] not in stale_nids and edge["dst"] not in stale_nids]

        # 8. Modular Device Registry Sync
        self.async_sync_device_registry(merged_nodes)
                    
        # Remove self-loops and duplicate edges
        edges = [e for e in edges if e.get("src") != e.get("dst")]
        
        def get_child_id(edge) -> str:
            kind = edge.get("kind")
            if kind == "lldp":
                return edge.get("dst")
            elif kind == "fdb":
                return edge.get("src")
            return None

        _LOGGER.debug("EDGES BEFORE PRUNING (count: %d):", len(edges))
        for edge in edges:
            _LOGGER.debug("  Edge before prune: %s (%s) -> %s | attrs: %s", edge.get("src"), edge.get("kind"), edge.get("dst"), edge.get("attrs"))

        # Prune duplicate uplink edges to ensure a single parent tree structure for each child node
        child_to_edges = {}
        for edge in edges:
            child_id = get_child_id(edge)
            if not child_id:
                continue
            if child_id not in child_to_edges:
                child_to_edges[child_id] = []
            child_to_edges[child_id].append(edge)
            
        pruned_edges = []
        for child_id, c_edges in child_to_edges.items():
            if len(c_edges) == 1:
                pruned_edges.append(c_edges[0])
            else:
                # Prioritize newly discovered edges over historical stored edges
                live_edges = [e for e in c_edges if not e.get("attrs", {}).get("is_historical")]
                active_pool = live_edges if live_edges else c_edges

                # Priority 1: LLDP edges
                lldp_edges = [e for e in active_pool if e.get("kind") == "lldp"]
                if lldp_edges:
                    # Prioritize parent nodes that are gateways
                    def sort_key(edge):
                        src = edge.get("src")
                        node = merged_nodes.get(src) or {}
                        is_gw = node.get("attrs", {}).get("is_gateway", False)
                        return 0 if is_gw else 1
                    lldp_edges.sort(key=sort_key)
                    pruned_edges.append(lldp_edges[0])
                    continue
                # Priority 2: Live WiFi edges from live AP mapping
                live_wifi_edges = [e for e in active_pool if e.get("kind") == "fdb" and e.get("attrs", {}).get("type") == "wifi"]
                if live_wifi_edges:
                    pruned_edges.append(live_wifi_edges[0])
                    continue
                # Priority 3: FDB edges with bridge_port info (wired)
                info_edges = [e for e in active_pool if e.get("attrs", {}).get("bridge_port") and e.get("attrs", {}).get("bridge_port") != "Unknown" and e.get("attrs", {}).get("type") != "wifi"]
                if info_edges:
                    pruned_edges.append(info_edges[0])
                    continue
                # Fallback: Just keep the first one
                pruned_edges.append(active_pool[0])
                
        other_edges = [e for e in edges if not get_child_id(e)]
        edges = pruned_edges + other_edges

        seen_edges_signatures = set()
        deduplicated_edges = []
        for edge in edges:
            if "src" in edge and "dst" in edge:
                sig = (edge["src"], edge["dst"], edge.get("kind", ""))
                if sig not in seen_edges_signatures:
                    seen_edges_signatures.add(sig)
                    deduplicated_edges.append(edge)
        edges = deduplicated_edges

        _LOGGER.debug("EDGES AFTER PRUNING (count: %d):", len(edges))
        for edge in edges:
            _LOGGER.debug("  Edge after prune: %s (%s) -> %s | attrs: %s", edge.get("src"), edge.get("kind"), edge.get("dst"), edge.get("attrs"))

        # Clean up bridge_port for wireless clients to consistently show "Link" in UI
        for edge in edges:
            dst_node = merged_nodes.get(edge.get("dst"))
            src_node = merged_nodes.get(edge.get("src"))
            if dst_node and src_node:
                if dst_node.get("attrs", {}).get("is_ap") and src_node.get("kind") == "endpoint":
                    edge.setdefault("attrs", {})["type"] = "wifi"
                    if "bridge_port" in edge["attrs"]:
                        del edge["attrs"]["bridge_port"]

        # Build snapshot
        # Ensure all endpoint nodes have friendly formatted names before snapshot creation
        for nid, node in merged_nodes.items():
            if node.get("kind") == "endpoint":
                n_attrs = node.setdefault("attrs", {})
                if n_attrs.get("name"):
                    n_attrs["name"] = format_friendly_name(n_attrs["name"])

        snapshot = {
            "snapshot_id": f"snap_{int(time.time())}",
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 300)),
            "ttl_seconds": 300,
            "seed_device_ids": [],
            "stats": {
                "switches_polled": len(clients),
                "endpoints_found": len([n for n in merged_nodes.values() if n["kind"] == "endpoint"])
            },
            "sources": ["snmp"],
            "graph": {
                "nodes": merged_nodes,
                "edges": edges
            }
        }
        
        # Track state changes history
        try:
            old_nodes = stored_data.get("nodes", {})
            state_history = stored_data.setdefault("state_history", {})
            
            history_days = options.get("history_days", 30)
            history_retention_sec = history_days * 86400
            now = time.time()
            
            for nid, node in merged_nodes.items():
                attrs = node.setdefault("attrs", {})
                new_state = attrs.get("state", "down")
                
                old_node = old_nodes.get(nid, {})
                old_attrs = old_node.get("attrs", {})
                old_state = old_attrs.get("state")
                
                node_history = state_history.setdefault(nid, [])
                
                # Check if state has changed
                if not node_history:
                    # Seed with last_seen if available, fallback to now
                    seed_time = attrs.get("last_seen", now)
                    node_history.append({
                        "state": new_state,
                        "timestamp": seed_time
                    })
                    _LOGGER.info("Seeding initial state history for node %s to state %s at timestamp %s", nid, new_state, seed_time)
                elif old_state and old_state != new_state:
                    node_history.append({
                        "state": new_state,
                        "timestamp": now
                    })
                    _LOGGER.info("Device state change: %s -> %s for node %s", old_state, new_state, nid)
                
                # Clean up history entries older than configured retention period
                node_history = [
                    h for h in node_history
                    if now - h.get("timestamp", 0) <= history_retention_sec
                ]
                
                # Cap entries to prevent excessive storage size
                if len(node_history) > 50:
                    node_history = node_history[-50:]
                    
                state_history[nid] = node_history
                
                # Attach recent history (last 10 events) to node attributes to send to frontend
                attrs["state_history"] = node_history
        except Exception as history_err:
            _LOGGER.error("Error updating device state history: %s", history_err)

        # Save back to store (including snapshot for API and skip lists)
        stored_data["nodes"] = merged_nodes
        stored_data["last_snapshot"] = snapshot
        stored_data["unsupported_fdb_devices"] = list(self.unsupported_fdb_devices)
        await self.store.async_save(stored_data)
        
        # Dispatch discovered device trackers to the device_tracker platform
        discovered_devices = []
        update_devices = {}
        for node_id, node in merged_nodes.items():
            if node.get("kind") == "endpoint" and "mac" in node.get("attrs", {}):
                attrs = node["attrs"]
                mac = attrs["mac"].upper()
                name = attrs.get("name")
                ip = attrs.get("ip", "Unknown")
                
                # Find connected switch and port by tracing edges (src is endpoint, dst is switch)
                switch_name = "Unknown"
                port_name = "Unknown"
                for edge in edges:
                    if edge.get("src") == node_id:
                        dst_node = merged_nodes.get(edge.get("dst"))
                        if dst_node:
                            switch_name = dst_node.get("attrs", {}).get("name", "Unknown")
                        port_name = edge.get("attrs", {}).get("bridge_port", "Unknown")
                        break
                
                # format_friendly_name defined at module level

                friendly_name = format_friendly_name(name)
                
                dev_data = {
                    "mac": mac,
                    "name": friendly_name,
                    "ip": ip,
                    "switch_name": switch_name,
                    "port_name": port_name,
                    "state": attrs.get("state", "down")
                }
                discovered_devices.append(dev_data)
                update_devices[mac] = dev_data

        if self.entry_id:
            from homeassistant.helpers.dispatcher import async_dispatcher_send
            new_device_signal = f"snmp_network_manager_new_device_{self.entry_id}"
            update_signal = f"snmp_network_manager_update_device_{self.entry_id}"
            
            # Send new devices first so they get created, then send updates to update their states
            async_dispatcher_send(self.hass, new_device_signal, discovered_devices)
            async_dispatcher_send(self.hass, update_signal, update_devices)
        
        _LOGGER.info("Successfully completed fetch_topology. Saved snapshot with %d edges.", len(edges))
        return snapshot

    async def get_last_snapshot(self):
        """Get the last constructed snapshot from store."""
        stored_data = await self.store.async_load() or {}
        snapshot = stored_data.get("last_snapshot", {
            "graph": {"nodes": {}, "edges": []}
        })
        return snapshot

    async def _async_ping_device(self, ip):
        """Ping a device to wake it up."""
        try:
            import asyncio
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", "1", ip,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            return proc.returncode == 0
        except Exception:
            return False

    def async_sync_device_registry(self, merged_nodes: dict) -> None:
        """Sync HA Device and Entity Registry entries with merged_nodes topology."""
        try:
            device_registry = dr.async_get(self.hass)
            entity_registry = er.async_get(self.hass)
            from .const import DOMAIN
            
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            options = entry.options if entry else {}
            
            exclude_macs = {m.strip().upper().replace(":", "").replace("-", "") for m in options.get("exclude_macs", "").split(",") if m.strip()}
            
            # Build set of ALL active/known topology MACs, device IDs, and node names in merged_nodes
            topology_macs = set()
            topology_device_ids = set()
            topology_names = set()

            for node in merged_nodes.values():
                attrs = node.get("attrs", {})
                mac_attr = attrs.get("mac")
                if mac_attr:
                    topology_macs.add(mac_attr.replace(":", "").replace("-", "").upper())
                dev_id_attr = attrs.get("device_id")
                if dev_id_attr:
                    topology_device_ids.add(dev_id_attr)
                name_attr = attrs.get("name")
                if name_attr:
                    topology_names.add(normalize_name(name_attr))
                        
            for dev in list(device_registry.devices):
                if self.entry_id in get_device_config_entry_ids(dev):
                    dev_macs = []
                    for conn_type, conn_val in dev.connections:
                        if conn_type == dr.CONNECTION_NETWORK_MAC:
                            dev_macs.append(conn_val.replace(":", "").replace("-", "").upper())
                            
                    is_active = False
                    if dev.id in topology_device_ids:
                        is_active = True
                    elif dev_macs:
                        for mac in dev_macs:
                            if mac in topology_macs:
                                is_active = True
                                break
                    elif dev.name and normalize_name(dev.name) in topology_names:
                        is_active = True
                                
                    should_remove = False
                    
                    # 1. If it has any excluded MAC, remove it
                    if dev_macs and any(mac in exclude_macs for mac in dev_macs):
                        should_remove = True
                        _LOGGER.info("Registry cleanup: Device %s has excluded MAC. Removing...", dev.name)
                    # 2. If it is NOT in our topology map (e.g., purged by stale_days), remove it
                    elif not is_active:
                        should_remove = True
                        _LOGGER.info("Registry cleanup: Device %s is no longer in topology map. Removing...", dev.name)
                    # 3. If it has been renamed from a MAC-named entry
                    elif dev.name and re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', dev.name):
                        name_upper = dev.name.replace(":", "").replace("-", "").upper()
                        for node in merged_nodes.values():
                            n_mac = node.get("attrs", {}).get("mac", "").replace(":", "").replace("-", "").upper()
                            if n_mac == name_upper:
                                active_name = node.get("attrs", {}).get("name", "")
                                if active_name and active_name != dev.name:
                                    should_remove = True
                                    _LOGGER.info("Registry cleanup: Device %s has been renamed to %s. Removing old MAC-named entry...", dev.name, active_name)
                                    break
                                    
                    if should_remove:
                        dev_entities = er.async_entries_for_device(entity_registry, dev.id, include_disabled_entities=True)
                        for entity in dev_entities:
                            if entity.platform == DOMAIN:
                                _LOGGER.info("Registry cleanup: Removing entity %s for device %s", entity.entity_id, dev.name)
                                entity_registry.async_remove(entity.entity_id)
                                
                        device_registry.async_remove_device(dev.id)
                            
            # Dispatch new device signal to ensure any newly added endpoints in merged_nodes are added as device trackers
            from homeassistant.helpers.dispatcher import async_dispatcher_send
            new_devices_to_add = []
            for nid, node in merged_nodes.items():
                if node.get("kind") == "endpoint":
                    attrs = node.get("attrs", {})
                    mac = attrs.get("mac")
                    if mac:
                        new_devices_to_add.append({
                            "mac": mac,
                            "name": attrs.get("name"),
                            "ip": attrs.get("ip", "Unknown"),
                            "switch_name": "Unknown",
                            "port_name": "Unknown"
                        })
            if new_devices_to_add:
                async_dispatcher_send(self.hass, f"snmp_network_manager_new_device_{self.entry_id}", new_devices_to_add)

        except Exception as e:
            _LOGGER.error("Failed to run device registry sync: %s", e)

    async def _async_fetch_switch_raw_data(self, name, client):
        """Fetch all raw SNMP MIB tables/OIDs for a single switch concurrently."""
        try:
            results = await asyncio.gather(
                client._async_walk(OID_ARP_MAC),                        # 0
                client._async_walk("1.3.6.1.2.1.4.35.1.4"),              # 1
                client._async_get_one(OID_BASE_MAC),                    # 2
                client._async_get_one("1.3.6.1.2.1.1.1.0"),              # 3
                client._async_walk("1.3.6.1.2.1.4.20.1.1"),              # 4
                client._async_walk("1.3.6.1.2.1.31.1.1.1.1"),            # 5
                client._async_walk("1.3.6.1.2.1.31.1.1.1.15"),           # 6
                client._async_walk("1.3.6.1.2.1.2.2.1.5"),              # 7
                client._async_walk(OID_LLDP_REM_SYS_NAME),              # 8
                client._async_walk(OID_Q_FDB_PORT),                      # 9
                client._async_walk(OID_FDB_PORT),                        # 10
                client._async_walk("1.3.6.1.2.1.17.1.4.1.2"),           # 11
                client._async_walk("1.3.6.1.2.1.2.2.1.6"),              # 12
                client._async_walk("1.3.6.1.2.1.2.2.1.8"),              # 13
                client._async_get_one("1.3.6.1.2.1.4.1.0"),             # 14
                client._async_walk("1.0.8802.1.1.2.1.4.1.1.12"),         # 15
                return_exceptions=True
            )
            def get_val(res):
                return res if isinstance(res, (list, tuple)) or (res is not None and not isinstance(res, Exception)) else None
                
            return {
                "arp_mac": get_val(results[0]),
                "arp_phys": get_val(results[1]),
                "base_mac": results[2] if not isinstance(results[2], Exception) else None,
                "sys_descr": results[3] if not isinstance(results[3], Exception) else None,
                "ip_addrs": get_val(results[4]),
                "if_names": get_val(results[5]),
                "if_highspeeds": get_val(results[6]),
                "if_speeds": get_val(results[7]),
                "lldp_names": get_val(results[8]),
                "q_fdb": get_val(results[9]),
                "fdb": get_val(results[10]),
                "baseports": get_val(results[11]),
                "if_macs": get_val(results[12]),
                "if_operstatus": get_val(results[13]),
                "ip_forwarding": results[14] if not isinstance(results[14], Exception) else None,
                "lldp_caps": get_val(results[15])
            }
        except Exception as e:
            _LOGGER.error("Fatal error during parallel SNMP query for %s: %s", name, e)
            return None
