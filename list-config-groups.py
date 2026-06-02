import requests
import getpass
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VMANAGE_URL = "https://vmanage-953677893.sdwan.cisco.com:8443"


def get_session(username, password):
    session = requests.Session()
    session.verify = False

    resp = session.post(
        f"{VMANAGE_URL}/j_security_check",
        data={"j_username": username, "j_password": password},
        allow_redirects=False,
    )

    if resp.status_code not in (200, 302) or "JSESSIONID" not in session.cookies:
        raise RuntimeError(f"Login failed (HTTP {resp.status_code})")

    # Fetch CSRF token required for API calls
    token_resp = session.get(f"{VMANAGE_URL}/dataservice/client/token")
    if token_resp.status_code == 200:
        session.headers.update({"X-XSRF-TOKEN": token_resp.text.strip()})

    return session


CONFIG_GROUP_ENDPOINTS = [
    "/dataservice/v1/config-group",
    "/dataservice/template/config-group",
    "/dataservice/configgroup",
]

POLICY_GROUP_ENDPOINTS = [
    "/dataservice/v1/policy-group",
    "/dataservice/template/policy-group",
    "/dataservice/policygroup",
]


def list_groups(session, endpoints, label):
    for path in endpoints:
        resp = session.get(f"{VMANAGE_URL}{path}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code != 404:
            resp.raise_for_status()
    raise RuntimeError(
        f"Could not find {label} endpoint. Tried: {endpoints}"
    )


def print_groups(data, id_keys, name_keys):
    groups = data if isinstance(data, list) else data.get("data", [])
    if not groups:
        return 0
    print(f"  {'ID':<40}  {'Name'}")
    print("  " + "-" * 78)
    for g in groups:
        gid  = next((g[k] for k in id_keys   if k in g), "N/A")
        name = next((g[k] for k in name_keys if k in g), "N/A")
        print(f"  {gid:<40}  {name}")
    return len(groups)


def main():
    print(f"vManage: {VMANAGE_URL}\n")
    username = input("Username: ")
    password = getpass.getpass("Password: ")

    print("\nAuthenticating...")
    session = get_session(username, password)

    print("\n── Configuration Groups ──────────────────────────────────────────────")
    data = list_groups(session, CONFIG_GROUP_ENDPOINTS, "config groups")
    n = print_groups(data, ["id", "configGroupId"], ["name", "configGroupName"])
    print(f"\n  Total: {n} group(s)")

    print("\n── Policy Groups ─────────────────────────────────────────────────────")
    try:
        data = list_groups(session, POLICY_GROUP_ENDPOINTS, "policy groups")
        n = print_groups(data, ["id", "policyGroupId"], ["name", "policyGroupName"])
        print(f"\n  Total: {n} group(s)")
    except RuntimeError as e:
        print(f"  {e}")


if __name__ == "__main__":
    main()
