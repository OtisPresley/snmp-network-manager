# SNMP Network Manager: Home Assistant Custom Integration

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-41BDF5?logo=home-assistant&logoColor=white&style=flat)](https://www.home-assistant.io/)
[![HACS Badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)
[![HA installs](https://img.shields.io/badge/dynamic/json?url=https://analytics.home-assistant.io/custom_integrations.json&query=$.snmp_network_manager.total&label=Installs&color=41BDF5)](https://analytics.home-assistant.io/custom_integrations.json)
[![License: MIT](https://img.shields.io/badge/License-MIT-e0b416.svg)](https://github.com/OtisPresley/snmp-network-manager/blob/main/LICENSE)
[![hassfest](https://img.shields.io/github/actions/workflow/status/OtisPresley/snmp-network-manager/hassfest.yaml?branch=main&label=hassfest)](https://github.com/OtisPresley/snmp-network-manager/actions/workflows/hassfest.yaml)
[![HACS](https://img.shields.io/github/actions/workflow/status/OtisPresley/snmp-network-manager/hacs.yaml?branch=main&label=HACS)](https://github.com/OtisPresley/snmp-network-manager/actions/workflows/hacs.yaml)
[![CI](https://img.shields.io/github/actions/workflow/status/OtisPresley/snmp-network-manager/ci.yaml?branch=main&event=push)](https://github.com/OtisPresley/snmp-network-manager/actions/workflows/ci.yaml)

SNMP Network Manager discovers, correlates, and visualizes your physical and wireless network topology in **Home Assistant**. By seamlessly integrating with **SNMP Switch Manager** and interrogating Bridge-MIB, Q-BRIDGE-MIB (FDB), ARP tables, and LLDP neighbors, it constructs a real-time, interactive hierarchical network map complete with port links, device status, and automatic Home Assistant registry binding.

---

## 📖 Table of Contents

- [✨ Core Highlights](#-core-highlights)
- [🛠️ Requirements](#️-requirements)
- [📦 Installation](#-installation)
  - [HACS (Recommended)](#hacs-recommended)
  - [Manual Install](#manual-install)
- [📘 Documentation](#-documentation)
- [🗺️ Interactive Map & Endpoint Correlation](#️-interactive-map--endpoint-correlation)
- [🏷️ WebSocket API & Services](#️-websocket-api--services)
- [🔧 Troubleshooting & Support](#-troubleshooting--support)
- [🛣️ Roadmap & Changelog](#️-roadmap--changelog)

---

## ✨ Core Highlights

- 🗺️ **Interactive Network Topology Map**: High-performance SVG visualizer with smooth pan, zoom, dynamic aspect-ratio resizing, node dragging, and detailed port-level link tooltips.
- 🔌 **Native SNMP Switch Manager Synergy**: Connects directly to existing `snmp_switch_manager` configurations—no duplicate SNMP credentials, community strings, or polling setups required.
- 🖧 **Automated L2/L3 Topology Discovery**: Interrogates Bridge-MIB and Q-BRIDGE-MIB Forwarding Databases (FDB), switch ARP tables, and LLDP remote system tables to map exact switch-to-switch and switch-to-client connections.
- 📶 **Universal Wireless Client Correlation**: Automatically maps wireless devices to their connected Access Points across UniFi, pfSense, Omada, ASUS, OpenWrt, or generic router integrations without manual configuration or external dependencies.
- 💤 **Smart Asleep vs. Down State Resolution**: Intelligently differentiates battery-powered and sleeping devices (asleep) from truly disconnected or unreachable network nodes (down) using native Home Assistant entity tracking.
- 🏷️ **End-to-End Friendly Naming**: Normalizes raw MAC addresses, reverse DNS hostnames, and registry entries into clean, human-friendly names (e.g., `jamie's-macbook-pro` to `Jamie's Macbook Pro`).
- 🔄 **Composite Device Merging & Entity Binding**: Correlates multi-interface or multi-integration endpoints into primary Home Assistant devices, exposing device controls and status in one unified card popup.
- ⏱️ **Historical Edge & State Persistence**: Preserves historical topology links across restarts and tracks per-node state change history for comprehensive auditability.
- 🚫 **Granular Exclusions & Purge Rules**: Config Flow options to exclude devices by MAC, IP, or Name regex, with automated cleanup of stale offline nodes.
- ⚡ **Zero-Overhead Asynchronous Architecture**: Optimized asynchronous table walks and event-driven dispatcher updates that never block the Home Assistant event loop.

---

<div align="center">
  <img width="900" alt="image" src="https://github.com/user-attachments/assets/8d53635c-f089-410b-9790-52131ae2b415" />
</div>

---

## 🛠️ Requirements

- **Home Assistant**: version 2025.11.2 or newer (recommended).
- **SNMP Switch Manager**: integration installed and configured with at least one managed switch.
- **Network Devices**: Switches and Access Points supporting standard MIBs (Bridge-MIB, Q-BRIDGE-MIB, or LLDP).

---

## 📦 Installation

### HACS (Recommended)

1. Open your Home Assistant instance and navigate to **HACS → Integrations**.
2. Click the three dots in the top-right corner, select **Custom Repositories**, and add:
   `https://github.com/OtisPresley/snmp-network-manager` (Category: **Integration**).
3. Search for **SNMP Network Manager** and click **Download**.
4. **Restart Home Assistant**.
5. Go to **Settings → Devices & Services → Add Integration** and search for **SNMP Network Manager**.

---

### Manual Install

1. Copy the contents of `custom_components/snmp_network_manager` into your Home Assistant installation folder under `/config/custom_components/snmp_network_manager`.
2. **Restart Home Assistant**.
3. Go to **Settings → Devices & Services → Add Integration** and select **SNMP Network Manager**.

---

## 📘 Documentation

Comprehensive, step-by-step guides are hosted in the **[GitHub Wiki](https://github.com/OtisPresley/snmp-switch-manager/wiki/Network-Manager-Home)**:

- 🏠 **[Network Manager Overview](https://github.com/OtisPresley/snmp-switch-manager/wiki/Network-Manager-Home)** – Architecture overview, SNMP Switch Manager synergy, and capabilities.
- 📦 **[Installation & Prerequisites](https://github.com/OtisPresley/snmp-switch-manager/wiki/Network-Manager-Installation)** – Setup requirements and HACS installation.
- ⚙️ **[Integration Configuration](https://github.com/OtisPresley/snmp-switch-manager/wiki/Network-Manager-Configuration)** – Walkthrough of exclusion options, retention periods, and polling intervals.
- 🗺️ **[Lovelace Map Card Configuration](https://github.com/OtisPresley/snmp-switch-manager/wiki/Network-Manager-Card)** – Card layout options, themes, pan/zoom controls, and node customization.
- 🔗 **[Device Correlation & Manual Linkage](https://github.com/OtisPresley/snmp-switch-manager/wiki/Network-Manager-Correlation)** – Understanding composite device grouping and manual MAC-to-device mapping.
- 🛠️ **[Troubleshooting & FAQ](https://github.com/OtisPresley/snmp-switch-manager/wiki/Network-Manager-Troubleshooting)** – Resolving missing edges, reverse DNS caching, and orphan node cleanup.

---

## 🗺️ Interactive Map & Endpoint Correlation

SNMP Network Manager pairs its topology discovery engine with a dedicated Lovelace card:

1. **Topology Hierarchy**: Gateways and core switches occupy top levels, access points form intermediate distribution layers, and wired/wireless endpoints branch out automatically.
2. **Interactive Node Control**: Clicking any node opens its Home Assistant more-info dialog, providing instant access to controls, sensors, and linked integrations.
3. **Manual Device Linkage**: Override automatic MAC matching directly from the card UI to associate unlinked MAC endpoints with existing Home Assistant device entries.
4. **Live Latency Testing**: Ping any node directly from the card UI to test reachability and display round-trip latency in real time.

---

## 🏷️ WebSocket API & Services

SNMP Network Manager exposes native WebSocket commands for real-time frontend interaction:

### WebSocket Command: `snmp_network_manager/get_topology`
Retrieves the latest complete network snapshot, enriched with device registry names, real-time states, and linked integrations.

### WebSocket Command: `snmp_network_manager/ping_device`
Executes an asynchronous ping from the Home Assistant host to an endpoint IP and returns round-trip latency.
```json
{
  "type": "snmp_network_manager/ping_device",
  "ip": "10.0.12.110"
}
```

### WebSocket Command: `snmp_network_manager/exclude_device`
Excludes a device by MAC, IP, or Name regex by dynamically updating the integration's Config Entry options.
```json
{
  "type": "snmp_network_manager/exclude_device",
  "exclude_type": "mac",
  "value": "AA:BB:CC:DD:EE:FF"
}
```

### WebSocket Command: `snmp_network_manager/link_device`
Manually associates or unlinks a MAC endpoint with a specific Home Assistant Device ID.
```json
{
  "type": "snmp_network_manager/link_device",
  "mac": "AA:BB:CC:DD:EE:FF",
  "device_id": "c479bf73a4b67946955bc6f5d8123456"
}
```

---

## 🔧 Troubleshooting & Support

### Common Issues Quick Guide

- **Switches or APs do not show up**: Ensure the switches are configured and operational in **SNMP Switch Manager**. SNMP Network Manager automatically consumes switch clients from SNMP Switch Manager.
- **Endpoints show as 'asleep' instead of 'up'**: The device's tracked Home Assistant entities (e.g., Apple TV, TV, laptop) report a standby, sleep, or docked state. This is intentional to prevent battery devices from being falsely marked as offline or down.
- **Endpoints show MAC addresses instead of names**: Ensure reverse DNS is configured on your local network/router or configure friendly names directly on the Home Assistant device registry entry.
- **Duplicate nodes or ghost endpoints**: Check the integration Options to adjust `stale_days` (default: 30 days) or exclude specific MACs or subnets.

### Reporting Issues

To report bugs, request features, or ask questions, please visit the **[GitHub Issues page](https://github.com/OtisPresley/snmp-network-manager/issues)**.

---

## 🛣️ Roadmap & Changelog

- Check the **[Changelog](https://github.com/OtisPresley/snmp-network-manager/blob/main/CHANGELOG.md)** for detailed release notes.

---

If you find this integration useful and want to support its ongoing development:

[![Buy Me a Coffee](https://img.shields.io/badge/Support-Buy%20Me%20a%20Coffee-orange)](https://www.buymeacoffee.com/OtisPresley)
[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-blue.svg)](https://paypal.me/OtisPresley)
