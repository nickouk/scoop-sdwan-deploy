#!/usr/bin/env python3
"""
SD-WAN Code Upgrade Script
Monitors routers via ping and upgrades to 17.15.04c.0.107 when reachable.
"""

import re
import os
import csv
import json
import time
import threading
import getpass
import subprocess
import sys
from datetime import datetime

import socket
import paramiko
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Constants ──────────────────────────────────────────────────────────────────
PING_FILE       = "/mnt/c/Users/nick.oneill/Tools/multiping/pingips.txt"
TARGET_VERSION  = "17.15.04c.0.107"
OLD_VERSION     = "17.12.05a.0.159"
INSTALL_FILE    = "bootflash:c1100-universalk9.17.15.04c.SPA.bin"
EXPECTED_FILE_SIZE = 734355992    # bytes; c1100-universalk9.17.15.04c.SPA.bin
UP_THRESHOLD    = 30          # seconds device must be Up before upgrade starts
PING_INTERVAL   = 5           # seconds between pings
RETRY_DELAY     = 300         # seconds before retrying a failed device (5 min)
# Labels that mean the device is on the target version and ready for the config/speedtest pipeline
PIPELINE_READY  = {"UPGRADE COMPLETE", "ALREADY UP TO DATE"}
SSH_TIMEOUT     = 30          # seconds for SSH connect/read
CMD_TIMEOUT     = 600         # seconds to wait for long-running commands
INSTALL_TIMEOUT = 1500        # seconds to wait for the install command (verify alone can take 16+ min)
REBOOT_TIMEOUT  = 600         # seconds to wait for device to come back after reboot
REBOOT_POLL     = 10          # seconds between reboot connectivity checks

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(_SCRIPT_DIR, "upgrade_status.json")

VMANAGE_BASE_URL      = "https://vmanage-953677893.sdwan.cisco.com:8443"
SPEEDTEST_DST_IP      = "172.31.116.1"    # SC-1-0001-THN-R1
SPEEDTEST_DST_COLOR   = "public-internet"
SPEEDTEST_SRC_COLOR   = {"1": "blue", "2": "green"}   # keyed by device index (R1/R2)
VMANAGE_ONBOARD_UUID = "8cd9fc8a-a552-41be-95f5-42fc4bcc6ad9"
VMANAGE_POLICY_GROUP_UUID = "ade1666a-8d3c-4ba3-a641-b38a129eeda3"  # remote_sites_policy_group
VMANAGE_FINAL_GROUPS = {
    "3": "90edb92d-05e9-4887-886c-fea0ef535422",
    "4": "90edb92d-05e9-4887-886c-fea0ef535422",
    "5": "70be7b37-7b9b-4b79-bfbc-80dba0f4c994",
    "6": "70be7b37-7b9b-4b79-bfbc-80dba0f4c994",
}
CSV_VARS_FILE = (
    "/mnt/c/Users/nick.oneill/OneDrive - Maintel Europe Limited/"
    "Southern Coops - Rollout docs/vmanage-import-sc.csv"
)
# Human-readable CSV column headers that map to different vManage variable names
VMANAGE_COL_MAP = {
    "System IP":               "system_ip",
    "Host Name":               "host_name",
    "Site Id":                 "site_id",
    "Dual Stack IPv6 Default": "ipv6_strict_control",
    "Rollback Timer (sec)":    "pseudo_commit_timer",
}

WEBEX_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "webex.json")


# ── Webex (populated at startup from webex.json) ──────────────────────────────
webex_bot_token = ""
webex_room_id   = ""


def webex_notify(message: str) -> None:
    """Send a Markdown message to the configured Webex room. Silently no-ops if unconfigured."""
    if not webex_bot_token or not webex_room_id:
        return
    try:
        resp = requests.post(
            "https://webexapis.com/v1/messages",
            headers={"Authorization": f"Bearer {webex_bot_token}"},
            json={"roomId": webex_room_id, "markdown": message},
            timeout=10,
            verify=True,
        )
        if not resp.ok:
            log("Webex", f"notification failed HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        log("Webex", f"notification failed: {exc}")


# ── Credentials (populated at startup) ────────────────────────────────────────
local_user      = ""
local_pass      = ""
ise_user        = ""
ise_pass        = ""
vmanage_user    = ""
vmanage_pass    = ""
vmanage_session: requests.Session | None = None


# ── Shared state ──────────────────────────────────────────────────────────────
ping_results: dict[str, str]    = {}          # ip -> "Up" | "Down"
up_since:     dict[str, float]  = {}          # ip -> epoch when first Up
completed:      dict[str, str]   = {}          # ip -> final status label
checking:       set[str]         = set()       # IPs currently checking version
checking_since: dict[str, float] = {}          # ip -> epoch when checking started
in_progress:      set[str]       = set()       # IPs actively being upgraded
in_progress_since: dict[str, float] = {}       # ip -> epoch when upgrade started
in_progress_step:  dict[str, str]  = {}        # ip -> current upgrade step label
retry_queue:  dict[str, float]  = {}          # ip -> epoch when retry is due
hostnames:    dict[str, str]    = {}          # ip -> discovered hostname
device_info:  dict[str, dict]  = {}          # ip -> {config, policy, circuit}
ping_miss_count:      dict[str, int]  = {}   # ip -> consecutive missed pings
ping_hit_count:       dict[str, int]  = {}   # ip -> consecutive successful pings
circuit_ping_offline: set[str]        = set() # completed IPs offline per ping tracking
bin_missing:          set[str]        = set() # IPs that failed because install .bin not found
file_copying:         set[str]        = set() # IPs where .bin exists but copy is still in progress
wan_ip_cache:         dict[str, str]  = {}   # ip -> last known WAN/Dialer1 IP
vmanage_status:       dict[str, str]  = {}   # ip -> config group move status
policy_status:        dict[str, str]  = {}   # ip -> policy group deploy status
speedtest_status:     dict[str, str]  = {}   # ip -> speedtest result or status label
ready_for_switch:     dict[str, str]  = {}    # site-key -> hostname of first device that passed speedtest
_speedtest_sem        = threading.Semaphore(1)  # only one speedtest at a time
_speedtest_running:   set[str]        = set()  # IPs with an active speedtest thread
_speedtest_session:   dict[str, str]  = {}     # ip -> last known vManage session_id
_speedtest_retry_after: dict[str, float] = {}  # ip -> earliest epoch to retry after TBST1008
_policy_retry_after:  dict[str, float] = {}    # ip -> earliest epoch to retry a failed policy deploy
_site_complete_notified: set[str]     = set()  # site-keys where completion alert was sent
_bin_missing_notify_after: dict[str, float] = {}  # ip -> earliest epoch for next bin-missing Webex alert
_last_coffee_break: float = 0.0
csv_vars:             dict[str, dict]  = {}   # system-ip -> full variable row from CSV
circuit_type:         dict[str, str]   = {}   # management ip -> "new" | "migrated"
vmanage_tasks:        list[dict]      = []   # active tasks polled from vManage API
vmanage_tasks_lock    = threading.Lock()
state_lock    = threading.Lock()
print_lock    = threading.Lock()
_info_sem     = threading.Semaphore(10)  # max concurrent info-collection SSH sessions
_log_file     = None   # opened in main()


# ── Status file ───────────────────────────────────────────────────────────────────

def save_status() -> None:
    """
    Write current device state to STATUS_FILE as JSON.
    Uses a temp-file + atomic rename so the file is never half-written.
    """
    now_str = datetime.now().isoformat(timespec='seconds')
    with state_lock:
        all_ips = (
            set(ping_results.keys())
            | set(completed.keys())
            | set(retry_queue.keys())
            | in_progress
            | checking
        )
        devices = {}
        for ip in sorted(all_ips):
            if ip in completed:
                devices[ip] = {"status": "complete", "label": completed[ip]}
            elif ip in retry_queue:
                devices[ip] = {"status": "retry", "retry_until": retry_queue[ip]}
            elif ip in in_progress:
                devices[ip] = {"status": "upgrading"}
            elif ip in checking:
                devices[ip] = {"status": "checking"}
            else:
                devices[ip] = {"status": "pending"}
            devices[ip]["last_updated"] = now_str
            if ip in hostnames:
                devices[ip]["hostname"] = hostnames[ip]
            if ip in device_info:
                devices[ip]["device_info"] = device_info[ip]
            if ip in vmanage_status:
                devices[ip]["vmanage_status"] = vmanage_status[ip]
            if ip in policy_status:
                devices[ip]["policy_status"] = policy_status[ip]
            if ip in speedtest_status:
                devices[ip]["speedtest_status"] = speedtest_status[ip]
            if ip in _speedtest_session:
                devices[ip]["speedtest_session"] = _speedtest_session[ip]

    data = {"last_updated": now_str, "devices": devices}
    tmp = STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, STATUS_FILE)
    except Exception as exc:
        print(f"WARNING: Could not write status file: {exc}")


def load_status(ips: list[str]) -> None:
    """
    Load STATUS_FILE and restore completed / retry state.
    Interrupted upgrades (upgrading/checking) are left as pending so the
    version-check logic can determine the correct resume point.
    """
    if not os.path.exists(STATUS_FILE):
        return

    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"WARNING: Could not read status file ({STATUS_FILE}): {exc}")
        return

    devices = data.get("devices", {})
    now     = time.time()
    msgs    = []

    for ip in ips:
        entry = devices.get(ip)
        if not entry:
            continue
        status = entry.get("status")

        if status == "complete":
            label = entry.get("label", "COMPLETE")
            if label == "CODE UPGRADE COMPLETE":
                label = "UPGRADE COMPLETE"
            completed[ip] = label
            hostname = entry.get("hostname", "")
            if hostname:
                hostnames[ip] = hostname
            saved_info = entry.get("device_info")
            if saved_info:
                device_info[ip] = saved_info
            vm_st = entry.get("vmanage_status")
            if vm_st:
                # Reset transient mid-deploy states — _collect_one will recover
                if vm_st in ("ASSOCIATING", "SETTING VARS", "DEPLOYING", "WAITING") or vm_st.startswith("WAITING ("):
                    vm_st = None
            if vm_st:
                vmanage_status[ip] = vm_st
            pol_st = entry.get("policy_status")
            if pol_st:
                # Reset mid-deploy states so they retry on restart
                if pol_st in ("ASSOCIATING", "SETTING VARS", "DEPLOYING", "WAITING"):
                    pol_st = None
                if pol_st:
                    policy_status[ip] = pol_st
            sp_st = entry.get("speedtest_status")
            if sp_st:
                speedtest_status[ip] = sp_st
            sp_sess = entry.get("speedtest_session")
            if sp_sess and not str(sp_st).startswith('↓'):
                # Only restore the session if the speedtest didn't complete — a completed
                # session has already been disabled; we don't want to re-disable it.
                _speedtest_session[ip] = sp_sess
            msgs.append(f"  {ip:<20}  {hostname:<28}  {label} (skipping)")

        elif status == "retry":
            retry_until = entry.get("retry_until", 0.0)
            if retry_until > now:
                retry_queue[ip] = retry_until
                remaining = int(retry_until - now)
                msgs.append(f"  {ip:<20}  {'':30}  RETRY in {fmt_elapsed(remaining)} (restored)")
            else:
                msgs.append(f"  {ip:<20}  {'':30}  retry expired, re-queuing as pending")

        elif status in ("upgrading", "checking", "interrupted"):
            msgs.append(f"  {ip:<20}  was {status} — will re-check version and resume")

    if msgs:
        print(f"Loaded status from {STATUS_FILE}:")
        for m in msgs:
            print(m)
        print()


def load_csv_vars() -> None:
    """Load per-device variables from the vManage import CSV, keyed by System IP."""
    if not os.path.exists(CSV_VARS_FILE):
        print(f"WARNING: vManage CSV not found: {CSV_VARS_FILE}")
        return
    try:
        with open(CSV_VARS_FILE, newline='', encoding='utf-8-sig') as fh:
            for row in csv.DictReader(fh):
                system_ip = row.get("System IP", "").strip()
                if system_ip:
                    # Exclude Device ID — it's a chassis identifier, not a template variable
                    csv_vars[system_ip] = {k: v for k, v in row.items() if k != "Device ID"}
        print(f"Loaded variables for {len(csv_vars)} device(s) from CSV")
    except Exception as exc:
        print(f"WARNING: Could not read vManage CSV: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def parse_sdwan_versions(output: str) -> dict:
    """
    Parse 'show sdwan software' table output.
    Returns: { version_str: {'active': bool, 'default': bool}, ... }
    Data rows have 'true' or 'false' in column positions 1 and 2.
    """
    versions = {}
    for line in output.splitlines():
        parts = line.split()
        if (
            len(parts) >= 3
            and parts[1].lower() in ('true', 'false')
            and parts[2].lower() in ('true', 'false')
        ):
            versions[parts[0]] = {
                'active':  parts[1].lower() == 'true',
                'default': parts[2].lower() == 'true',
            }
    return versions


def parse_dir_file_size(output: str, filename: str) -> int | None:
    """
    Return the file size (bytes) for *filename* from IOS-XE 'dir' output, or None.
    Matches lines containing the basename and extracts the size field
    (the integer that precedes the 3-letter month abbreviation).
    """
    basename = filename.split(":")[-1]
    for line in output.splitlines():
        if basename not in line:
            continue
        m = re.search(r"\s(\d+)\s+\w{3}\s+\d{1,2}\s+\d{4}", line)
        if m:
            return int(m.group(1))
    return None


def log(ip: str, msg: str, console: bool = False) -> None:
    line = f"[{ts()}] [{ip}] {msg}"
    with print_lock:
        if console:
            print(line)
        if _log_file:
            _log_file.write(line + "\n")
            _log_file.flush()


def load_webex_config() -> None:
    global webex_bot_token, webex_room_id
    try:
        with open(WEBEX_CONFIG_FILE) as f:
            cfg = json.load(f)
        webex_bot_token = cfg.get("bot_token", "").strip()
        webex_room_id   = cfg.get("room_id", "").strip()
        if webex_bot_token and webex_room_id:
            print(f"Webex notifications enabled (room {webex_room_id[:8]}…)")
        else:
            print("Webex: config found but bot_token/room_id missing — notifications disabled")
    except FileNotFoundError:
        print(f"Webex: {WEBEX_CONFIG_FILE} not found — notifications disabled")
    except Exception as exc:
        print(f"Webex: failed to load config — {exc} — notifications disabled")


def prompt_credentials() -> None:
    global local_user, local_pass, ise_user, ise_pass, vmanage_user, vmanage_pass
    print("=== SD-WAN Code Upgrade Tool ===\n")
    local_user = input("Local username: ").strip()
    local_pass = getpass.getpass("Local password: ")
    print()
    ise_user   = input("ISE username: ").strip()
    ise_pass   = getpass.getpass("ISE password: ")
    print()
    vmanage_user = input("vManage username: ").strip()
    vmanage_pass = getpass.getpass("vManage password: ")
    print()


def extract_172_ips(filepath: str) -> tuple[list[str], list[str]]:
    """
    Read the file and return:
      ips          - unique IPv4 addresses with first octet 172 (for processing)
      display_order - ordered list mixing IPs and comment strings (for display)
    Comment lines are any line whose first non-whitespace character is '#'.
    """
    pattern = re.compile(r"\b(172\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
    seen_set: set[str] = set()
    ips: list[str] = []
    display_order: list[str] = []   # entries are IPs or "#comment" strings
    try:
        with open(filepath, "r", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    display_order.append(stripped)
                    continue
                for match in pattern.finditer(line):
                    ip = match.group(1)
                    if ip not in seen_set:
                        seen_set.add(ip)
                        ips.append(ip)
                        display_order.append(ip)
    except FileNotFoundError:
        print(f"ERROR: Cannot open {filepath}")
        sys.exit(1)
    return ips, display_order


def parse_circuit_types(filepath: str) -> dict[str, str]:
    """
    Parse pingips.txt and return a dict mapping each management IP (172.x) to
    'new' or 'migrated'.

    A non-172 IP on a line immediately before a 172.x IP is that router's WAN
    IP — meaning the router is using a pre-existing (migrated) broadband circuit.
    A 172.x IP with no preceding WAN IP is on a new circuit.

    Comment (#) or blank lines reset the WAN-IP association.
    """
    result: dict[str, str] = {}
    pending_wan = False
    ip_re = re.compile(r"^\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*$")
    try:
        with open(filepath, "r", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    pending_wan = False
                    continue
                m = ip_re.match(stripped)
                if not m:
                    continue
                addr = m.group(1)
                if addr.startswith("172."):
                    result[addr] = "migrated" if pending_wan else "new"
                    pending_wan = False
                else:
                    pending_wan = True
    except FileNotFoundError:
        pass
    return result


def ping_once(ip: str) -> bool:
    """Return True if host replies to a single ping."""
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "2", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


# ── SSH helpers ───────────────────────────────────────────────────────────────

def ssh_connect(ip: str) -> paramiko.SSHClient:
    """
    Try local credentials first; fall back to ISE credentials.
    Returns an open SSHClient or raises an exception.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    for user, passwd, label in [
        (local_user, local_pass, "local"),
        (ise_user,   ise_pass,   "ISE"),
    ]:
        try:
            client.connect(
                ip,
                username=user,
                password=passwd,
                timeout=SSH_TIMEOUT,
                look_for_keys=False,
                allow_agent=False,
            )
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(10)  # send keepalive every 10 s
            log(ip, f"SSH connected using {label} credentials")
            return client
        except paramiko.AuthenticationException:
            log(ip, f"SSH auth failed with {label} credentials")
        except Exception as exc:
            log(ip, f"SSH connect error ({label}): {exc}")
            raise

    raise RuntimeError(f"All credential sets failed for {ip}")


def discover_hostname(client: paramiko.SSHClient, ip: str) -> paramiko.SSHClient:
    """
    Fetch the device hostname via 'show version | include uptime' using the
    safe run_command wrapper and store it in the hostnames dict.
    Never raises — hostname is optional.
    """
    try:
        client, hn_output = run_command(
            client, ip, "show version | include uptime", timeout=SSH_TIMEOUT
        )
        line = hn_output.strip().splitlines()[0] if hn_output.strip() else ""
        hostname = line.split(" uptime")[0].strip() if " uptime" in line else ""
        if hostname:
            with state_lock:
                hostnames[ip] = hostname
            log(ip, f"Hostname: {hostname}")
    except Exception:
        pass
    return client


def collect_device_info(client: paramiko.SSHClient, ip: str) -> paramiko.SSHClient:
    """
    Collect CONFIG / POLICY / CIRCUIT status via SSH.
    Never raises — all fields default to '?' if the command fails.
    Always re-collects so status stays current.
    """
    info = dict(device_info.get(ip, {}))
    info.setdefault('config',      '?')
    info.setdefault('policy',      '?')
    info.setdefault('circuit',     '?')
    info.setdefault('switchports', '?')

    # ── CONFIG: show sdwan system ─────────────────────────────────────────────
    try:
        client, out = run_command(client, ip, "show sdwan system", timeout=SSH_TIMEOUT)
        log(ip, f"show sdwan system output:\n{out.strip()}")
        config_status = '?'
        for line in out.splitlines():
            lower = line.lower()
            # Field may appear as 'configuration template', 'config-template', etc.
            if any(k in lower for k in ('configuration template', 'config-template', 'config template')):
                # Split on colon or 2+ whitespace, discard empty parts
                parts = [p.strip() for p in re.split(r':\s*|\s{2,}', line.strip()) if p.strip()]
                value = parts[-1].lower() if len(parts) >= 2 else ''
                if value.startswith('onboard'):
                    config_status = 'REQUIRED'
                elif value.startswith('type'):
                    config_status = 'COMPLETE'
                elif value:
                    config_status = value[:12]
                break
        info['config'] = config_status
        log(ip, f"CONFIG: {config_status}")
    except Exception as exc:
        log(ip, f"CONFIG collect error: {type(exc).__name__}: {exc}")

    # ── POLICY: show utd engine standard config ───────────────────────────────
    try:
        client, out = run_command(client, ip, "show utd engine standard config", timeout=SSH_TIMEOUT)
        log(ip, f"show utd engine standard config output:\n{out.strip()}")
        policy_status = '?'  # stays '?' if Unified Policy line not found
        for line in out.splitlines():
            if 'unified policy' in line.lower():
                parts = [p.strip() for p in re.split(r':\s*|\s{2,}', line.strip()) if p.strip()]
                value = parts[-1] if len(parts) >= 2 else ''
                policy_status = 'COMPLETE' if value.lower() == 'enabled' else 'REQUIRED'
                break
        info['policy'] = policy_status
        log(ip, f"POLICY: {policy_status}")
    except Exception as exc:
        log(ip, f"POLICY collect error: {type(exc).__name__}: {exc}")

    # ── CIRCUIT + SWITCHPORTS: show ip interface brief ────────────────────────
    try:
        client, out = run_command(client, ip, "show ip interface brief", timeout=SSH_TIMEOUT)
        log(ip, f"show ip interface brief output:\n{out.strip()}")
        circuit_status = 'NOT CONNECTED'
        ip_pattern = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
        iface_up: dict[str, bool] = {}
        for line in out.splitlines():
            parts = line.split()
            if not parts or len(parts) < 5:
                continue
            iface_norm = re.sub(r'^(?:gigabitethernet|gi|ge)', '', parts[0].lower())
            iface_up[iface_norm] = (parts[4].lower() == 'up')
            if parts[0].lower().startswith('dialer') and len(parts) >= 2 and ip_pattern.match(parts[1]):
                circuit_status = f"CONNECTED ({parts[1]})"
                wan_ip_cache[ip] = parts[1]
        info['circuit'] = circuit_status
        log(ip, f"CIRCUIT: {circuit_status}")

        sw_labels = ['G0/1/0 UP' if iface_up.get('0/1/0') else 'G0/1/0 DOWN']
        if iface_up.get('0/1/7'):
            sw_labels.append('PROVISIONING')
        if any(iface_up.get(f'0/1/{n}') for n in range(1, 7)):
            sw_labels.append('TEMP PORTS IN USE')
        info['switchports'] = ' | '.join(sw_labels)
        log(ip, f"SWITCHPORTS: {info['switchports']}")

        hostname = hostnames.get(ip, ip)
        site_key = re.sub(r'-R\d+$', '', hostname)
        if (iface_up.get('0/1/0') or iface_up.get('0/1/7')) and site_key in ready_for_switch:
            active = 'G0/1/0' if iface_up.get('0/1/0') else 'G0/1/7'
            with state_lock:
                ready_for_switch.pop(site_key, None)
            log(ip, f"{hostname}: {active} now UP — SWITCH underway, clearing READY message", console=True)
    except Exception as exc:
        log(ip, f"CIRCUIT collect error: {type(exc).__name__}: {exc}")

    with state_lock:
        device_info[ip] = info
    save_status()
    return client


def run_command(client: paramiko.SSHClient, ip: str, command: str,
                timeout: int = CMD_TIMEOUT,
                no_retry: bool = False) -> tuple[paramiko.SSHClient, str]:
    """
    Execute a command and return (client, output).
    If the SSH transport has dropped, reconnects automatically before running.
    If no_retry=True, SSH timeout exceptions are re-raised instead of retried
    (use for long-running commands where a blind retry would be wrong).
    """
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        log(ip, "SSH transport inactive — reconnecting before command")
        try:
            client.close()
        except Exception:
            pass
        client = ssh_connect(ip)

    log(ip, f"CMD: {command}")
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode(errors="replace")
        err    = stderr.read().decode(errors="replace")
    except (EOFError, socket.timeout, TimeoutError) as exc:
        if no_retry:
            raise
        log(ip, f"SSH channel {type(exc).__name__} — reconnecting and retrying")
        try:
            client.close()
        except Exception:
            pass
        client = ssh_connect(ip)
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode(errors="replace")
        err    = stderr.read().decode(errors="replace")
    if err.strip():
        log(ip, f"STDERR: {err.strip()}")
    return client, output


# ── vManage helpers ───────────────────────────────────────────────────────────

def vmanage_login() -> bool:
    """Authenticate with vManage. Stores the authenticated session globally."""
    global vmanage_session
    session = requests.Session()
    session.verify = False

    # ── Step 1: Basic connectivity check ──────────────────────────────────────
    print(f"  Connecting to vManage at {VMANAGE_BASE_URL} …")
    try:
        probe = session.get(VMANAGE_BASE_URL, timeout=15, allow_redirects=True)
        print(f"  vManage reachable — HTTP {probe.status_code} ({probe.url})")
    except Exception as exc:
        print(f"  vManage unreachable: {exc}")
        return False

    # ── Step 2: POST credentials (don't follow redirects — check for JSESSIONID) ──
    print(f"  Logging in as '{vmanage_user}' …")
    try:
        resp = session.post(
            f"{VMANAGE_BASE_URL}/j_security_check",
            data={"j_username": vmanage_user, "j_password": vmanage_pass},
            timeout=30,
            allow_redirects=False,
        )
        print(f"  Login POST → HTTP {resp.status_code}")
        if resp.status_code not in (200, 302) or "JSESSIONID" not in session.cookies:
            print(f"  Login failed — no session cookie returned")
            return False
    except Exception as exc:
        print(f"  Login POST error: {exc}")
        return False

    # ── Step 3: Fetch XSRF token ───────────────────────────────────────────────
    try:
        resp2 = session.get(f"{VMANAGE_BASE_URL}/dataservice/client/token", timeout=15)
        print(f"  XSRF token fetch → HTTP {resp2.status_code}")
        if resp2.status_code == 200 and resp2.text.strip():
            session.headers.update({"X-XSRF-TOKEN": resp2.text.strip()})
    except Exception as exc:
        print(f"  XSRF token fetch error (non-fatal): {exc}")

    vmanage_session = session
    print("  vManage login successful\n")
    log("vManage", "Login successful", console=True)
    return True


def _vmanage_get_device_uuid(ip: str) -> str | None:
    """Return the vManage device UUID for the given system-ip."""
    if not vmanage_session:
        return None
    try:
        resp = vmanage_session.get(
            f"{VMANAGE_BASE_URL}/dataservice/system/device/vedges",
            timeout=30,
        )
        resp.raise_for_status()
        for dev in resp.json().get("data", []):
            if dev.get("system-ip") == ip:
                return dev.get("uuid")
    except Exception as exc:
        log(ip, f"vManage get-device-uuid error: {exc}")
    return None


def _vmanage_get_bfd_colors(ip: str) -> list[str]:
    """Return the list of BFD TLOC colors active on this device, e.g. ['green'] or ['blue']."""
    if not vmanage_session:
        return []
    try:
        resp = vmanage_session.get(
            f"{VMANAGE_BASE_URL}/dataservice/device/bfd/state/device/tlocInterfaceMap",
            params={"deviceId": ip},
            timeout=15,
        )
        resp.raise_for_status()
        return list(resp.json().get("intfList", {}).keys())
    except Exception as exc:
        log(ip, f"vManage get-bfd-colors error: {exc}")
    return []


def _wait_for_vmanage_idle(ip: str, timeout: int = 600, poll: int = 15) -> bool:
    """
    Block until vManage has no running tasks, then return True.
    Returns False if tasks don't clear within timeout seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = vmanage_session.get(
                f"{VMANAGE_BASE_URL}/dataservice/device/action/status/tasks",
                timeout=15,
            )
            resp.raise_for_status()
            running = resp.json().get("runningTasks", [])
            # Update global task list so the dashboard stays current
            with vmanage_tasks_lock:
                vmanage_tasks[:] = running
            if not running:
                return True
            names = ", ".join(t.get("name", "task") for t in running)
            log(ip, f"vManage: waiting for {len(running)} running task(s): {names}", console=True)
            with state_lock:
                vmanage_status[ip] = f"WAITING ({len(running)} task(s))"
        except Exception as exc:
            log(ip, f"vManage: task-check error while waiting: {exc}")
        time.sleep(poll)
    return False


def _vmanage_rollback_to_onboard(ip: str, uuid: str | None) -> None:
    """Re-associate device with onboarding group if a mid-flight failure left it orphaned."""
    if not uuid or not vmanage_session:
        return
    try:
        resp = vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/v1/config-group/{VMANAGE_ONBOARD_UUID}/device/associate",
            json={"devices": [{"id": uuid}]},
            timeout=60,
        )
        if resp.ok:
            log(ip, "vManage: rolled back to onboarding config group", console=True)
        else:
            log(ip, f"vManage: rollback failed HTTP {resp.status_code} — {resp.text[:200]!r}", console=True)
    except Exception as exc:
        log(ip, f"vManage: rollback error: {exc}", console=True)


def _try_trigger_policy_for_site(trigger_ip: str) -> None:
    """
    Check if all conditions are met to deploy the policy group for this site:
      - Every router at the site has vmanage_status == "DEPLOYED" (CONFIG complete)
      - At least one router at the site has a completed speedtest result (starts with ↓)
    If conditions are met, atomically claims all site IPs and spawns ONE
    deploy_policy_group_for_site thread covering the whole site.
    """
    with state_lock:
        hostname = hostnames.get(trigger_ip, "")
        if not hostname:
            return
        site_key = re.sub(r'-R\d+$', '', hostname)

        # Build site_ips from csv_vars (all devices in CSV) so an offline R2
        # that hasn't SSH'd yet is still included in the gate check.
        site_ips = [
            ip for ip, row in csv_vars.items()
            if re.sub(r'-R\d+$', '', row.get('Host Name', '')) == site_key
            and ip in ping_results
        ]
        if not site_ips:
            # csv_vars not loaded or site not in CSV — fall back to known hostnames
            site_ips = [
                ip for ip, hn in hostnames.items()
                if re.sub(r'-R\d+$', '', hn) == site_key
            ]
        if not site_ips:
            return

        for ip in site_ips:
            if vmanage_status.get(ip) != "DEPLOYED":
                log(trigger_ip, f"Policy trigger: waiting — {ip} CONFIG not yet DEPLOYED ({vmanage_status.get(ip, 'N/A')})")
                return

        speedtest_done = any(
            str(speedtest_status.get(ip, '')).startswith('↓')
            for ip in site_ips
        )
        if not speedtest_done:
            log(trigger_ip, f"Policy trigger: waiting — no speedtest complete yet for site {site_key}")
            return

        # Don't fire while any speedtest is actively running — it would race with the deploy.
        # "WAIT CIRCUIT" is not an active test (the thread is just sleeping) so don't block on it.
        running = [ip for ip in site_ips if speedtest_status.get(ip) == "RUNNING"]
        if running:
            log(trigger_ip, f"Policy trigger: waiting — speedtest still running on {running}")
            return

        # Skip if a deploy is already in-flight for this site
        if any(
            policy_status.get(ip) in ("WAITING", "ASSOCIATING", "SETTING VARS", "DEPLOYING")
            for ip in site_ips
        ):
            return

        # Skip if every known device is already deployed (nothing new to add)
        if all(policy_status.get(ip) == "DEPLOYED" for ip in site_ips):
            return

        # Atomically claim all IPs for this site before releasing the lock
        for ip in site_ips:
            policy_status[ip] = "WAITING"

    log(trigger_ip, f"Triggering policy group deployment for site {site_key} ({len(site_ips)} device(s))", console=True)
    threading.Thread(target=deploy_policy_group_for_site, args=(site_ips,), daemon=True).start()


def deploy_policy_group_for_site(site_ips: list[str]) -> None:
    """
    Associate and deploy the policy group for all devices at a site in a single
    vManage task, avoiding transaction conflicts from concurrent per-device tasks.
    """
    log_ip   = site_ips[0]
    site_key = re.sub(r'-R\d+$', '', hostnames.get(log_ip, log_ip))

    def set_all(status: str) -> None:
        with state_lock:
            for ip in site_ips:
                policy_status[ip] = status

    if not vmanage_session:
        log(log_ip, "vManage: no session — skipping policy group deploy", console=True)
        set_all("SKIPPED")
        save_status()
        return

    if not _wait_for_vmanage_idle(log_ip):
        log(log_ip, "vManage: timed out waiting for tasks to clear before policy deploy", console=True)
        set_all("FAILED")
        save_status()
        return

    try:
        # Resolve UUID and variable list for every device in the site
        devices_info: list[tuple[str, str, list]] = []
        for ip in site_ips:
            uuid = _vmanage_get_device_uuid(ip)
            if not uuid:
                raise ValueError(f"device UUID not found for system-ip {ip}")
            csv_row = csv_vars.get(ip)
            if not csv_row:
                raise ValueError(f"no CSV variables found for system-ip {ip}")
            var_list = _vmanage_build_variable_list(ip, VMANAGE_POLICY_GROUP_UUID, uuid, csv_row,
                                                    group_type="policy-group")
            devices_info.append((ip, uuid, var_list))

        # ── Step 1: Associate all devices in one call ─────────────────────────
        set_all("ASSOCIATING")
        log(log_ip, f"vManage: associating {len(devices_info)} device(s) with policy group", console=True)
        resp = vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/v1/policy-group/{VMANAGE_POLICY_GROUP_UUID}/device/associate",
            json={"devices": [{"id": uuid} for _, uuid, _ in devices_info]},
            timeout=60,
        )
        if not resp.ok:
            try:
                err_code = resp.json().get("error", {}).get("code", "")
            except Exception:
                err_code = ""
            if resp.status_code == 400 and err_code == "PLGRP0018":
                # Device(s) already associated with this policy group — fine, proceed to deploy
                log(log_ip, f"vManage: policy associate — device(s) already in group (PLGRP0018), proceeding to deploy", console=True)
            else:
                log(log_ip, f"vManage: policy associate HTTP {resp.status_code} — {resp.text[:500]!r}", console=True)
                resp.raise_for_status()
        else:
            log(log_ip, f"vManage: policy associate successful (HTTP {resp.status_code})", console=True)

        # ── Step 2: Wait after associate ──────────────────────────────────────
        set_all("WAITING")
        if not _wait_for_vmanage_idle(log_ip):
            raise ValueError("timed out waiting after policy associate")

        # ── Step 3: Set variables for all devices in one call ─────────────────
        set_all("SETTING VARS")
        for _ip, _uuid, _vl in devices_info:
            log(log_ip, f"vManage: policy variables for {_ip}: { {v['name']: v['value'] for v in _vl} }")
        log(log_ip, "vManage: setting policy group variables for all devices", console=True)
        resp = vmanage_session.put(
            f"{VMANAGE_BASE_URL}/dataservice/v1/policy-group/{VMANAGE_POLICY_GROUP_UUID}/device/variables",
            json={
                "solution": "sdwan",
                "devices": [{"device-id": uuid, "variables": var_list} for _, uuid, var_list in devices_info],
            },
            timeout=60,
        )
        if not resp.ok:
            log(log_ip, f"vManage: policy variables HTTP {resp.status_code} (non-fatal) — {resp.text[:500]!r}", console=True)
        else:
            log(log_ip, f"vManage: policy variables set (HTTP {resp.status_code})", console=True)

        # ── Step 4: Deploy all devices in a single task ───────────────────────
        set_all("DEPLOYING")
        log(log_ip, f"vManage: deploying policy group to {len(devices_info)} device(s)", console=True)
        resp = vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/v1/policy-group/{VMANAGE_POLICY_GROUP_UUID}/device/deploy",
            json={"devices": [{"id": uuid} for _, uuid, _ in devices_info]},
            timeout=60,
        )
        if not resp.ok:
            log(log_ip, f"vManage: policy deploy HTTP {resp.status_code} — {resp.text[:500]!r}", console=True)
        resp.raise_for_status()

        # ── Step 5: Poll task until vManage reports completion ─────────────────
        action_id = None
        try:
            body = resp.json()
            action_id = body.get("id") or body.get("actionId") or body.get("taskId")
            if not action_id:
                log(log_ip, f"vManage: policy deploy response body (no action ID): {body}")
        except Exception:
            pass

        if action_id:
            log(log_ip, f"vManage: policy deploy task {action_id} — polling for completion", console=True)
            deadline = time.time() + 1800
            while time.time() < deadline:
                time.sleep(15)
                try:
                    r = vmanage_session.get(
                        f"{VMANAGE_BASE_URL}/dataservice/device/action/status/{action_id}",
                        timeout=15,
                    )
                    if not r.ok:
                        log(log_ip, f"vManage: policy task poll HTTP {r.status_code} — retrying")
                        continue
                    body = r.json()
                    summary_status = body.get("summary", {}).get("status", "")
                    log(log_ip, f"vManage: policy task {action_id} status={summary_status!r}")
                    if summary_status.lower() == "done":
                        device_results = body.get("data", [])
                        failures = [d for d in device_results if d.get("status", "").lower() == "failure"]
                        if failures:
                            raise ValueError(f"policy deploy task reported failure: {failures[0].get('activity', failures[0])}")
                        break
                    elif summary_status.lower() in ("error", "failed"):
                        raise ValueError(f"policy deploy task failed with status: {summary_status!r}")
                except ValueError:
                    raise
                except Exception as exc:
                    log(log_ip, f"vManage: policy task poll error: {exc}")
            else:
                raise ValueError("policy deploy task did not complete within 30 minutes")
        else:
            log(log_ip, "vManage: policy deploy had no action ID — waiting for vManage tasks to clear", console=True)
            set_all("DEPLOYING")
            if not _wait_for_vmanage_idle(log_ip, timeout=1800):
                raise ValueError("policy deploy task did not complete within 30 minutes")

        set_all("DEPLOYED")
        log(log_ip, f"vManage: policy group DEPLOYED for site {site_key}", console=True)
        webex_notify(f"✅ **{site_key}**: policy group DEPLOYED")

    except requests.HTTPError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        log(log_ip, f"vManage: policy deploy FAILED: {exc}  body={body!r}", console=True)
        webex_notify(f"⚠️ **FAILURE** site {site_key}: policy group deploy failed — {exc}")
        set_all("FAILED")
        with state_lock:
            for ip in site_ips:
                _policy_retry_after[ip] = time.time() + 300
    except Exception as exc:
        log(log_ip, f"vManage: policy deploy FAILED: {exc}", console=True)
        webex_notify(f"⚠️ **FAILURE** site {site_key}: policy group deploy failed — {exc}")
        set_all("FAILED")
        with state_lock:
            for ip in site_ips:
                _policy_retry_after[ip] = time.time() + 300

    save_status()


def run_speedtest(ip: str) -> None:
    """
    Run a speedtest from this device to the THN hub. Runs in its own thread.

    Phase 1 (outside semaphore): pre-flight checks and CIRCUIT wait.
    Phase 2 (inside semaphore):  actual vManage API calls — serialised so only
                                 one test runs at a time.
    """
    # ── Duplicate-spawn guard ─────────────────────────────────────────────────
    with state_lock:
        if ip in _speedtest_running:
            log(ip, "Speedtest: thread already running — skipping duplicate spawn")
            return
        _speedtest_running.add(ip)

    try:
        # ── Phase 1: pre-flight (no semaphore — circuit wait can take 30 min) ─
        if not vmanage_session:
            log(ip, "Speedtest: no vManage session — skipping", console=True)
            with state_lock:
                speedtest_status[ip] = "SKIPPED"
            save_status()
            return

        hostname = hostnames.get(ip, "")
        m = re.search(r'-R(\d+)$', hostname)
        if not m:
            log(ip, f"Speedtest: cannot determine R1/R2 from hostname {hostname!r} — skipping", console=True)
            with state_lock:
                speedtest_status[ip] = "SKIPPED"
            save_status()
            return

        device_index = m.group(1)
        src_color = SPEEDTEST_SRC_COLOR.get(device_index)
        log(ip, f"Speedtest: {hostname} → color={src_color!r}")
        if not src_color:
            log(ip, f"Speedtest: no color available for R{device_index} — skipping", console=True)
            with state_lock:
                speedtest_status[ip] = "SKIPPED"
            save_status()
            return

        # Wait for CIRCUIT: CONNECTED (outside semaphore so blocked devices don't
        # prevent other ready devices from running their tests)
        with state_lock:
            circuit = device_info.get(ip, {}).get('circuit', '')
        if not circuit.startswith('CONNECTED'):
            log(ip, "Speedtest: waiting for CIRCUIT CONNECTED…", console=True)
            with state_lock:
                speedtest_status[ip] = "WAIT CIRCUIT"
            last_logged = ""
            while True:
                with state_lock:
                    circuit = device_info.get(ip, {}).get('circuit', '')
                    ip_in_dev_info = ip in device_info
                if circuit != last_logged:
                    log(ip, f"Speedtest: CIRCUIT check — value={circuit!r}  in_device_info={ip_in_dev_info}", console=True)
                    last_logged = circuit
                if circuit.startswith('CONNECTED'):
                    log(ip, "Speedtest: CIRCUIT now CONNECTED — proceeding", console=True)
                    break
                time.sleep(10)

        # ── Phase 2: run the test (serialised) ────────────────────────────────
        with _speedtest_sem:
            _run_speedtest(ip, src_color)

    finally:
        with state_lock:
            _speedtest_running.discard(ip)


def _kill_orphaned_speedtest_session(ip: str) -> bool:
    """Query vManage statistics for any recent non-completed speedtest for ip and disable it.
    Returns True if a session was found and the disable call was made."""
    try:
        cutoff_ms = int((time.time() - 7200) * 1000)  # last 2 hours
        r = vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/statistics/speedtest",
            json={
                "query": {
                    "condition": "AND",
                    "rules": [
                        {"value": [ip],              "field": "source_local_ip", "type": "string", "operator": "in"},
                        {"value": [str(cutoff_ms)],  "field": "entry_time",      "type": "date",   "operator": "greater"},
                    ],
                },
                "fields": ["entry_time", "status", "session_id"],
                "size": 10,
                "sort": [{"field": "entry_time", "type": "date", "order": "desc"}],
            },
            timeout=15,
        )
        if r.ok:
            for record in r.json().get("data", []):
                sid    = record.get("session_id")
                status = record.get("status", "")
                if sid and status not in ("completed", "complete", "done"):
                    log(ip, f"Speedtest: found orphaned session {sid} (status={status!r}) via statistics — disabling")
                    d = vmanage_session.get(
                        f"{VMANAGE_BASE_URL}/dataservice/stream/device/speed/disable/{sid}",
                        timeout=15,
                    )
                    log(ip, f"Speedtest: disable orphaned session HTTP {d.status_code}")
                    return True
        else:
            log(ip, f"Speedtest: statistics query for orphaned session returned HTTP {r.status_code}")
    except Exception as exc:
        log(ip, f"Speedtest: could not query/kill orphaned session: {exc}")
    return False


def _run_speedtest(ip: str, src_color: str) -> None:
    with state_lock:
        speedtest_status[ip] = "RUNNING"

    log(ip, f"Speedtest: starting  src={ip} color={src_color}  dst={SPEEDTEST_DST_IP} color={SPEEDTEST_DST_COLOR}", console=True)

    try:
        # Disable any previous session for this device (handles orphaned sessions
        # left over from script restarts or failed cleanup)
        with state_lock:
            prev_session = _speedtest_session.pop(ip, None)
        if prev_session:
            log(ip, f"Speedtest: disabling previous session {prev_session} before starting")
            try:
                vmanage_session.get(
                    f"{VMANAGE_BASE_URL}/dataservice/stream/device/speed/disable/{prev_session}",
                    timeout=15,
                )
                time.sleep(3)
            except Exception as exc:
                log(ip, f"Speedtest: could not disable previous session: {exc}")

        # Get source UUID
        src_uuid = _vmanage_get_device_uuid(ip)
        if not src_uuid:
            raise ValueError(f"UUID not found for {ip}")

        # Enable data stream
        vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/settings/configuration/vmanagedatastream",
            json={"enable": True, "ipType": "systemIp", "serverHostName": "systemIp", "vpn": "0"},
            timeout=15,
        )

        # Start speedtest session
        resp = vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/stream/device/speed",
            json={
                "deviceUUID":       src_uuid,
                "sourceIp":         ip,
                "sourceColor":      src_color,
                "destinationIp":    SPEEDTEST_DST_IP,
                "destinationColor": SPEEDTEST_DST_COLOR,
                "port":             "80",
            },
            timeout=30,
        )
        if not resp.ok:
            log(ip, f"Speedtest: POST /stream/device/speed HTTP {resp.status_code}: {resp.text[:400]}")
            # TBST1008 = another session is still active on vManage (e.g. orphaned from a
            # script restart). We don't have the session_id, so set a backoff and let
            # the session expire naturally on vManage before retrying.
            try:
                err_code = resp.json().get("error", {}).get("code", "")
            except Exception:
                err_code = ""
            if err_code == "TBST1008":
                # Try to find and disable the orphaned session via statistics API
                # (catches the case where the session ID wasn't persisted across restarts)
                killed = _kill_orphaned_speedtest_session(ip)
                if killed:
                    # Short wait then let the caller retry immediately
                    with state_lock:
                        _speedtest_retry_after[ip] = time.time() + 15
                    log(ip, "Speedtest: TBST1008 — disabled orphaned session, retrying in 15s", console=True)
                else:
                    with state_lock:
                        _speedtest_retry_after[ip] = time.time() + 300
                    log(ip, "Speedtest: TBST1008 — orphaned session active, backing off 5 minutes", console=True)
        resp.raise_for_status()
        session_id = resp.json()["sessionId"]
        with state_lock:
            _speedtest_session[ip] = session_id
        log(ip, f"Speedtest: session {session_id}")

        # Kick off
        vmanage_session.get(
            f"{VMANAGE_BASE_URL}/dataservice/stream/device/speed/start/{session_id}",
            timeout=15,
        ).raise_for_status()

        # Poll until completed or timeout
        deadline = time.time() + 600
        last_status = "unknown"
        _consecutive_tbst1014 = 0
        while time.time() < deadline:
            time.sleep(5)
            r = vmanage_session.get(
                f"{VMANAGE_BASE_URL}/dataservice/stream/device/speed/{session_id}",
                params={"logId": 2}, timeout=15,
            )
            if not r.ok:
                log(ip, f"Speedtest: poll HTTP {r.status_code} — {r.text[:200]}")
                # TBST1014 = device still running a prior stuck speedtest that
                # our disable call didn't clear.  If this persists, back off and
                # let the device finish/reset on its own.
                try:
                    if r.json().get("error", {}).get("code", "") == "TBST1014":
                        _consecutive_tbst1014 += 1
                        if _consecutive_tbst1014 >= 6:   # 30s of consecutive blocks
                            with state_lock:
                                _speedtest_retry_after[ip] = time.time() + 600
                            raise ValueError(
                                f"TBST1014: device has a stuck speedtest blocking session "
                                f"{session_id} — backing off 10 minutes"
                            )
                    else:
                        _consecutive_tbst1014 = 0
                except ValueError:
                    raise
                except Exception:
                    _consecutive_tbst1014 = 0
                continue
            _consecutive_tbst1014 = 0
            try:
                body = r.json()
                if isinstance(body, dict) and "status" in body:
                    status = body["status"]
                elif isinstance(body, dict) and "data" in body:
                    entries = body.get("data", [])
                    status = entries[-1].get("status", "progress") if entries else "progress"
                else:
                    status = "progress"
                    log(ip, f"Speedtest: poll unexpected body shape: {str(body)[:200]}")
            except Exception as exc:
                status = "progress"
                log(ip, f"Speedtest: poll parse error: {exc}")
            if status != last_status:
                log(ip, f"Speedtest: poll status={status!r}")
                last_status = status
            if status in ("completed", "complete", "done"):
                break
        else:
            log(ip, f"Speedtest: poll timed out after 600s — last status={last_status!r}")

        # Clean up session
        vmanage_session.get(
            f"{VMANAGE_BASE_URL}/dataservice/stream/device/speed/disable/{session_id}",
            timeout=15,
        )
        with state_lock:
            _speedtest_session.pop(ip, None)

        # Fetch results — vManage may take a few seconds to index the result, so
        # retry with increasing delays before giving up.
        data = []
        for attempt, wait in enumerate([5, 10, 15, 30], start=1):
            time.sleep(wait)
            log(ip, f"Speedtest: querying statistics (attempt {attempt})")
            r = vmanage_session.post(
                f"{VMANAGE_BASE_URL}/dataservice/statistics/speedtest",
                json={
                    "query": {
                        "condition": "AND",
                        "rules": [
                            {"value": [session_id], "field": "session_id", "type": "string", "operator": "in"},
                            {"value": ["completed"], "field": "status",    "type": "string", "operator": "in"},
                        ],
                    },
                    "fields": ["entry_time", "down_speed", "up_speed", "source_circuit", "destination_circuit", "session_id"],
                    "size": 5,
                    "sort": [{"field": "entry_time", "type": "date", "order": "desc"}],
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                log(ip, f"Speedtest: result found by session_id on attempt {attempt}")
                break
            log(ip, f"Speedtest: no result by session_id on attempt {attempt} — raw: {r.text[:400]}")
            # Fallback: query by source IP (catches older vManage versions where session_id isn't indexed)
            r2 = vmanage_session.post(
                f"{VMANAGE_BASE_URL}/dataservice/statistics/speedtest",
                json={
                    "query": {
                        "condition": "AND",
                        "rules": [
                            {"value": [ip],          "field": "source_local_ip", "type": "string", "operator": "in"},
                            {"value": ["completed"], "field": "status",           "type": "string", "operator": "in"},
                        ],
                    },
                    "fields": ["entry_time", "down_speed", "up_speed", "source_circuit", "destination_circuit", "session_id"],
                    "size": 5,
                    "sort": [{"field": "entry_time", "type": "date", "order": "desc"}],
                },
                timeout=15,
            )
            r2.raise_for_status()
            data = r2.json().get("data", [])
            if data:
                log(ip, f"Speedtest: result found by source_local_ip on attempt {attempt}")
                break
            log(ip, f"Speedtest: no result by source_local_ip on attempt {attempt} — raw: {r2.text[:400]}")
        if not data:
            raise ValueError("no completed result found in statistics after 4 attempts")

        result = data[0]
        dl = result.get("down_speed", 0)
        ul = result.get("up_speed", 0)
        label = f"↓{dl:.1f} ↑{ul:.1f} Mbps"
        log(ip, f"Speedtest: {label}  circuit={result.get('source_circuit')}→{result.get('destination_circuit')}", console=True)
        hostname = hostnames.get(ip, ip)
        site_key = re.sub(r'-R\d+$', '', hostname)  # e.g. SC-3-0079
        webex_notify(f"📊 **{hostname}** (`{ip}`): speedtest {label}")
        with state_lock:
            speedtest_status[ip] = label
            upgrade_ok = completed.get(ip) in PIPELINE_READY
            config_ok  = vmanage_status.get(ip) == "DEPLOYED"
            if upgrade_ok and config_ok and site_key not in ready_for_switch:
                # Find all routers at this site (CSV is authoritative; fall back to hostnames)
                site_ips = [
                    i for i, row in csv_vars.items()
                    if re.sub(r'-R\d+$', '', row.get('Host Name', '')) == site_key
                ]
                if not site_ips:
                    site_ips = [i for i, hn in hostnames.items()
                                if re.sub(r'-R\d+$', '', hn) == site_key]

                new_circuit_ips      = [i for i in site_ips if circuit_type.get(i) == 'new']
                migrated_circuit_ips = [i for i in site_ips if circuit_type.get(i) == 'migrated']

                if len(site_ips) <= 1:
                    # Single router — fire immediately on speedtest completion
                    required_ips = site_ips or [ip]
                elif not new_circuit_ips:
                    # All migrated circuits — first speedtest done is enough
                    required_ips = [ip]
                elif not migrated_circuit_ips:
                    # All new circuits — every router must complete speedtest
                    required_ips = site_ips
                else:
                    # Mixed — only the new-circuit router(s) must complete
                    required_ips = new_circuit_ips

                if all(str(speedtest_status.get(i, '')).startswith('↓') for i in required_ips):
                    ready_for_switch[site_key] = hostname
                    log(ip, f"{hostname} is READY for SWITCH (circuit types: {[(i, circuit_type.get(i,'?')) for i in site_ips]})", console=True)
                    webex_notify(f"✅ **{hostname}** is READY for SWITCH — {label}")

        _try_trigger_policy_for_site(ip)

    except Exception as exc:
        log(ip, f"Speedtest FAILED: {exc}", console=True)
        webex_notify(f"⚠️ **FAILURE** {hostnames.get(ip, ip)} (`{ip}`): speedtest failed — {exc}")
        with state_lock:
            speedtest_status[ip] = "FAILED"

    save_status()


def _vmanage_build_variable_list(ip: str, target_group: str, uuid: str, csv_row: dict,
                                  group_type: str = "config-group") -> list[dict]:
    """
    Build [{name, value}] for PUT /v1/{group_type}/{id}/device/variables.

    GETs the full variable list from an existing device in the group to learn
    variable names and their correct Python types.  Falls back to smart
    inference if no template device is available.
    """
    reverse_col_map = {vm: csv_col for csv_col, vm in VMANAGE_COL_MAP.items()}

    type_map: dict[str, type] = {}
    all_var_names: list[str] = []

    try:
        resp = vmanage_session.get(
            f"{VMANAGE_BASE_URL}/dataservice/v1/{group_type}/{target_group}/device/variables",
            timeout=15,
        )
        if resp.ok:
            devices = resp.json().get("devices", [])
            # Prefer a device that isn't ours so we get real typed values
            template_dev = next(
                (d for d in devices if d.get("device-id") != uuid and d.get("variables")),
                next((d for d in devices if d.get("variables")), None),
            )
            if template_dev:
                for v in template_dev["variables"]:
                    name = v["name"]
                    all_var_names.append(name)
                    val = v.get("value")
                    if val is not None:
                        type_map[name] = type(val)
                log(ip, f"vManage: {group_type} schema — {len(all_var_names)} var(s), types: { {n: type_map[n].__name__ for n in all_var_names if n in type_map} }")
    except Exception as exc:
        log(ip, f"vManage: variable list fetch failed (using inference): {exc}")

    if not all_var_names:
        # No template — use snake_case CSV keys plus the 5 mapped metadata columns
        all_var_names = list(reverse_col_map.keys()) + [
            k for k in csv_row if k not in VMANAGE_COL_MAP and re.match(r"^[a-zA-Z0-9_]+$", k)
        ]

    var_list: list[dict] = []
    for vname in all_var_names:
        csv_col = reverse_col_map.get(vname, vname)
        raw = csv_row.get(csv_col, "").strip()
        if not raw:
            continue

        vtype = type_map.get(vname)
        try:
            if vtype is list or vname.endswith("_dhcp_exclude"):
                try:
                    parsed = json.loads(raw)
                    converted: object = parsed if isinstance(parsed, list) else [str(parsed)]
                except (json.JSONDecodeError, ValueError):
                    if '";"' in raw:
                        converted = [s.strip() for s in raw.split('";"') if s.strip()]
                    else:
                        converted = [raw]
            elif vtype is bool:
                converted = raw.lower() not in ("false", "0", "no", "")
            elif vtype is int:
                converted = int(float(raw))
            elif vtype is float:
                converted = float(raw)
            elif vtype is str:
                converted = raw
            else:
                # Unknown type — infer from value rather than sending a raw string,
                # which causes SCHVALID0001 for numeric/boolean fields whose template
                # device had null values and so contributed no type information.
                raw_lower = raw.lower()
                if raw_lower in ("true", "false"):
                    converted = raw_lower == "true"
                else:
                    try:
                        converted = int(raw)
                    except ValueError:
                        try:
                            converted = float(raw)
                        except ValueError:
                            converted = raw
        except (ValueError, TypeError):
            converted = raw

        var_list.append({"name": vname, "value": converted})

    return var_list


def move_to_final_config_group(ip: str) -> None:
    """Move device from onboarding to final config group. Runs in its own thread."""
    with state_lock:
        upgrade_done   = completed.get(ip) in PIPELINE_READY
        config_status  = device_info.get(ip, {}).get('config', '?')
        hostname       = hostnames.get(ip, "")

    if not upgrade_done:
        log(ip, "vManage: skipping config group move — upgrade not complete", console=True)
        return

    if config_status == 'COMPLETE':
        log(ip, "vManage: CONFIG already COMPLETE — marking DEPLOYED and resuming pipeline", console=True)
        with state_lock:
            vmanage_status[ip] = "DEPLOYED"
            existing_spd = speedtest_status.get(ip, "")
        save_status()
        webex_notify(f"✅ **{hostnames.get(ip, ip)}** (`{ip}`): config group DEPLOYED")
        if not str(existing_spd).startswith("↓"):
            threading.Thread(target=run_speedtest, args=(ip,), daemon=True).start()
        _try_trigger_policy_for_site(ip)
        return

    # Claim the deploy slot atomically so a concurrent _collect_one trigger
    # (which fires when vmanage_status is None and ssh_config is REQUIRED) bails
    # out rather than running a second full deploy in parallel.
    with state_lock:
        current_vm = vmanage_status.get(ip)
        if current_vm in ("WAITING", "ASSOCIATING", "SETTING VARS", "DEPLOYING"):
            log(ip, f"vManage: config deploy already in-progress ({current_vm}) — skipping duplicate", console=True)
            return
        vmanage_status[ip] = "WAITING"

    if config_status == '?':
        # Info not yet collected post-reboot — wait up to 3 minutes for the info collector to populate it
        log(ip, "vManage: CONFIG not yet polled — waiting for info collector…", console=True)
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(10)
            with state_lock:
                config_status = device_info.get(ip, {}).get('config', '?')
            if config_status != '?':
                break
        if config_status == '?':
            log(ip, "vManage: CONFIG still unknown after 3 minutes — proceeding anyway (device is on onboarding group post-upgrade)", console=True)
        elif config_status == 'COMPLETE':
            log(ip, "vManage: CONFIG now COMPLETE — marking DEPLOYED and resuming pipeline", console=True)
            with state_lock:
                vmanage_status[ip] = "DEPLOYED"
                existing_spd = speedtest_status.get(ip, "")
            save_status()
            webex_notify(f"✅ **{hostnames.get(ip, ip)}** (`{ip}`): config group DEPLOYED")
            if not str(existing_spd).startswith("↓"):
                threading.Thread(target=run_speedtest, args=(ip,), daemon=True).start()
            _try_trigger_policy_for_site(ip)
            return

    if not vmanage_session:
        log(ip, "vManage: no session — skipping config group move", console=True)
        with state_lock:
            vmanage_status[ip] = "SKIPPED"
        save_status()
        return

    if not _wait_for_vmanage_idle(ip):
        log(ip, "vManage: timed out waiting for running tasks to clear — skipping move", console=True)
        with state_lock:
            vmanage_status[ip] = "FAILED"
        save_status()
        return

    with state_lock:
        vmanage_status[ip] = "ASSOCIATING"

    uuid: str | None = None
    disassociated = False
    try:
        if not hostname:
            raise ValueError("hostname not known — cannot derive site-id")

        m = re.match(r'^SC-(\d+)-(\d{4})-', hostname)
        if not m:
            raise ValueError(f"cannot parse hostname: {hostname}")
        site_type = m.group(1)

        target_group = VMANAGE_FINAL_GROUPS.get(site_type)
        if not target_group:
            raise ValueError(f"no final config group for site-type {site_type}")

        csv_row = csv_vars.get(ip)
        if not csv_row:
            raise ValueError(f"no CSV variables found for system-ip {ip}")
        log(ip, f"vManage: loaded {len(csv_row)} CSV columns")

        uuid = _vmanage_get_device_uuid(ip)
        if not uuid:
            raise ValueError(f"device UUID not found for system-ip {ip}")
        log(ip, f"vManage: device UUID = {uuid}")

        # ── Step 1: Disassociate from onboarding config group ─────────────────
        log(ip, "vManage: disassociating from onboarding config group", console=True)
        resp = vmanage_session.delete(
            f"{VMANAGE_BASE_URL}/dataservice/v1/config-group/{VMANAGE_ONBOARD_UUID}/device/associate",
            json={"devices": [{"id": uuid}]},
            timeout=60,
        )
        if not resp.ok:
            log(ip, f"vManage: disassociate HTTP {resp.status_code} — body: {resp.text[:500]!r}", console=True)
        resp.raise_for_status()
        log(ip, f"vManage: disassociate successful (HTTP {resp.status_code})", console=True)
        disassociated = True

        # ── Step 2: Wait after disassociate ───────────────────────────────────
        with state_lock:
            vmanage_status[ip] = "WAITING"
        if not _wait_for_vmanage_idle(ip):
            raise ValueError("timed out waiting after disassociate")

        # ── Step 3: Associate with final group (no variables at this stage) ────
        log(ip, f"vManage: associating with final config group (site-type={site_type})", console=True)
        resp = vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/v1/config-group/{target_group}/device/associate",
            json={"devices": [{"id": uuid}]},
            timeout=60,
        )
        if not resp.ok:
            log(ip, f"vManage: associate HTTP {resp.status_code} — body: {resp.text[:500]!r}", console=True)
        resp.raise_for_status()
        log(ip, f"vManage: associate successful (HTTP {resp.status_code})", console=True)

        # ── Step 4: Wait after associate ──────────────────────────────────────
        with state_lock:
            vmanage_status[ip] = "WAITING"
        if not _wait_for_vmanage_idle(ip):
            raise ValueError("timed out waiting after associate")

        # ── Step 5: Set per-device variables ──────────────────────────────────
        with state_lock:
            vmanage_status[ip] = "SETTING VARS"
        log(ip, "vManage: setting device variables", console=True)
        var_list = _vmanage_build_variable_list(ip, target_group, uuid, csv_row)
        log(ip, f"vManage: built {len(var_list)} variable entries from CSV")
        resp = vmanage_session.put(
            f"{VMANAGE_BASE_URL}/dataservice/v1/config-group/{target_group}/device/variables",
            json={"solution": "sdwan", "devices": [{"device-id": uuid, "variables": var_list}]},
            timeout=60,
        )
        if not resp.ok:
            log(ip, f"vManage: set variables HTTP {resp.status_code} — body: {resp.text[:500]!r}", console=True)
        resp.raise_for_status()
        log(ip, f"vManage: variables set successfully (HTTP {resp.status_code})", console=True)

        # ── Step 6: Deploy ─────────────────────────────────────────────────────
        with state_lock:
            vmanage_status[ip] = "DEPLOYING"
        log(ip, "vManage: deploying config group to device", console=True)
        resp = vmanage_session.post(
            f"{VMANAGE_BASE_URL}/dataservice/v1/config-group/{target_group}/device/deploy",
            json={"devices": [{"id": uuid}]},
            timeout=60,
        )
        if not resp.ok:
            log(ip, f"vManage: deploy HTTP {resp.status_code} — body: {resp.text[:500]!r}", console=True)
        resp.raise_for_status()
        log(ip, f"vManage: deploy submitted (HTTP {resp.status_code})", console=True)

        # ── Step 7: Poll task to completion ───────────────────────────────────
        action_id = None
        try:
            body = resp.json()
            action_id = body.get("id") or body.get("actionId") or body.get("taskId")
            if not action_id:
                log(ip, f"vManage: config deploy response body: {body}")
        except Exception:
            pass

        if action_id:
            log(ip, f"vManage: config deploy task {action_id} — polling for completion", console=True)
            deadline = time.time() + 1800
            while time.time() < deadline:
                time.sleep(15)
                try:
                    r = vmanage_session.get(
                        f"{VMANAGE_BASE_URL}/dataservice/device/action/status/{action_id}",
                        timeout=15,
                    )
                    if not r.ok:
                        log(ip, f"vManage: config task poll HTTP {r.status_code} — retrying")
                        continue
                    body = r.json()
                    summary_status = body.get("summary", {}).get("status", "")
                    log(ip, f"vManage: config task {action_id} status={summary_status!r}")
                    if summary_status.lower() == "done":
                        device_results = body.get("data", [])
                        failures = [d for d in device_results if d.get("status", "").lower() == "failure"]
                        if failures:
                            raise ValueError(f"config deploy task reported failure: {failures[0].get('activity', failures[0])}")
                        break
                    elif summary_status.lower() in ("error", "failed"):
                        raise ValueError(f"config deploy task failed with status: {summary_status!r}")
                except ValueError:
                    raise
                except Exception as exc:
                    log(ip, f"vManage: config task poll error: {exc}")
            else:
                raise ValueError("config deploy task did not complete within 30 minutes")
        else:
            log(ip, "vManage: config deploy returned no action ID — skipping task poll, verifying via SSH", console=True)

        # Verify the device actually received the config via SSH — required whether or not
        # we had a task ID to poll (task "done" doesn't guarantee push reached the device
        # if it lost connectivity to vManage mid-deploy)
        log(ip, "vManage: verifying config applied via SSH…", console=True)
        verify_deadline = time.time() + 300
        config_verified = False
        while time.time() < verify_deadline:
            time.sleep(30)
            try:
                ssh_client = ssh_connect(ip)
                ssh_client, ssh_out = run_command(ssh_client, ip, "show sdwan system", timeout=SSH_TIMEOUT)
                ssh_client.close()
                for line in ssh_out.splitlines():
                    if any(k in line.lower() for k in ('configuration template', 'config-template', 'config template')):
                        parts = [p.strip() for p in re.split(r':\s*|\s{2,}', line.strip()) if p.strip()]
                        val = parts[-1].lower() if len(parts) >= 2 else ''
                        if val.startswith('type'):
                            config_verified = True
                        break
            except Exception as exc:
                log(ip, f"vManage: SSH config verify error: {exc}")
            # Fallback: accept device_info from _collect_one if direct SSH fails
            if not config_verified:
                with state_lock:
                    collected_config = device_info.get(ip, {}).get('config', '')
                if collected_config == 'COMPLETE':
                    log(ip, "vManage: SSH verify failed but info-collector confirms CONFIG: COMPLETE — accepting")
                    config_verified = True
            if config_verified:
                break
            log(ip, "vManage: device still on onboard config — waiting for push to arrive…")
        if not config_verified:
            raise ValueError("config deploy task completed but device still on onboard config after 5 minutes — device likely lost connectivity to vManage during push")

        with state_lock:
            vmanage_status[ip] = "DEPLOYED"
            speedtest_status[ip] = "PENDING"
        webex_notify(f"✅ **{hostnames.get(ip, ip)}** (`{ip}`): config group DEPLOYED")
        threading.Thread(target=run_speedtest, args=(ip,), daemon=True).start()
        _try_trigger_policy_for_site(ip)

    except requests.HTTPError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        log(ip, f"vManage: config group deploy FAILED: {exc}  body={body!r}", console=True)
        webex_notify(f"⚠️ **FAILURE** {hostnames.get(ip, ip)} (`{ip}`): config group deploy failed — {exc}")
        with state_lock:
            vmanage_status[ip] = "FAILED"
        if disassociated:
            _vmanage_rollback_to_onboard(ip, uuid)
    except Exception as exc:
        log(ip, f"vManage: config group deploy FAILED: {exc}", console=True)
        webex_notify(f"⚠️ **FAILURE** {hostnames.get(ip, ip)} (`{ip}`): config group deploy failed — {exc}")
        with state_lock:
            vmanage_status[ip] = "FAILED"
        if disassociated:
            _vmanage_rollback_to_onboard(ip, uuid)

    save_status()


vmanage_poll_status: dict = {"last_poll": None, "error": None, "raw_count": 0}

def _poll_vmanage_tasks() -> None:
    """Fetch active tasks from vManage and update the global vmanage_tasks list."""
    if not vmanage_session:
        vmanage_poll_status["error"] = "no session"
        return
    try:
        resp = vmanage_session.get(
            f"{VMANAGE_BASE_URL}/dataservice/device/action/status/tasks",
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        all_tasks = data.get("runningTasks", [])
        log("vManage", f"Task poll: {len(all_tasks)} running task(s)")
        with vmanage_tasks_lock:
            vmanage_tasks[:] = all_tasks
        vmanage_poll_status.update({"last_poll": ts(), "error": None, "raw_count": len(all_tasks)})
    except Exception as exc:
        vmanage_poll_status.update({"last_poll": ts(), "error": str(exc)})
        log("vManage", f"Task poll error: {exc}")


def vmanage_task_monitor_loop() -> None:
    """Background thread: poll vManage tasks every 30 seconds."""
    while True:
        _poll_vmanage_tasks()
        time.sleep(30)


def wait_for_reboot(ip: str) -> bool:
    """
    Wait until the device responds to ping again after a reboot.
    Returns True if it came back within REBOOT_TIMEOUT, False otherwise.
    """
    log(ip, "Waiting for device to go offline (reboot initiated)…")
    deadline = time.time() + REBOOT_TIMEOUT

    # Wait for it to go down first
    down_seen = False
    while time.time() < deadline:
        if not ping_once(ip):
            down_seen = True
            log(ip, "Device is offline (rebooting)…")
            break
        time.sleep(REBOOT_POLL)

    if not down_seen:
        log(ip, "WARNING: Device never went offline — may not have rebooted")

    # Wait for it to come back
    log(ip, "Waiting for device to come back online…")
    while time.time() < deadline:
        if ping_once(ip):
            log(ip, "Device is back online")
            return True
        time.sleep(REBOOT_POLL)

    log(ip, "ERROR: Device did not come back within timeout")
    return False


# ── Install operation status helpers ──────────────────────────────────────────

def _query_install_op_status(client: paramiko.SSHClient, ip: str) -> tuple[paramiko.SSHClient, str]:
    """
    After an install SSH timeout or reconnect, determine whether the platform
    install operation is still running, succeeded, or failed.

    Runs both:
      show platform software install RP active operation current detail
      show platform software install RP active operation history detail

    Returns (client, status) where status is one of:
      "success"  — most recent add completed OK
      "running"  — operation still in progress
      "failed"   — operation reported a failure
      "unknown"  — could not determine outcome
    """
    # Check for a currently-running operation
    client, current = run_command(
        client, ip,
        "show platform software install RP active operation current detail",
        timeout=SSH_TIMEOUT,
    )
    if current.strip():
        log(ip, f"Install operation (current):\n{current.strip()}")
    else:
        log(ip, "Install operation (current): no active operation")

    current_s = current.strip().lower()
    if current_s and "no current" not in current_s and len(current_s) > 20:
        if "status: failed" in current_s or "status: error" in current_s:
            return client, "failed"
        return client, "running"

    # No active operation — check history for the outcome of the last add
    client, history = run_command(
        client, ip,
        "show platform software install RP active operation history detail",
        timeout=SSH_TIMEOUT,
    )
    log(ip, f"Install operation (history):\n{history.strip()}")

    # Parse the summary table lines: <uuid> <op_id> <command> <status> ...
    for line in history.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "add":
            if parts[3] == "OK":
                return client, "success"
            if parts[3] in ("Failed", "FAILED", "Error", "ERROR"):
                return client, "failed"

    # Fall back to scanning the detail section
    for line in history.splitlines():
        ll = line.lower()
        if "command: add" in ll:
            if "status: ok" in ll:
                return client, "success"
            if "status: fail" in ll or "status: error" in ll:
                return client, "failed"

    return client, "unknown"


def _wait_for_site_peers_before_reboot(ip: str) -> None:
    """
    Hold an activate/reboot if this device owns the active WAN circuit and a
    site peer is still mid-upgrade.  The peer routes through our WAN via TLOC
    extension, so rebooting us now would kill its SSH session.
    """
    with state_lock:
        my_circuit = device_info.get(ip, {}).get('circuit', '')

    if not my_circuit.startswith("CONNECTED"):
        return  # we don't own the WAN — our reboot can't hurt peers

    hostname = hostnames.get(ip, "")
    if not hostname:
        return
    site_key = re.sub(r'-R\d+$', '', hostname)

    while True:
        with state_lock:
            blocking = {
                i: in_progress_step.get(i, 'upgrading')
                for i, hn in hostnames.items()
                if i != ip
                and re.sub(r'-R\d+$', '', hn) == site_key
                and i in in_progress
            }
        if not blocking:
            return
        log(ip,
            f"Holding activate — peer(s) upgrading via our WAN circuit: {blocking}",
            console=True)
        time.sleep(30)


# ── Upgrade task (runs in its own thread) ─────────────────────────────────────

def upgrade_device(ip: str) -> None:
    """Full upgrade workflow for a single device."""
    log(ip, "=== Starting upgrade workflow ===")

    def fail(reason: str, suppress_webex: bool = False) -> None:
        log(ip, f"FAILED: {reason} — queuing for retry in 5 minutes", console=True)
        if not suppress_webex:
            hostname = hostnames.get(ip, ip)
            webex_notify(f"⚠️ **FAILURE** {hostname} (`{ip}`): upgrade failed — {reason}")
        with state_lock:
            checking.discard(ip)
            checking_since.pop(ip, None)
            in_progress.discard(ip)
            in_progress_since.pop(ip, None)
            in_progress_step.pop(ip, None)
            retry_queue[ip] = time.time() + RETRY_DELAY
        save_status()

    def set_step(label: str) -> None:
        """Update the displayed upgrade step for this device."""
        with state_lock:
            in_progress_step[ip] = label
        log(ip, label, console=True)

    # ── Step 1: SSH connect ────────────────────────────────────────────────────
    try:
        client = ssh_connect(ip)
    except Exception as exc:
        fail(f"SSH connect: {exc}")
        return

    try:
        # ── Step 2: Parse version table ───────────────────────────────────────
        # Discover hostname (uses run_command — safe for IOS-XE)
        if ip not in hostnames:
            client = discover_hostname(client, ip)

        log(ip, "Checking active software version…")
        client, output = run_command(client, ip, "show sdwan software")
        log(ip, f"show sdwan software output:\n{output.strip()}")

        versions    = parse_sdwan_versions(output)
        target      = versions.get(TARGET_VERSION, {})
        is_active   = target.get('active',  False)
        is_default  = target.get('default', False)
        is_installed = TARGET_VERSION in versions

        if is_active and is_default:
            log(ip, f"Already running {TARGET_VERSION} as active+default — no upgrade needed")
            with state_lock:
                completed[ip] = "ALREADY UP TO DATE"
                checking.discard(ip)
                checking_since.pop(ip, None)
                needs_config = vmanage_status.get(ip) not in ("DEPLOYED", "SKIPPED")
            save_status()
            client.close()
            if needs_config:
                threading.Thread(target=move_to_final_config_group, args=(ip,), daemon=True).start()
            return

        # Needs work — transition to in_progress
        with state_lock:
            checking.discard(ip)
            checking_since.pop(ip, None)
            in_progress.add(ip)
            in_progress_since[ip] = time.time()
            in_progress_step[ip] = "INSTALLING"
            bin_missing.discard(ip)
            file_copying.discard(ip)
        save_status()
        if time.time() >= _bin_missing_notify_after.get(ip, 0):
            webex_notify(f"📡 **{hostnames.get(ip, ip)}** (`{ip}`): online — pipeline starting")

        if is_active and not is_default:
            # Active but set-default / remove not yet done — jump to step 7
            log(ip, f"{TARGET_VERSION} is active but not default — resuming at set-default", console=True)

        elif is_installed and not is_active:
            # Installed but activate not yet done — jump to step 5
            log(ip, f"{TARGET_VERSION} is installed but not active — resuming at activate", console=True)

            # ── Step 5: Activate (triggers reboot) ────────────────────────────
            _wait_for_site_peers_before_reboot(ip)
            set_step("ACTIVATING")
            activate_cmd = (
                f"request platform software sdwan software activate {TARGET_VERSION}"
            )
            try:
                client, activate_output = run_command(
                    client, ip, activate_cmd, timeout=CMD_TIMEOUT
                )
                log(ip, f"Activate output:\n{activate_output.strip()}")
            except Exception:
                log(ip, "Connection dropped (expected during reboot)")
            finally:
                client.close()

            # ── Step 6: Wait for reboot ───────────────────────────────────────
            if not wait_for_reboot(ip):
                fail("Device did not return after activate reboot")
                return
            log(ip, "Waiting 60s for SSH daemon to restart…")
            time.sleep(60)

        else:
            # ── Step 3: Check file exists on bootflash ────────────────────────
            log(ip, f"Checking {INSTALL_FILE} on bootflash…")
            client, dir_output = run_command(
                client, ip, f"dir {INSTALL_FILE}", timeout=SSH_TIMEOUT
            )
            file_missing = (
                "No such file" in dir_output
                or "Error"      in dir_output
                or "error"      in dir_output
            )

            if file_missing:
                # Install may have already run and consumed the bin.
                # Check software table first, then fall back to operation history.
                log(ip, f"Install file not found — re-checking software table…")
                client, sw2 = run_command(client, ip, "show sdwan software")
                log(ip, f"show sdwan software output:\n{sw2.strip()}")
                versions2 = parse_sdwan_versions(sw2)
                if TARGET_VERSION in versions2:
                    log(ip, f"{TARGET_VERSION} present — bin consumed by prior run, skipping to activate")
                else:
                    # show sdwan software may not list a staged-but-not-activated install.
                    # Check the platform install operation history for a successful add.
                    log(ip, f"{TARGET_VERSION} not in software table — checking install operation history…")
                    client, op_status = _query_install_op_status(client, ip)
                    if op_status == "success":
                        log(ip, f"{TARGET_VERSION} confirmed staged by install operation history — skipping to activate", console=True)
                    elif op_status == "running":
                        log(ip, "Install operation still running — will poll for completion", console=True)
                        poll_deadline = time.time() + INSTALL_TIMEOUT
                        while True:
                            if time.time() >= poll_deadline:
                                client.close()
                                fail("Install timed out waiting for in-progress operation")
                                return
                            time.sleep(60)
                            client, op_status = _query_install_op_status(client, ip)
                            if op_status == "success":
                                log(ip, "Install operation completed successfully", console=True)
                                break
                            if op_status in ("failed", "unknown"):
                                client.close()
                                fail(f"Install operation {op_status} (detected via history while bin missing)")
                                return
                    else:
                        client.close()
                        with state_lock:
                            bin_missing.add(ip)
                        now = time.time()
                        suppress = now < _bin_missing_notify_after.get(ip, 0)
                        if not suppress:
                            _bin_missing_notify_after[ip] = now + 900
                        fail(f"Install file not found and {TARGET_VERSION} not installed: {INSTALL_FILE}",
                             suppress_webex=suppress)
                        return
            else:
                log(ip, "Install file found on bootflash")

                # ── Size check: guard against partially-copied .bin ───────────
                if EXPECTED_FILE_SIZE > 0:
                    found_size = parse_dir_file_size(dir_output, INSTALL_FILE)
                    log(ip, f"Install file size on device: {found_size} (expected {EXPECTED_FILE_SIZE})")
                    if found_size is None or found_size != EXPECTED_FILE_SIZE:
                        client.close()
                        with state_lock:
                            file_copying.add(ip)
                        fail(
                            f"Install file size {found_size} != expected {EXPECTED_FILE_SIZE}"
                            " — copy still in progress"
                        )
                        return
                    with state_lock:
                        file_copying.discard(ip)

                # ── Step 4: Install ───────────────────────────────────────────
                set_step("INSTALLING")
                install_cmd = (
                    f"request platform software sdwan software install {INSTALL_FILE}"
                )
                try:
                    client, install_output = run_command(
                        client, ip, install_cmd,
                        timeout=INSTALL_TIMEOUT, no_retry=True,
                    )
                    log(ip, f"Install output:\n{install_output.strip()}")
                    if "error" in install_output.lower() or "failed" in install_output.lower():
                        client.close()
                        fail("Install command reported an error")
                        return
                except (EOFError, socket.timeout, TimeoutError) as exc:
                    log(ip, f"Install SSH timeout ({type(exc).__name__}) — reconnecting to check operation status", console=True)
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = ssh_connect(ip)
                    poll_deadline = time.time() + INSTALL_TIMEOUT
                    while True:
                        client, op_status = _query_install_op_status(client, ip)
                        if op_status == "success":
                            log(ip, "Install completed successfully (confirmed after SSH timeout)", console=True)
                            break
                        if op_status == "failed":
                            client.close()
                            fail("Install failed (confirmed by operation history)")
                            return
                        if op_status == "running":
                            if time.time() >= poll_deadline:
                                client.close()
                                fail("Install timed out waiting for operation to complete")
                                return
                            log(ip, "Install still in progress — waiting 60s before recheck", console=True)
                            time.sleep(60)
                            continue
                        # "unknown" — cannot determine outcome
                        client.close()
                        fail("Install operation status unknown after SSH timeout")
                        return

            # ── Step 5: Activate (triggers reboot) ────────────────────────────
            _wait_for_site_peers_before_reboot(ip)
            set_step("ACTIVATING")
            activate_cmd = (
                f"request platform software sdwan software activate {TARGET_VERSION}"
            )
            try:
                client, activate_output = run_command(
                    client, ip, activate_cmd, timeout=CMD_TIMEOUT
                )
                log(ip, f"Activate output:\n{activate_output.strip()}")
            except Exception:
                log(ip, "Connection dropped (expected during reboot)", console=True)
            finally:
                client.close()

            # ── Step 6: Wait for reboot ───────────────────────────────────────
            set_step("ACTIVATING - REBOOTING")
            if not wait_for_reboot(ip):
                fail("Device did not return after activate reboot")
                return
            log(ip, "Waiting 60s for SSH daemon to restart…", console=True)
            time.sleep(60)

        # ── Step 7: Reconnect and set-default ─────────────────────────────────
        set_step("SETTING DEFAULT")
        log(ip, "Reconnecting for set-default…")
        try:
            client = ssh_connect(ip)
        except Exception as exc:
            fail(f"Reconnect after activate failed: {exc}")
            return

        log(ip, f"Setting {TARGET_VERSION} as default…")
        setdef_cmd = (
            f"request platform software sdwan software set-default {TARGET_VERSION}"
        )
        client, setdef_output = run_command(client, ip, setdef_cmd, timeout=CMD_TIMEOUT)
        log(ip, f"Set-default output:\n{setdef_output.strip()}")
        client.close()

        # ── Step 8: Remove old version ────────────────────────────────────────
        time.sleep(10)
        set_step("REMOVING OLD CODE")
        log(ip, "Reconnecting to remove old software…")
        try:
            client = ssh_connect(ip)
        except Exception as exc:
            fail(f"Reconnect for remove failed: {exc}")
            return

        log(ip, f"Removing old version {OLD_VERSION}…")
        remove_cmd = (
            f"request platform software sdwan software remove {OLD_VERSION}"
        )
        client, remove_output = run_command(client, ip, remove_cmd, timeout=CMD_TIMEOUT)
        log(ip, f"Remove output:\n{remove_output.strip()}")
        client.close()

        # ── Done ──────────────────────────────────────────────────────────────
        log(ip, f"=== UPGRADE COMPLETE — {TARGET_VERSION} active+default ===", console=True)
        webex_notify(f"✅ **{hostnames.get(ip, ip)}** (`{ip}`): code upgrade complete — {TARGET_VERSION}")
        with state_lock:
            completed[ip] = "UPGRADE COMPLETE"
            in_progress.discard(ip)
            in_progress_since.pop(ip, None)
            in_progress_step.pop(ip, None)
        save_status()

        # ── Step 9: Move to final config group on vManage ─────────────────────
        threading.Thread(target=move_to_final_config_group, args=(ip,), daemon=True).start()

    except Exception as exc:
        try:
            client.close()
        except Exception:
            pass
        reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        fail(reason)


# ── Ping monitor ──────────────────────────────────────────────────────────────

def ping_loop(ips: list[str], display_order: list[str]) -> None:
    """Continuously ping all IPs and print a status table every cycle."""
    while True:
        now = time.time()

        # Check retry queue
        with state_lock:
            due = [ip for ip, t in list(retry_queue.items()) if now >= t]
            for ip in due:
                del retry_queue[ip]
                log(ip, "Retry delay elapsed — re-entering ping monitoring")

        for ip in ips:
            with state_lock:
                is_completed = ip in completed
                skip = not is_completed and (ip in in_progress or ip in checking)
            if skip:
                continue

            up = ping_once(ip)

            with state_lock:
                was_up = ping_results.get(ip) == "Up"
                ping_results[ip] = "Up" if up else "Down"

                if is_completed:
                    # Track consecutive misses/hits to detect circuit going offline
                    if up:
                        ping_miss_count[ip] = 0
                        ping_hit_count[ip] = ping_hit_count.get(ip, 0) + 1
                        if ip in circuit_ping_offline and ping_hit_count[ip] >= 2:
                            circuit_ping_offline.discard(ip)
                            log(ip, "Circuit back ONLINE (2 consecutive pings)", console=True)
                    else:
                        ping_hit_count[ip] = 0
                        ping_miss_count[ip] = ping_miss_count.get(ip, 0) + 1
                        if ping_miss_count[ip] >= 3 and ip not in circuit_ping_offline:
                            circuit_ping_offline.add(ip)
                            log(ip, "Circuit marked OFFLINE (3 consecutive missed pings)", console=True)
                else:
                    if up:
                        if not was_up:
                            up_since[ip] = now
                        elif now - up_since.get(ip, now) >= UP_THRESHOLD:
                            if ip not in checking and ip not in in_progress and ip not in completed:
                                checking.add(ip)
                                checking_since[ip] = now
                                t = threading.Thread(
                                    target=upgrade_device, args=(ip,), daemon=True
                                )
                                t.start()
                    else:
                        up_since.pop(ip, None)

        # Print status summary
        print_status(display_order, now)
        _maybe_coffee_break()
        time.sleep(PING_INTERVAL)


def _collect_one(ip: str) -> None:
    """Collect device info for a single IP; runs in its own thread."""
    with _info_sem:
        with state_lock:
            if ping_results.get(ip, "Down") == "Down":
                return
        last_exc = None
        for attempt in range(3):
            try:
                if attempt:
                    time.sleep(15)
                client = ssh_connect(ip)
                if ip not in hostnames:
                    client = discover_hostname(client, ip)
                collect_device_info(client, ip)
                client.close()
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                log(ip, f"Info collect SSH error (attempt {attempt + 1}/3): {type(exc).__name__}: {exc}")
        if last_exc:
            return

    with state_lock:
        ssh_config   = device_info.get(ip, {}).get('config', '?')
        vm_status    = vmanage_status.get(ip)
        upgrade_done = completed.get(ip) in PIPELINE_READY

    # Script says DEPLOYED but device is still on onboard config — re-queue the deploy
    if vm_status == "DEPLOYED" and ssh_config == "REQUIRED" and upgrade_done:
        log(ip, "Config mismatch: script says DEPLOYED but device still on onboard config — re-queuing config deploy", console=True)
        with state_lock:
            vmanage_status[ip]   = None
            speedtest_status[ip] = None
        save_status()
        threading.Thread(target=move_to_final_config_group, args=(ip,), daemon=True).start()
        return

    # Config deploy never ran or FAILED — device still on onboard config, kick it off
    if vm_status in ("FAILED", None) and ssh_config == "REQUIRED" and upgrade_done:
        log(ip, "Config deploy FAILED and device still on onboard config — re-queuing config deploy", console=True)
        with state_lock:
            vmanage_status[ip] = None
        save_status()
        threading.Thread(target=move_to_final_config_group, args=(ip,), daemon=True).start()
        return

    # Deploy in-progress but SSH already confirms final config landed — no need to wait for verify loop
    if vm_status == "DEPLOYING" and ssh_config == "COMPLETE" and upgrade_done:
        log(ip, "Config deploy in-progress but SSH confirms final config already applied — marking DEPLOYED", console=True)
        with state_lock:
            vmanage_status[ip] = "DEPLOYED"
            existing_spd = speedtest_status.get(ip, "")
            if not str(existing_spd).startswith("↓"):
                speedtest_status[ip] = "PENDING"
        save_status()
        if not str(existing_spd).startswith("↓"):
            threading.Thread(target=run_speedtest, args=(ip,), daemon=True).start()
        _try_trigger_policy_for_site(ip)
        return

    # Deploy was marked FAILED (or never ran) but SSH confirms device is already on final config group
    if vm_status in ("FAILED", None) and ssh_config == "COMPLETE" and upgrade_done:
        log(ip, "Config deploy was marked FAILED but SSH confirms device is on final config — recovering pipeline", console=True)
        with state_lock:
            vmanage_status[ip] = "DEPLOYED"
            existing_spd = speedtest_status.get(ip, "")
            if not str(existing_spd).startswith("↓"):
                speedtest_status[ip] = "PENDING"
        save_status()
        if not str(existing_spd).startswith("↓"):
            threading.Thread(target=run_speedtest, args=(ip,), daemon=True).start()
        _try_trigger_policy_for_site(ip)
        return

    # Kick speedtest if it's stuck (WAIT CIRCUIT with circuit now up) or previously failed
    with state_lock:
        circuit     = device_info.get(ip, {}).get('circuit', '')
        spd         = speedtest_status.get(ip)
        retry_after = _speedtest_retry_after.get(ip, 0)
        kickable = (spd in ("WAIT CIRCUIT", "FAILED", "SKIPPED")
                    and vmanage_status.get(ip) == "DEPLOYED"
                    and ip not in _speedtest_running
                    and circuit.startswith('CONNECTED')
                    and time.time() >= retry_after)
    if kickable:
        reason = ("stuck in WAIT CIRCUIT — circuit now CONNECTED" if spd == "WAIT CIRCUIT"
                  else "previously skipped — circuit now CONNECTED" if spd == "SKIPPED"
                  else "previous speedtest failed")
        log(ip, f"Speedtest {reason} — re-spawning", console=True)
        threading.Thread(target=run_speedtest, args=(ip,), daemon=True).start()

    # Retry or initially trigger policy deploy (5-minute cooldown between attempts).
    # Fires for both FAILED (previous attempt errored) and None (never triggered — e.g.
    # because a site-mate was stuck in WAIT CIRCUIT when config deploy completed).
    with state_lock:
        pol_st      = policy_status.get(ip)
        spd_st      = str(speedtest_status.get(ip, ''))
        vm_st       = vmanage_status.get(ip)
        retry_after = _policy_retry_after.get(ip, 0)
    if (pol_st in ("FAILED", None)
            and vm_st == "DEPLOYED"
            and spd_st.startswith('↓')
            and time.time() >= retry_after):
        log(ip, f"Policy not yet deployed (status={pol_st!r}) — checking trigger conditions", console=True)
        with state_lock:
            _policy_retry_after[ip] = time.time() + 300
        _try_trigger_policy_for_site(ip)

    # Site completion: Dialer1 connected, only G0/1/0 live (provisioning + temp ports down),
    # all pipeline stages done (policy DEPLOYED for every router at the site).
    with state_lock:
        hostname_now = hostnames.get(ip, '')
        info_now     = device_info.get(ip, {})
    if hostname_now:
        sw   = info_now.get('switchports', '')
        circ = info_now.get('circuit', '')
        if ('G0/1/0 UP' in sw
                and 'PROVISIONING' not in sw
                and 'TEMP PORTS IN USE' not in sw
                and circ.startswith('CONNECTED')):
            site_key = re.sub(r'-R\d+$', '', hostname_now)
            with state_lock:
                site_ips   = [sip for sip, hn in hostnames.items()
                              if re.sub(r'-R\d+$', '', hn) == site_key]
                all_done   = (bool(site_ips)
                              and all(policy_status.get(sip) == 'DEPLOYED' for sip in site_ips)
                              and site_key not in _site_complete_notified)
            if all_done:
                with state_lock:
                    _site_complete_notified.add(site_key)
                log(ip, f"Site {site_key} COMPLETE — G0/1/0 live, {circ}, policy deployed for all routers", console=True)
                webex_notify(
                    f"🎉 **{site_key} COMPLETE** — {hostname_now}: G0/1/0 UP, {circ}, "
                    f"no provisioning/temp ports, policy deployed for all routers"
                )


def info_collector_loop(ips: list[str]) -> None:
    """
    Every 60 seconds, collect CONFIG/POLICY/CIRCUIT/SWITCHPORTS for all Up
    devices that are not actively being upgraded.  Collections run in parallel
    (up to 10 concurrent SSH sessions) so the cycle time is bounded by the
    slowest single device rather than the sum of all devices.
    """
    INFO_INTERVAL = 60
    while True:
        cycle_start = time.time()

        with state_lock:
            candidates = [
                ip for ip in ips
                if ip not in in_progress
                and ip not in checking
            ]

        threads = [
            threading.Thread(target=_collect_one, args=(ip,), daemon=True)
            for ip in candidates
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        remaining = INFO_INTERVAL - (time.time() - cycle_start)
        if remaining > 0:
            time.sleep(remaining)


_DASH_WIDTH = 192

# ── Hourly coffee break animation ─────────────────────────────────────────────

_COFFEE_FRAMES: list[tuple[float, str]] = [
    (0.8, """
  ╔══════════════════════════════════════════╗
  ║  ⏰  ONE HOUR IN — COFFEE TIME, BOB!  ⏰║
  ╚══════════════════════════════════════════╝
"""),
    (1.0, """
      O
     /|\\            "Wait... is that the time?!"
     / \\
      BOB
"""),
    (0.9, """
      O   →→→     ___________
     /|\\          |   COFFEE  |   "Oh yes."
     / \\          |_MACHINE___|
      BOB
"""),
    (1.3, """
            ___________
      O     |  ●  BREW |  *BRRRRR*
     /|\\    |__________|
     / \\
                          ♨  ♨  ♨   Brewing...
"""),
    (0.9, """
            ___________
      O     |  ~~~~~   |
     /|\\    |____☕____|   Done!
     / \\
"""),
    (0.8, """
      O  ☕
     /|/ ←←
     / \\              "Got it!"
"""),
    (1.3, """
      O
     /|☕               *S L U R P*
     / \\
                        "Mmmmmmmm..."
"""),
    (1.6, """
      O   ♥ ♥
     \\|/    ♥           "AHHHHHHHH."
      |
     / \\                That. Hit. Different.
"""),
    (5.0, """
  ╔══════════════════════════════════════════╗
  ║  ☕  Recharged. Resuming deployment!    ║
  ╚══════════════════════════════════════════╝
"""),
]


def _coffee_break_animation() -> None:
    """Play the ASCII coffee break — holds print_lock for the full sequence."""
    with print_lock:
        for delay, frame in _COFFEE_FRAMES:
            sys.stdout.write(frame)
            sys.stdout.flush()
            time.sleep(delay)


def _maybe_coffee_break() -> None:
    """Spawn the coffee break thread once per clock hour, on the hour."""
    global _last_coffee_break
    current_hour_ts = datetime.now().replace(minute=0, second=0, microsecond=0).timestamp()
    if current_hour_ts > _last_coffee_break:
        _last_coffee_break = current_hour_ts
        threading.Thread(target=_coffee_break_animation, daemon=True).start()


def print_status(display_order: list[str], now: float) -> None:
    with print_lock:
        print("\033[2J\033[H", end="")  # clear screen, cursor to top
        print(f"{'─'*_DASH_WIDTH}")
        print(f"  {'IP':<20}  {'HOSTNAME':<14}  {'CODE STATUS':<34}  {'CONFIG':<10}  {'SPEED':<20}  {'POLICY':<10}  {'CIRCUIT':<28}  {'SWITCHPORTS':<40}")
        print(f"{'─'*_DASH_WIDTH}")
        for entry in display_order:
            if entry.startswith("#"):
                print(f"  {entry}")
                continue
            ip = entry
            with state_lock:
                if ip in completed:
                    state = completed[ip]
                elif ip in in_progress:
                    elapsed = max(0, int(now - in_progress_since.get(ip, now)))
                    step = in_progress_step.get(ip, "INSTALLING")
                    state = f"{step} ({fmt_elapsed(elapsed)})"
                elif ip in checking:
                    elapsed = max(0, int(now - checking_since.get(ip, now)))
                    state = f"CHECKING CODE VERSION ({fmt_elapsed(elapsed)})"
                elif ip in retry_queue:
                    remaining = max(0, int(retry_queue[ip] - now))
                    if ip in bin_missing:
                        state = f"CODE (.bin file) MISSING (RETRY in {remaining}s)"
                    elif ip in file_copying:
                        state = f"CODE COPYING (RETRY in {remaining}s)"
                    else:
                        state = f"RETRY in {remaining}s"
                else:
                    status = ping_results.get(ip, "Unknown")
                    if status == "Up":
                        held = max(0, int(now - up_since.get(ip, now)))
                        state = f"Up ({held}s / {UP_THRESHOLD}s)"
                    else:
                        state = status
                info = device_info.get(ip, {})
                cfg_device = info.get('config', '')
                vm_st      = vmanage_status.get(ip, '')
                if cfg_device == 'COMPLETE':
                    cfg = 'COMPLETE'
                elif vm_st:
                    cfg = vm_st
                else:
                    cfg = cfg_device
                pol_deploy = policy_status.get(ip, '')
                policy = pol_deploy if pol_deploy else info.get('policy', '')
                if ip in circuit_ping_offline:
                    wan_ip  = wan_ip_cache.get(ip, '')
                    circuit = f"OFFLINE ({wan_ip})" if wan_ip else "OFFLINE"
                else:
                    circuit = info.get('circuit', '')
                switchports = info.get('switchports', '')
                speed = speedtest_status.get(ip, '')
            print(f"  {ip:<20}  {hostnames.get(ip, ''):<14}  {state:<34}  {cfg:<10}  {speed:<20}  {policy:<10}  {circuit:<28}  {switchports:<40}")
        print(f"{'─'*_DASH_WIDTH}  [{ts()}]")

        # Ready-for-switch alerts
        with state_lock:
            rfs_snapshot = dict(ready_for_switch)
        if rfs_snapshot:
            print()
            for _, rfs_host in sorted(rfs_snapshot.items()):
                print(f"  *** {rfs_host} is READY for SWITCH ***")

        # vManage task summary (live from vManage API)
        print()
        if not vmanage_session:
            print("  vManage  not connected")
        else:
            poll_err  = vmanage_poll_status.get("error")
            last_poll = vmanage_poll_status.get("last_poll")
            if poll_err:
                print(f"  vManage  poll error: {poll_err}" + (f"  (last ok: {last_poll})" if last_poll else ""))
            elif not last_poll:
                print("  vManage  connected — waiting for first poll…")
            else:
                with vmanage_tasks_lock:
                    tasks_snapshot = list(vmanage_tasks)
                if not tasks_snapshot:
                    print(f"  vManage  no active tasks  (polled {last_poll})")
                else:
                    for task in tasks_snapshot:
                        name   = task.get("name", "unknown task")
                        status = task.get("status", "")
                        user   = task.get("userSessionUserName", "")
                        parts  = [f"  vManage  {name}  [{status}]"]
                        if user:
                            parts.append(f"user={user}")
                        print("  ".join(parts))

        _now_dt = datetime.now()
        secs_to_coffee = 3600 - (_now_dt.minute * 60 + _now_dt.second)
        if secs_to_coffee <= 300:
            mins, secs = divmod(secs_to_coffee, 60)
            print(f"  ☕  Coffee break in {mins}m {secs:02d}s")

        print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _log_file

    print(r"""
  ____    ___   ____   _   _  _____  _____
 | __ )  / _ \ | __ ) | \ | || ____||_   _|
 |  _ \ | | | ||  _ \ |  \| ||  _|    | |
 | |_) || |_| || |_) || |\  || |___   | |
 |____/  \___/ |____/ |_| \_||_____|  |_|

          SD-WAN Deployment pipeline  --  Coming Online
""")

    log_path = os.path.join(_SCRIPT_DIR, f"code_upgrade_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    _log_file = open(log_path, "w", buffering=1, encoding="utf-8")
    print(f"Logging to: {log_path}")
    print(f"Status file: {STATUS_FILE}\n")

    load_webex_config()
    prompt_credentials()

    if not vmanage_login():
        print("ERROR: vManage login failed — cannot continue without a vManage session.")
        sys.exit(1)

    load_csv_vars()

    ips, display_order = extract_172_ips(PING_FILE)
    if not ips:
        print(f"No 172.x.x.x addresses found in {PING_FILE}")
        sys.exit(1)
    circuit_type.update(parse_circuit_types(PING_FILE))

    # Initialise ping state for all IPs, then overlay saved status
    for ip in ips:
        ping_results[ip] = "Down"
    load_status(ips)

    global _last_coffee_break
    if _last_coffee_break == 0.0:
        _last_coffee_break = datetime.now().replace(minute=0, second=0, microsecond=0).timestamp()

    webex_notify(f"🚀 SD-WAN deployment pipeline started — monitoring **{len(ips)}** device(s)")

    # Resume any config group moves that didn't complete in a previous run
    with state_lock:
        pending_moves = [
            ip for ip in ips
            if completed.get(ip) in PIPELINE_READY
            and vmanage_status.get(ip) not in ("DEPLOYED", "SKIPPED")
        ]
    for ip in pending_moves:
        log(ip, "Resuming vManage config group move from previous run", console=True)
        threading.Thread(target=move_to_final_config_group, args=(ip,), daemon=True).start()

    # Resume any speedtests that didn't complete in a previous run
    with state_lock:
        pending_speedtests = [
            ip for ip in ips
            if vmanage_status.get(ip) == "DEPLOYED"
            and speedtest_status.get(ip) in ("PENDING", "RUNNING", "WAIT CIRCUIT", "SKIPPED", None)
            and ip not in {p for p in pending_moves}
        ]
    for ip in pending_speedtests:
        log(ip, "Resuming speedtest from previous run", console=True)
        threading.Thread(target=run_speedtest, args=(ip,), daemon=True).start()

    # Resume any policy deployments that didn't complete in a previous run
    with state_lock:
        resume_policy_candidates = [
            ip for ip in ips
            if vmanage_status.get(ip) == "DEPLOYED"
            and policy_status.get(ip) not in ("DEPLOYED", "SKIPPED")
        ]
    for ip in resume_policy_candidates:
        _try_trigger_policy_for_site(ip)

    print(f"Found {len(ips)} target IP(s):\n  " + "\n  ".join(ips))
    print(f"\nMonitoring — upgrade triggers after {UP_THRESHOLD}s continuous uptime\n")

    # Start background info collector
    t_info = threading.Thread(target=info_collector_loop, args=(ips,), daemon=True)
    t_info.start()

    if vmanage_session:
        t_vm = threading.Thread(target=vmanage_task_monitor_loop, daemon=True)
        t_vm.start()

    try:
        ping_loop(ips, display_order)
    except KeyboardInterrupt:
        print("\nInterrupted — saving status and exiting…")
        save_status()
    finally:
        _log_file.close()
        print("Log closed. Goodbye.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--coffee":
        _coffee_break_animation()
    else:
        if len(sys.argv) > 1 and sys.argv[1] == "--coffee-soon":
            def _coffee_soon():
                time.sleep(45)
                _coffee_break_animation()
            threading.Thread(target=_coffee_soon, daemon=True).start()
        main()
