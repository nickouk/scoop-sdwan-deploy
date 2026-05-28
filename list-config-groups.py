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


def list_config_groups(session):
    for path in CONFIG_GROUP_ENDPOINTS:
        resp = session.get(f"{VMANAGE_URL}{path}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code != 404:
            resp.raise_for_status()
    raise RuntimeError(
        f"Could not find config groups endpoint. Tried: {CONFIG_GROUP_ENDPOINTS}"
    )


def main():
    print(f"vManage: {VMANAGE_URL}\n")
    username = input("Username: ")
    password = getpass.getpass("Password: ")

    print("\nAuthenticating...")
    session = get_session(username, password)

    print("Fetching configuration groups...\n")
    data = list_config_groups(session)

    groups = data if isinstance(data, list) else data.get("data", [])

    if not groups:
        print("No configuration groups found.")
        return

    print(f"{'ID':<40}  {'Name'}")
    print("-" * 80)
    for g in groups:
        gid = g.get("id", g.get("configGroupId", "N/A"))
        name = g.get("name", g.get("configGroupName", "N/A"))
        print(f"{gid:<40}  {name}")

    print(f"\nTotal: {len(groups)} group(s)")


if __name__ == "__main__":
    main()
