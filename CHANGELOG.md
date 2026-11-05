# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4] - 2026-11-05

### Fixed
- 🔤 **Comprehensive Attribute XSS Escaping**: 
  - Escaped DOM element `id` attributes (`hoverId`, `edgeId`, `nodeKey`) in `snmp-network-manager-card.js` for SVG map node and edge elements to prevent potential attribute breakout and script execution from arbitrary remote LLDP system names (`device:<sysName>`).
  - Escaped `icon` attribute bindings in `<ha-icon>` elements for custom icons, device icons, child icons, and switch icons.
  - Escaped search query `value` attributes in list and grouped search bars.

---

## [0.1.3] - 2026-11-04

### Fixed
- 🛡️ **Shared Device Registry Protection**: 
  - Restricted device registry removals and detachment in `topology.py` (`async_sync_device_registry` and `enrich_topology`) to devices solely owned by this integration. Shared devices belonging to other integrations (UniFi, router, mobile app, etc.) are now safely detached using `remove_config_entry_id` without deleting the device or wiping external entities.
  - Restricted manual link and unlink operations in `__init__.py` (`websocket_link_device`) to only move entities created by this integration (`platform == DOMAIN`), leaving other integrations' entities, names, identifiers, and connections intact.
  - Prevented overwriting device registry `name` attributes for shared devices in `device_tracker.py` during entity setup and state updates.
- 🎯 **Scoped Entity Cleanup**: Filtered legacy topology sensor registry cleanups in `sensor.py` strictly by `platform == DOMAIN` and the current `config_entry_id`.
- 🔒 **WebSocket Security & Command Validation**:
  - Enforced `@websocket_api.require_admin` on `snmp_network_manager/exclude_device`, `snmp_network_manager/link_device`, and `snmp_network_manager/ping_device`.
  - Added strict IP validation via `ipaddress.ip_address()` in both `websocket_ping_device` and `async_ping` to prevent injection of malicious arguments or options into the ping command.
- 🔤 **Frontend XSS Prevention & HTML Escaping**: Added HTML escaping across `snmp-network-manager-card.js` for all network-discovered and user-supplied strings, including node names, hostnames, IP addresses, MAC addresses, port names, switch names, integration titles, and SVG map/list/grouped view tooltips and labels.

---

## [0.1.2] - 2026-10-24

### Fixed
- 🗺️ **Map View State Synchronization**: Eliminated conflicting frontend `device_tracker` state overrides in `snmp-network-manager-card.js`. Map nodes now directly render using authoritative backend-correlated `node.attrs.state`, resolving discrepancies between Map view, Grouped view, List view, and Device Details modal.
- ⚡ **Non-blocking Event Loop Executor (`topology.py`)**: Wrapped `read_local_arp()` in `hass.async_add_executor_job` to prevent event loop blocking file I/O warnings when reading `/proc/net/arp`.
- 🛠️ **Device Registry Deprecation**: Replaced `device_registry.devices` mapping check with direct iteration in `get_composite_devices()`, eliminating the Home Assistant 2027.9.0 deprecation warning.
- 📋 **HACS Submission Compliance**: Updated `manifest.json` documentation URL to point to `https://github.com/OtisPresley/snmp-network-manager`.

### Added
- 🔄 **Debounced Real-Time Topology Refetching**: Added debounced topology updates in `snmp-network-manager-card.js` on linked entity state changes and client roaming events.

---

## [0.1.1] - 2026-10-24

### Fixed
- 📱 **Mobile Roaming Device Away Detection**:
  - Differentiated GPS geofence (`source_type: "gps"`) from actual local network connectivity.
  - Integrated mobile companion app Wi-Fi (`sensor.<device>_wi_fi_connection`), SSID, and cellular network sensors.
  - Aggregated composite roaming devices (e.g. UniFi client trackers + mobile app entities) so cellular or disconnected phones show `away` even when located at home.
- 🎨 **Layout Button Controls**: Fixed Delete Layout button visibility and enhanced battery device state resolution.

---

## [0.1.0] - 2026-10-20

### Added
- 🗺️ **Interactive Network Topology Map**: Dynamic SVG map card featuring pan, zoom, auto-layout, node dragging, and port-level link details.
- 🔌 **Native SNMP Switch Manager Synergy**: Directly interfaces with configured `snmp_switch_manager` switch clients, walking Bridge-MIB (`1.3.6.1.2.1.17.4.3.1.2`), Q-BRIDGE-MIB (`1.3.6.1.2.1.17.7.1.2.2.1.2`), and LLDP (`1.0.8802.1.1.2.1.4.1.1.9`) to map physical port connections.
- 📶 **Universal Wireless Client Mapping**: Correlates wireless endpoints to connected Access Points using live entity state attributes across UniFi, pfSense, Omada, ASUS, OpenWrt, or generic router integrations without manual configuration or external dependencies.
- 💤 **Smart Asleep vs. Down State Resolution**: Distinguishes active, sleeping/battery-standby (`asleep`), and truly unreachable (`down`) network nodes via native Home Assistant entity tracking.
- 🏷️ **End-to-End Friendly Naming**: Normalizes MAC addresses, reverse DNS lookups, and registry entries into clean, human-friendly names (e.g., `jamie's-macbook-pro` -> `Jamie's Macbook Pro`).
- 🔄 **Composite Device Merging & Entity Binding**: Correlates multi-interface or multi-integration endpoints into primary Home Assistant devices, exposing controls and status in one place.
- ⏱️ **Historical Edge & State Persistence**: Preserves historical topology links across restarts and tracks per-node state change history.
- 🚫 **Granular Exclusions & Purge Rules**: Config Flow options to exclude devices by MAC, IP, or Name regex, with automated cleanup of stale offline nodes.
- ⚡ **Zero-Overhead Asynchronous Architecture**: Optimized asynchronous table walks and event-driven dispatcher updates that never block the Home Assistant event loop.
