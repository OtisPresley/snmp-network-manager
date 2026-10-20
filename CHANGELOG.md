# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
