# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file Python script that monitors Cisco IOS-XE SD-WAN routers via ping and automatically upgrades their software to `17.15.04c.0.107` when they become reachable. Designed for batch upgrades across many store/funeral-home sites.

## Running

```bash
# Activate the shared venv (lives in parent home directory)
source ~/my-venv/bin/activate

python code-upgrade.py
```

The script prompts for two credential sets at startup: **local** (device-local user) and **ISE** (RADIUS/TACACS fallback). It reads target IPs from the multiping file at `PING_FILE` (a Windows path via WSL: `/mnt/c/Users/nick.oneill/Tools/multiping/pingips.txt`).

## Key constants (top of file)

| Constant | Default | Meaning |
|---|---|---|
| `PING_FILE` | Windows path via WSL | Source of 172.x.x.x IPs to monitor |
| `TARGET_VERSION` | `17.15.04c.0.107` | Version to upgrade to |
| `OLD_VERSION` | `17.12.05a.0.159` | Version to remove after upgrade |
| `INSTALL_FILE` | `bootflash:c1100-universalk9.17.15.04c.SPA.bin` | Expected location of installer |
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
- G0/1/0 is the switch trunk port, up when the site is live on SD-WAN or ready for migration. G0/1/7 is the provisioning port. G0/1/1–6 are temporary connections.

## SSH behaviour

`ssh_connect` tries local credentials first, then ISE credentials. `run_command` auto-reconnects if the SSH transport has dropped (common during and after reboots). The upgrade workflow explicitly handles the connection drop caused by the activate reboot.

## Output

- **Console**: status table printed every ping cycle; key events logged with `console=True`
- **Log file**: timestamped file (`code_upgrade_YYYYMMDD_HHMMSS.log`) written in the script directory; every SSH command and its output is logged
