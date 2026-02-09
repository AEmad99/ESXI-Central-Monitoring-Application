# Database Schema Design

This document outlines the proposed database schema for transitioning the network monitoring application to a persistent, history-aware architecture.

## Overview
We will use **SQLAlchemy** (ORM) with **SQLite** as the backend. This allows for cleaner Python code, migration support, and easy query building for the time-travel features.

## Tables

### 1. `network_devices` (Hosts Table)
Stores unique machines identified on the network. This table establishes "ownership" of an IP address.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PK, Auto-increment | Unique internal ID |
| `mac_address` | String | Unique, Nullable | Hardware address (primary identifier if available) |
| `hostname` | String | Nullable | DNS name or VM name |
| `type` | String | Default 'Unknown' | 'VM', 'Physical', 'Unknown' |
| `first_seen` | DateTime | Default Now | When this device was first discovered |
| `last_seen` | DateTime | Default Now | Last time this device was confirmed online |

### 2. `ip_leases` (IP Management)
Maps IP addresses to their current state and owner. This table represents the **current** state of the network.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `ip_address` | String | PK | The IPv4 address (e.g., '192.168.1.50') |
| `subnet` | String | Index | The subnet prefix (e.g., '192.168.1') |
| `status` | Enum | 'Free', 'Active', 'Reserved', 'Down' | Current status of the IP |
| `device_id` | Integer | FK -> `network_devices.id`, Nullable | The device currently holding or reserving this IP |
| `last_updated` | DateTime | Default Now | When this record was last modified |

### 3. `network_history` (History Log)
Time-series log of all network state changes. Essential for the "Time Travel" and "Disaster Recovery" features.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PK, Auto-increment | Log entry ID |
| `timestamp` | DateTime | Index, Default Now | Exact time of the scan/event |
| `ip_address` | String | Index | The IP affected |
| `status` | Enum | 'Free', 'Active', 'Reserved', 'Down' | The status at that time |
| `device_id` | Integer | FK -> `network_devices.id`, Nullable | The device associated at that time |
| `hostname_snapshot` | String | Nullable | Hostname at the time of log (denormalized for speed) |

### 4. `esxi_hosts` (Configuration)
Stores configuration for ESXi hypervisors. (Renamed from old `hosts` table to avoid ambiguity).

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PK, Auto-increment | Internal ID |
| `ip` | String | Unique, Not Null | ESXi Management IP |
| `username` | String | Not Null | User for API connection |
| `password` | String | Not Null | Password for API connection |
| `group_name` | String | Nullable | Grouping label (e.g., 'Production') |

### 5. `virtual_machines` (VM Inventory)
Stores detailed inventory from ESXi scans. Linked to `esxi_hosts`.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PK, Auto-increment | Internal ID |
| `esxi_host_id` | Integer | FK -> `esxi_hosts.id` | The physical host running this VM |
| `name` | String | Not Null | VM Name in vCenter/ESXi |
| `os` | String | Nullable | Detected Operating System |
| `ip_addresses` | String | Nullable | Comma-separated list of detected IPs |
| `power_state` | String | Nullable | 'poweredOn', 'poweredOff', etc. |
| `cpu_count` | Integer | Default 0 | vCPU count |
| `ram_info` | String | Nullable | Memory details |
| `disk_info` | String | Nullable | Storage details |
| `created_date` | String | Nullable | VM creation timestamp from ESXi |
| `last_updated` | DateTime | Default Now | Last scan time |

### 6. `subnets` (Configuration)
List of subnets to scan.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `prefix` | String | PK | Subnet prefix (e.g., '192.168.1') |

## Logic Updates

### Smart Rescan (Upsert Strategy)
When scanning an IP (e.g., `192.168.1.50`):
1. **If responding (PING OK):**
   - Check if `ip_leases` has an entry.
   - If not, create new `network_device` (if hostname resolves) or link to existing.
   - Set status to **'Active'**.
   - Log entry to `network_history`.
2. **If NOT responding (PING FAIL):**
   - Check `ip_leases`.
   - If status was **'Active'** or **'Reserved'**:
     - Do NOT set to 'Free' immediately.
     - Set status to **'Reserved'** (or 'Down').
     - Log entry to `network_history` as 'Down'.
   - If status was **'Free'**:
     - Keep as **'Free'**.
