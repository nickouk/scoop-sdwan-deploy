# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## API Reference

**Cisco Catalyst WAN SDK**: https://github.com/cisco-open/cisco-catalyst-wan-sdk

This open-source Python SDK is the authoritative reference for constructing vManage REST API calls. When an API endpoint or request format is unclear, consult the SDK source — particularly:
- `catalystwan/api/speedtest_api.py` — speedtest flow (`/dataservice/stream/device/speed`)
- `catalystwan/api/basic_api.py` — device state methods (BFD colors, data stream enable)
- `catalystwan/api/` — other API wrappers organised by feature area

The SDK uses `catalystwan.session` for authenticated requests; the equivalent in our scripts is the `requests.Session` with JSESSIONID cookie + `X-XSRF-TOKEN` header.

---

## Deployment Pipeline

This repository is **Step 2** of a broader SD-WAN deployment pipeline for migrating sites to a Cisco IOS-XE SD-WAN solution.

| Step | Script | Purpose |
|------|--------|---------|
| 1 | *(ping monitor — embedded in step 2)* | Confirm sites are reachable and stable |
| 2 | `code-upgrade.py` | Upgrade IOS-XE software to the target SD-WAN version |
| 3 | *(future)* | Move devices to final config group once code version confirmed |
| 4 | *(future)* | Verification pass — confirm site is live |

Each step runs against the same IP list (sourced from the multiping file) and should produce a clear pass/fail status per site before the next step begins.

---

## Device & Site Naming

**Hostname format:** `SC-{SITE-TYPE}-{STORE-NUM}-{DEVICETYPE}{INDEX}`
**Example:** `SC-3-0007-R1`

| Field | Values | Meaning |
|-------|--------|---------|
| `SITE-TYPE` | `3` | Retail |
| | `4` | Welcome Franchise |
| | `5` | ELS Lite |
| | `6` | ELS |
| `STORE-NUM` | `0001`–`9999` | 4-digit zero-padded store number |
| `DEVICETYPE` | `R` | CEDGE (branch router) |
| `INDEX` | `1`, `2` | Device index at site (HA pairs) |

The `SITE-TYPE` + `STORE-NUM` concatenated (e.g. `30007`) is the **vManage `site-id`** for that site.

---

## vManage

**Base URL:** `https://vmanage-953677893.sdwan.cisco.com:8443/`
**Controller version:** `20.15.4.4`
**Auth:** username/password — prompted at script startup alongside device credentials

### Configuration Groups

Each CEDGE is initially assigned to the **onboarding** config group for the code upgrade, then moved to its **final** config group once the target code version is confirmed.

| UUID | Name | Used when |
|------|------|-----------|
| `8cd9fc8a-a552-41be-95f5-42fc4bcc6ad9` | `onboard_r1_pppoe_r2_pppoe` | All sites during code upgrade (step 2) |
| `9ec9950c-bbcb-4d09-8466-c33d2eb8d902` | `onboard_r1_eth_r2_eth` | Onboarding (Ethernet WAN variant) |
| `90edb92d-05e9-4887-886c-fea0ef535422` | `type34_r1_pppoe_r2_pppoe` | **Final** — SITE-TYPE 3 or 4 |
| `70be7b37-7b9b-4b79-bfbc-80dba0f4c994` | `type56_r1_pppoe_r2_pppoe` | **Final** — SITE-TYPE 5 or 6 |
| `8567e55b-26da-406b-9ede-694490f5c870` | `type34_r1_eth_r2_eth` | Final — type 3/4 Ethernet WAN variant |
| `cfb79bce-ec89-4790-900b-d9493ef3c3db` | `type56_1127X` | Final — type 5/6 on 1127X hardware |
| `77da43d2-1f81-4ab2-8aac-ce76512136fb` | `onboard_1127X` | Onboarding — 1127X hardware |
| `5607f32e-7391-4241-95a6-c2ac5b4d17fd` | `poc_nicko` | PoC / testing |
| `f5ea86ef-8f5c-4319-a518-6c169b9d7025` | `icon_hubs` | Hub sites |

**Config group deploy sequence (step 3) — confirmed working:**
1. `DELETE /v1/config-group/{onboard-uuid}/device/associate` — body: `{"devices": [{"id": uuid}]}`
2. `POST   /v1/config-group/{final-uuid}/device/associate`   — body: `{"devices": [{"id": uuid}]}` (no variables)
3. `PUT    /v1/config-group/{final-uuid}/device/variables`   — body: `{"solution": "sdwan", "devices": [{"device-id": uuid, "variables": [{name, value}...]}]}`
4. `POST   /v1/config-group/{final-uuid}/device/deploy`      — body: `{"devices": [{"id": uuid}]}`

**Variable format for step 3:**
- Variables are a `[{name, value}]` list — NOT a flat dict
- Variable names are snake_case (e.g. `system_ip`, `vlan10_ipv4`)
- 5 CSV column headers need remapping: `"System IP"→system_ip`, `"Host Name"→host_name`, `"Site Id"→site_id`, `"Dual Stack IPv6 Default"→ipv6_strict_control`, `"Rollback Timer (sec)"→pseudo_commit_timer`
- Value types must match the schema: int, float, bool, str, or list
- `*_dhcp_exclude` variables are `list[str]`; CSV encodes multiple ranges with `";"` delimiter (e.g. `"10.0.0.1-10.0.0.63";"10.0.0.124-10.0.0.126"`)
- Variable names and types are discovered at runtime by GETting `/device/variables` for an existing device in the same group

**Config group selection logic (final):**
```
SITE-TYPE in {3, 4}  →  type34_r1_pppoe_r2_pppoe
SITE-TYPE in {5, 6}  →  type56_r1_pppoe_r2_pppoe
```

### Per-site Variables

These must be substituted for each device when attaching a config group:

| Variable | Source |
|----------|--------|
| `system-ip` | `172.x.x.x` — assigned manually, read from `pingips.txt` |
| `site-id` | Integer value of `SITE-TYPE + STORE-NUM` (e.g. `30007`) |
| `hostname` | Derived from naming format above |
| `loopback0` | Same as `system-ip` |
| `timezone` | Derived from region (UK → `Europe/London`, EU → `Europe/Berlin`, etc.) |

### Hub Sites

BFD sessions to both hubs must be up as part of the verification pass criteria.

| Hostname | System IP |
|----------|-----------|
| `SC-1-0001-LD8-R1` | `172.31.116.0` |
| `SC-1-0001-THN-R1` | `172.31.116.1` |

---

## vManage Import CSV

**Path:** `/mnt/c/Users/nick.oneill/OneDrive - Maintel Europe Limited/Southern Coops - Rollout docs/vmanage-import-sc.csv`

This CSV contains all per-device template variables required to associate and deploy a config group in vManage. It is the authoritative source for device variables — do not hardcode these values in the script.

| Column | Meaning |
|--------|---------|
| `Device ID` | Chassis serial number (identifier only — excluded from template variables) |
| `System IP` | Management/system IP — used as the lookup key to match a dashboard device to its CSV row |
| `Host Name` | Device hostname |
| `Site Id` | Numeric site ID (e.g. `30007`) |
| *(remaining 111 columns)* | VLAN configs, WAN settings, PPPoE credentials, QoS, NAT, etc. |

The script loads this file at startup (`load_csv_vars()`) into a dict keyed by System IP, then passes **all columns except `Device ID`** as template variables when associating a device with its final config group.

---

## Verification Pass Criteria

A site is considered **live** only when **all** of the following are true:

- vManage sync status = `In Sync`
- BFD sessions up to both hub sites (`172.31.116.0` and `172.31.116.1`)
- OMP peers = 2
- Ping to `8.8.8.8` from device succeeds

---

## Overview

A single-file Python script that monitors Cisco IOS-XE SD-WAN routers via ping and automatically upgrades their software to `17.15.04c.0.107` when they become reachable. The ping monitor (Step 1) is embedded in this script — devices must sustain 30 seconds of continuous uptime before the upgrade (Step 2) is triggered.

## Running

```bash
# Activate the shared venv (lives in parent home directory)
source ~/my-venv/bin/activate

python code-upgrade.py
```

The script prompts for three credential sets at startup: **local** (device-local user), **ISE** (RADIUS/TACACS fallback), and **vManage** (for config group deployment).

It reads target IPs from the multiping file at `PING_FILE` (a Windows path via WSL: `/mnt/c/Users/nick.oneill/Tools/multiping/pingips.txt`).

## Key constants (top of file)

| Constant | Default | Meaning |
|---|---|---|
| `PING_FILE` | Windows path via WSL | Source of 172.x.x.x IPs to monitor |
| `TARGET_VERSION` | `17.15.04c.0.107` | Version to upgrade to |
| `OLD_VERSION` | `17.12.05a.0.159` | Version to remove after upgrade |
| `INSTALL_FILE` | `bootflash:c1100-universalk9.17.15.04c.SPA.bin` | Expected location of installer |
| `CSV_VARS_FILE` | OneDrive path via WSL | Per-device vManage template variables |
| `UP_THRESHOLD` | 30s | Continuous uptime before upgrade starts |
| `RETRY_DELAY` | 300s | Wait before retrying a failed device |

## Architecture

The script uses three concurrent execution paths:

1. **`ping_loop`** (main thread) — pings every IP every 5s, tracks uptime, spawns `upgrade_device` threads when a device has been up for `UP_THRESHOLD` seconds, and prints the status table.

2. **`upgrade_device`** (one thread per device) — full upgrade workflow: SSH connect → check version → install `.bin` → activate (triggers reboot) → wait for reboot → set-default → remove old version. Idempotent: checks which step already completed and resumes from the right point.

3. **`info_collector_loop`** (background thread) — every 60s collects CONFIG/POLICY/CIRCUIT/SWITCHPORTS for all non-upgrading devices in parallel (up to 10 concurrent SSH sessions via `_info_sem`).

## State management

All shared state is protected by `state_lock`. The key dictionaries:

- `completed` — IPs that finished (or were already at target version)
- `in_progress` / `in_progress_step` — IPs currently being upgraded
- `checking` — IPs where the version check is running
- `retry_queue` — IPs waiting to retry after a failure
- `device_info` — per-IP `{config, policy, circuit}` collected by the info thread

State is persisted to `upgrade_status.json` (atomic rename via temp file) after every status change, allowing the script to be restarted mid-run without re-upgrading completed devices.

## Network topology notes

- Sites have two routers (R1, R2). Whichever router has the live WAN circuit shows `CIRCUIT: CONNECTED`; the other shows `CIRCUIT: NOT CONNECTED`.
- Both are still reachable via SSH — they use **peer TLOC extension**, meaning either router can use the other's WAN circuit. `NOT CONNECTED` does not mean the device is offline.
- **G0/1/0** — switch **live** port (connects router to site's production switch once migration is complete)
- **G0/1/7** — switch **provisioning** port (used during rollout to connect router to the existing switch for the migration handover)
- G0/1/1–6 — temporary connections
- Either G0/1/0 or G0/1/7 going UP indicates the physical switch handover is underway and the site no longer needs a READY FOR SWITCH alert

## SSH behaviour

`ssh_connect` tries local credentials first, then ISE credentials. `run_command` auto-reconnects if the SSH transport has dropped (common during and after reboots). The upgrade workflow explicitly handles the connection drop caused by the activate reboot.

## Output

- **Console**: status table printed every ping cycle; key events logged with `console=True`
- **Log file**: timestamped file (`code_upgrade_YYYYMMDD_HHMMSS.log`) written in the script directory; every SSH command and its output is logged
