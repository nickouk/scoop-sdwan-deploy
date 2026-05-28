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
SSH_TIMEOUT     = 30          # seconds for SSH connect/read
CMD_TIMEOUT     = 600         # seconds to wait for long-running commands
REBOOT_TIMEOUT  = 600         # seconds to wait for device to come back after reboot
REBOOT_POLL     = 10          # seconds between reboot connectivity checks

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(_SCRIPT_DIR, "upgrade_status.json")

VMANAGE_BASE_URL     = "https://vmanage-953677893.sdwan.cisco.com:8443"
VMANAGE_ONBOARD_UUID = "8cd9fc8a-a552-41be-95f5-42fc4bcc6ad9"
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
csv_vars:             dict[str, dict]  = {}   # system-ip -> full variable row from CSV
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
                vmanage_status[ip] = vm_st
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
    except Exception as exc:
        log(ip, f"CIRCUIT collect error: {type(exc).__name__}: {exc}")

    with state_lock:
        device_info[ip] = info
    save_status()
    return client


def run_command(client: paramiko.SSHClient, ip: str, command: str,
                timeout: int = CMD_TIMEOUT) -> tuple[paramiko.SSHClient, str]:
    """
    Execute a command and return (client, output).
    If the SSH transport has dropped, reconnects automatically before running.
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


def _vmanage_build_variable_list(ip: str, target_group: str, uuid: str, csv_row: dict) -> list[dict]:
    """
    Build [{name, value}] for PUT /v1/config-group/{id}/device/variables.

    GETs the full variable list from an existing device in the group to learn
    variable names and their correct Python types.  Falls back to smart
    inference if no template device is available.
    """
    reverse_col_map = {vm: csv_col for csv_col, vm in VMANAGE_COL_MAP.items()}

    type_map: dict[str, type] = {}
    all_var_names: list[str] = []

    try:
        resp = vmanage_session.get(
            f"{VMANAGE_BASE_URL}/dataservice/v1/config-group/{target_group}/device/variables",
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
            else:
                converted = raw
        except (ValueError, TypeError):
            converted = raw

        var_list.append({"name": vname, "value": converted})

    return var_list


def move_to_final_config_group(ip: str) -> None:
    """Move device from onboarding to final config group. Runs in its own thread."""
    with state_lock:
        upgrade_done   = completed.get(ip) == "UPGRADE COMPLETE"
        config_status  = device_info.get(ip, {}).get('config', '?')
        hostname       = hostnames.get(ip, "")

    if not upgrade_done:
        log(ip, "vManage: skipping config group move — upgrade not complete", console=True)
        return

    if config_status == 'COMPLETE':
        log(ip, "vManage: skipping config group move — device already on final config group", console=True)
        return

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
            log(ip, "vManage: CONFIG is now COMPLETE — device already on final group, skipping move", console=True)
            return

    if not vmanage_session:
        log(ip, "vManage: no session — skipping config group move", console=True)
        with state_lock:
            vmanage_status[ip] = "SKIPPED"
        save_status()
        return

    with state_lock:
        vmanage_status[ip] = "WAITING"

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
        with state_lock:
            vmanage_status[ip] = "DEPLOYED"

    except requests.HTTPError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        log(ip, f"vManage: config group deploy FAILED: {exc}  body={body!r}", console=True)
        with state_lock:
            vmanage_status[ip] = "FAILED"
        if disassociated:
            _vmanage_rollback_to_onboard(ip, uuid)
    except Exception as exc:
        log(ip, f"vManage: config group deploy FAILED: {exc}", console=True)
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


# ── Upgrade task (runs in its own thread) ─────────────────────────────────────

def upgrade_device(ip: str) -> None:
    """Full upgrade workflow for a single device."""
    log(ip, "=== Starting upgrade workflow ===")

    def fail(reason: str) -> None:
        log(ip, f"FAILED: {reason} — queuing for retry in 5 minutes", console=True)
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
            save_status()
            client.close()
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

        if is_active and not is_default:
            # Active but set-default / remove not yet done — jump to step 7
            log(ip, f"{TARGET_VERSION} is active but not default — resuming at set-default", console=True)

        elif is_installed and not is_active:
            # Installed but activate not yet done — jump to step 5
            log(ip, f"{TARGET_VERSION} is installed but not active — resuming at activate", console=True)

            # ── Step 5: Activate (triggers reboot) ────────────────────────────
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
                log(ip, f"Install file not found — re-checking software table…")
                client, sw2 = run_command(client, ip, "show sdwan software")
                log(ip, f"show sdwan software output:\n{sw2.strip()}")
                versions2 = parse_sdwan_versions(sw2)
                if TARGET_VERSION not in versions2:
                    client.close()
                    with state_lock:
                        bin_missing.add(ip)
                    fail(f"Install file not found and {TARGET_VERSION} not installed: {INSTALL_FILE}")
                    return
                log(ip, f"{TARGET_VERSION} present — bin consumed by prior run, skipping to activate")
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
                client, install_output = run_command(client, ip, install_cmd, timeout=CMD_TIMEOUT)
                log(ip, f"Install output:\n{install_output.strip()}")
                if "error" in install_output.lower() or "failed" in install_output.lower():
                    client.close()
                    fail("Install command reported an error")
                    return

            # ── Step 5: Activate (triggers reboot) ────────────────────────────
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
        time.sleep(PING_INTERVAL)


def _collect_one(ip: str) -> None:
    """Collect device info for a single IP; runs in its own thread."""
    with _info_sem:
        with state_lock:
            if ping_results.get(ip, "Down") == "Down":
                return
        try:
            client = ssh_connect(ip)
            if ip not in hostnames:
                client = discover_hostname(client, ip)
            collect_device_info(client, ip)
            client.close()
        except Exception as exc:
            log(ip, f"Info collect SSH error: {type(exc).__name__}: {exc}")


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


_DASH_WIDTH = 170

def print_status(display_order: list[str], now: float) -> None:
    with print_lock:
        print("\033[2J\033[H", end="")  # clear screen, cursor to top
        print(f"{'─'*_DASH_WIDTH}")
        print(f"  {'IP':<20}  {'HOSTNAME':<14}  {'CODE STATUS':<34}  {'CONFIG':<10}  {'POLICY':<10}  {'CIRCUIT':<28}  {'SWITCHPORTS':<40}")
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
                policy  = info.get('policy',  '')
                if ip in circuit_ping_offline:
                    wan_ip  = wan_ip_cache.get(ip, '')
                    circuit = f"OFFLINE ({wan_ip})" if wan_ip else "OFFLINE"
                else:
                    circuit = info.get('circuit', '')
                switchports = info.get('switchports', '')
            print(f"  {ip:<20}  {hostnames.get(ip, ''):<14}  {state:<34}  {cfg:<10}  {policy:<10}  {circuit:<28}  {switchports:<40}")
        print(f"{'─'*_DASH_WIDTH}  [{ts()}]")

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

    prompt_credentials()

    if not vmanage_login():
        print("WARNING: vManage login failed — config group moves will be skipped\n")

    load_csv_vars()

    ips, display_order = extract_172_ips(PING_FILE)
    if not ips:
        print(f"No 172.x.x.x addresses found in {PING_FILE}")
        sys.exit(1)

    # Initialise ping state for all IPs, then overlay saved status
    for ip in ips:
        ping_results[ip] = "Down"
    load_status(ips)

    # Resume any config group moves that didn't complete in a previous run
    with state_lock:
        pending_moves = [
            ip for ip in ips
            if completed.get(ip) == "UPGRADE COMPLETE"
            and vmanage_status.get(ip) not in ("MOVED", "SKIPPED")
        ]
    for ip in pending_moves:
        log(ip, "Resuming vManage config group move from previous run", console=True)
        threading.Thread(target=move_to_final_config_group, args=(ip,), daemon=True).start()

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
    main()
