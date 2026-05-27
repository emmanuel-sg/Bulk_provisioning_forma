"""
Autodesk Construction Cloud (ACC) - User Provisioner
Reads a CSV of users and provisions them to ACC projects:
  - New users are imported with roles, products, company, and access level.
  - Existing users are compared (role, company, access_level) and updated
    only if any field differs. CSV = desired final state for roles.

Usage:
    python acc_provisioner.py <csv_file> [target] [--dry-run] [--add-only]

    --dry-run : Run the full pipeline (CSV parse, project/role lookup,
                comparison) but skip actual import/update API calls.
    --add-only: Never update existing users. If email already exists
                in the target project, skip that row.
    --create-companies: If CSV company is missing in the hub, create it at
                account level and then assign it.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

import requests
import auth
from auth import get_auth_headers, BASE_URL
from acc_hub_projects import get_hubs, get_projects

_PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ROLE_JSON_PATH_TST = os.path.join(_PROJECT_ROOT, "ACC_roles", "role_id_acc_TST.json")
ROLE_JSON_PATH_AG = os.path.join(_PROJECT_ROOT, "ACC_roles", "role_id_acc_AG.json")


REQUEST_TIMEOUT = (5, 30)
MAX_RETRIES = 5
IMPORT_BATCH_SIZE = 50

# ---------------------------------------------------------------------------
# Access-level configurations
# ---------------------------------------------------------------------------

_ALL_PRODUCT_KEYS = [
    "projectAdministration", "docs", "build", "insight",
    "modelCoordination", "designCollaboration", "takeoff",
    "cost", "capitalPlanning", "buildingConnected", "forma",
]

_MEMBER_CORE = {"docs", "build", "insight", "modelCoordination"}
_ADMIN_CORE = {"projectAdministration", "docs", "build", "insight", "modelCoordination"}

MEMBER_PRODUCTS = [
    {"key": k, "access": "member" if k in _MEMBER_CORE else "none"}
    for k in _ALL_PRODUCT_KEYS
]

ADMIN_PRODUCTS = [
    {"key": k, "access": "administrator" if k in _ADMIN_CORE else "none"}
    for k in _ALL_PRODUCT_KEYS
]

MEMBER_ACCESS_LEVELS = {
    "accountAdmin": False,
    "projectAdmin": False,
    "executive": False,
    "accountStandardsAdministrator": False,
}

ADMIN_ACCESS_LEVELS = {
    "accountAdmin": False,
    "projectAdmin": True,
    "executive": False,
    "accountStandardsAdministrator": False,
}


# ---------------------------------------------------------------------------
# ACC API helpers
# ---------------------------------------------------------------------------

def _strip_id(project_id):
    return project_id[2:] if project_id.startswith("b.") else project_id


def _api_get(url, params=None):
    """GET with timeout, retry on 429."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=get_auth_headers(), params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            return None, str(e)

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue
            return None, "rate limited after max retries"

        return resp, None

    return None, "exhausted retries"


def _api_post(url, json_body, extra_headers=None):
    """POST with timeout, retry on 429, 5xx, and transient request errors."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            headers = get_auth_headers()
            if extra_headers:
                headers.update(extra_headers)
            resp = requests.post(url, headers=headers, json=json_body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                wait = min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            return None, str(e)

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "").strip()
            wait = int(retry_after) if retry_after.isdigit() else min(2 ** attempt, 30)
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue
            return None, "rate limited after max retries"

        if resp.status_code in {500, 502, 503, 504}:
            if attempt < MAX_RETRIES:
                wait = min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            return None, f"server error after max retries (HTTP {resp.status_code})"

        return resp, None

    return None, "exhausted retries"


# ---------------------------------------------------------------------------
# Role lookup from JSON file
# ---------------------------------------------------------------------------

def get_role_json_path_for_env(env_name):
    """Return role-map JSON path for the selected environment."""
    env = (env_name or "").strip().upper()
    return ROLE_JSON_PATH_AG if env == "AG" else ROLE_JSON_PATH_TST


def load_role_map_from_json(path):
    """Load role name -> role ID mapping from the JSON file.
    Returns dict of lowercase_name -> role_id.
    """
    if not os.path.exists(path):
        print(f"  Warning: role JSON not found at {path}")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print(f"  Warning: role JSON is invalid/empty at {path}")
        return {}

    role_map = {}
    # Format A: {"roles": [{"name": "...", "id": "..."}]}
    if isinstance(data, dict) and isinstance(data.get("roles"), list):
        for r in data.get("roles", []):
            name = r.get("name", "").strip().lower()
            rid = r.get("id", "")
            if name and rid:
                role_map[name] = rid
        return role_map

    # Format B: {"role_name": "role_id", ...}
    if isinstance(data, dict):
        for name, rid in data.items():
            clean_name = str(name).strip().lower()
            clean_id = str(rid).strip()
            if clean_name and clean_id:
                role_map[clean_name] = clean_id
    return role_map


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------

def build_project_map(hub_id):
    """Fetch all projects and return a dict of lowercase_name -> {id, name}."""
    projects = get_projects(hub_id)
    acc_project_map = {}
    for p in projects:
        name = p.get("attributes", {}).get("name", "")
        pid = p.get("id", "")
        acc_project_map[name.strip().lower()] = {"id": pid, "name": name}
    return acc_project_map


# ---------------------------------------------------------------------------
# Account-level company fetch
# ---------------------------------------------------------------------------

def fetch_account_companies(hub_id):
    """Fetch all companies at the account/hub level.
    Returns acc_company_map: dict of lowercase_company_name -> companyId.
    """
    account_id = _strip_id(hub_id)
    url = f"{BASE_URL}/construction/admin/v1/accounts/{account_id}/companies"

    acc_company_map = {}
    offset = 0
    limit = 100

    while True:
        resp, err = _api_get(url, params={"offset": offset, "limit": limit})
        if err or resp.status_code != 200:
            break

        data = resp.json()
        companies = data if isinstance(data, list) else data.get("results", [])

        for c in companies:
            name = c.get("name", "").strip().lower()
            cid = c.get("id", "")
            if name and cid:
                acc_company_map[name] = cid

        if isinstance(data, dict):
            total = data.get("pagination", {}).get("totalResults", 0)
            if offset + limit >= total:
                break
        else:
            break

        offset += limit

    return acc_company_map


# ---------------------------------------------------------------------------
# Company creation (account-level)
# ---------------------------------------------------------------------------

def create_account_company(hub_id, company_name):
    """Create an account-level company and return (company_id, error_message)."""
    account_id = _strip_id(hub_id)
    # Company creation is handled by the ACC "HQ" API, not the construction admin API.
    # Ref: APS ACC docs (Companies) -> POST /hq/v1/accounts/:account_id/companies
    url = f"{BASE_URL}/hq/v1/accounts/{account_id}/companies"

    # The HQ API requires at least 'name' and 'trade'. Use a neutral default for trade.
    body = {"name": (company_name or "").strip(), "trade": "Other"}
    if not body["name"]:
        return "", "empty company name"

    extra = {"x-user-id": auth.USER_ID} if auth.USER_ID else None
    resp, err = _api_post(url, body, extra_headers=extra)
    if err:
        return "", err

    # ACC APIs sometimes return 200/201 with the created entity, sometimes 409 if it exists.
    if resp.status_code in (200, 201):
        try:
            data = resp.json()
        except ValueError:
            data = {}
        cid = ""
        if isinstance(data, dict):
            cid = str(data.get("id", "")).strip()
        return cid, "" if cid else "company created but response missing id"

    if resp.status_code == 409:
        return "", "company already exists (409)"

    return "", f"HTTP {resp.status_code}: {resp.text[:300]}"


# ---------------------------------------------------------------------------
# Project users fetch (membership check)
# ---------------------------------------------------------------------------

def fetch_project_users(project_id):
    """Fetch all users for a project.
    Returns acc_user_map: dict of lowercase_email -> user detail dict
    containing id, roleIds, companyId, products, and accessLevels.
    """
    clean_id = _strip_id(project_id)
    url = f"{BASE_URL}/construction/admin/v1/projects/{clean_id}/users"

    acc_user_map = {}
    offset = 0
    limit = 100

    while True:
        resp, err = _api_get(url, params={"offset": offset, "limit": limit})
        if err or resp.status_code != 200:
            break

        data = resp.json()
        users = data if isinstance(data, list) else data.get("results", [])

        for u in users:
            email = u.get("email", "").strip().lower()
            if email:
                acc_user_map[email] = {
                    "id": u.get("id", ""),
                    "firstName": (u.get("firstName") or u.get("first_name") or "").strip(),
                    "lastName": (u.get("lastName") or u.get("last_name") or "").strip(),
                    "roleIds": u.get("roleIds", []),
                    "companyId": u.get("companyId", ""),
                    "products": u.get("products", []),
                    "accessLevels": u.get("accessLevels", {}),
                }

        if isinstance(data, dict):
            total = data.get("pagination", {}).get("totalResults", 0)
            if offset + limit >= total:
                break
        else:
            break

        offset += limit

    return acc_user_map


# ---------------------------------------------------------------------------
# PATCH helper
# ---------------------------------------------------------------------------

def _api_patch(url, json_body, extra_headers=None):
    """PATCH with timeout, retry on 429."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            headers = get_auth_headers()
            if extra_headers:
                headers.update(extra_headers)
            resp = requests.patch(url, headers=headers, json=json_body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            return None, str(e)

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue
            return None, "rate limited after max retries"

        return resp, None

    return None, "exhausted retries"


# ---------------------------------------------------------------------------
# Detect differences between existing ACC user and desired CSV state
# ---------------------------------------------------------------------------
# existing_user -> ACC data
# desired_role_ids -> CSV roles → resolved to IDs via JSON
# desired_company_id -> CSV company → resolved to ID via ACC companies
# desired_access_level -> CSV raw string ("Member" or "Administrator")
def _detect_changes(
    existing_user,
    desired_first_name,
    desired_last_name,
    desired_role_ids,
    desired_company_id,
    desired_access_level,
):
    """Compare existing ACC user with CSV desired state.
    Returns (changes_dict, reasons_list).
    changes_dict contains only the fields that need to be PATCHed.
    reasons_list contains human-readable descriptions of what changed.
    """
    changes = {}
    reasons = []

    # Name: compare first/last name if CSV provides values.
    desired_first = (desired_first_name or "").strip()
    desired_last = (desired_last_name or "").strip()
    existing_first = (existing_user.get("firstName") or "").strip()
    existing_last = (existing_user.get("lastName") or "").strip()

    if desired_first and desired_first != existing_first:
        changes["firstName"] = desired_first
        reasons.append("first_name changed")
    if desired_last and desired_last != existing_last:
        changes["lastName"] = desired_last
        reasons.append("last_name changed")

    # Roles: only compare if CSV specifies roles (non-empty)
    if desired_role_ids:
        existing_roles = set(existing_user.get("roleIds", []))
        desired_roles = set(desired_role_ids)
        if existing_roles != desired_roles:
            changes["roleIds"] = desired_role_ids
            reasons.append("role changed")

    # Company: only compare if CSV company resolved to an ID
    if desired_company_id:
        existing_company = existing_user.get("companyId", "")
        if existing_company != desired_company_id:
            changes["companyId"] = desired_company_id
            reasons.append("company changed")

    # Access level: always compare (CSV always has Member or Administrator)
    is_admin = _is_admin(desired_access_level)
    existing_is_admin = existing_user.get("accessLevels", {}).get("projectAdmin", False)
    if is_admin != existing_is_admin:
        changes["products"] = list(ADMIN_PRODUCTS if is_admin else MEMBER_PRODUCTS)
        reasons.append("access_level changed")

    return changes, reasons


# ---------------------------------------------------------------------------
# User update (PATCH)
# ---------------------------------------------------------------------------

def update_user_in_project(project_id, user_id, changes):
    """PATCH a user's fields in a project.
    Returns (success: bool, error_message: str).
    """
    clean_id = _strip_id(project_id)
    url = f"{BASE_URL}/construction/admin/v1/projects/{clean_id}/users/{user_id}"

    extra = {"x-user-id": auth.USER_ID} if auth.USER_ID else None
    resp, err = _api_patch(url, changes, extra_headers=extra)
    if err:
        return False, err

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"

    return True, ""


# ---------------------------------------------------------------------------
# User import
# ---------------------------------------------------------------------------

def _is_admin(access_level):
    """Check if an access_level string means Administrator."""
    return "administrator" in access_level.strip().lower()


def import_user_to_project(project_id, email, role_ids, access_level, company_id=None):
    """
    Import (invite) a user to a project with roles, products, access level,
    and company. Returns (success: bool, error_message: str).
    """
    clean_id = _strip_id(project_id)
    url = f"{BASE_URL}/construction/admin/v1/projects/{clean_id}/users:import"

    is_admin = _is_admin(access_level)
    products = list(ADMIN_PRODUCTS if is_admin else MEMBER_PRODUCTS)
    access_levels = dict(ADMIN_ACCESS_LEVELS if is_admin else MEMBER_ACCESS_LEVELS)

    user_payload = {
        "email": email.strip().lower(),
        "products": products,
    }
    if role_ids:
        user_payload["roleIds"] = role_ids
    if company_id:
        user_payload["companyId"] = company_id

    body = {"users": [user_payload]}

    extra = {"x-user-id": auth.USER_ID} if auth.USER_ID else None #: The users:import endpoint doesn't accept a plain 2-legged token. The x-user-id header tells the API "act on behalf of this admin user." Without it, the API returns 401 Unauthorized — which is exactly what happened before we added it.
    resp, err = _api_post(url, body, extra_headers=extra)
    if err:
        return False, err

    if resp.status_code not in (200, 201, 202):
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"

    result = resp.json()
    if isinstance(result, dict):
        failures = result.get("failure", []) #if it is a failure, it will be in the failure array
        if failures:
            reason = failures[0].get("errors", [{}])[0].get("title", "unknown error")
            return False, reason

    return True, ""


def _build_import_user_payload(email, role_ids, access_level, company_id=None):
    """Build one users:import payload item."""
    is_admin = _is_admin(access_level)
    user_payload = {
        "email": email.strip().lower(),
        "products": list(ADMIN_PRODUCTS if is_admin else MEMBER_PRODUCTS),
    }
    if role_ids:
        user_payload["roleIds"] = role_ids
    if company_id:
        user_payload["companyId"] = company_id
    return user_payload


def import_users_batch_to_project(project_id, user_payloads):
    """Import a batch of users into one project.
    Returns (successful_emails_set, failure_reason_by_email_dict, batch_error_message).
    """
    clean_id = _strip_id(project_id)
    url = f"{BASE_URL}/construction/admin/v1/projects/{clean_id}/users:import"

    body = {"users": user_payloads}
    extra = {"x-user-id": auth.USER_ID} if auth.USER_ID else None
    resp, err = _api_post(url, body, extra_headers=extra)
    if err:
        return set(), {}, err

    if resp.status_code not in (200, 201, 202):
        return set(), {}, f"HTTP {resp.status_code}: {resp.text[:300]}"

    try:
        result = resp.json()
    except ValueError:
        result = {}

    requested_emails = {u.get("email", "").strip().lower() for u in user_payloads if u.get("email")}
    failure_map = {}
    success_set = set()

    if isinstance(result, dict):
        for f in result.get("failure", []):
            email = str(f.get("email", "")).strip().lower()
            reason = "unknown error"
            errors = f.get("errors", [])
            if errors and isinstance(errors, list):
                first_err = errors[0] if isinstance(errors[0], dict) else {}
                reason = first_err.get("title") or first_err.get("detail") or reason
            if email:
                failure_map[email] = reason

        for s in result.get("success", []):
            if isinstance(s, dict):
                email = str(s.get("email", "")).strip().lower()
                if email:
                    success_set.add(email)

    # If API doesn't explicitly return success list, infer success from requested - failures.
    if not success_set:
        success_set = requested_emails.difference(set(failure_map.keys()))

    return success_set, failure_map, ""


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_csv(path):
    """Parse the input CSV into a list of row dicts."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for record in reader:
            roles_raw = record.get("roles", "").strip()
            roles = [r.strip() for r in roles_raw.split(";") if r.strip() and r.strip() != "N/A"]

            rows.append({
                "first_name": record.get("first_name", "").strip(),
                "last_name": record.get("last_name", "").strip(),
                "email": record.get("email", "").strip().lower(),
                "project_name": record.get("project_name", "").strip(),
                "roles": roles,
                "company": record.get("company", "").strip(),
                "access_level": record.get("access_level", "").strip(),
            })
    return rows


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def print_summary(added, updated, skipped, failed):
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    print(f"\n  Added: {len(added)}")
    for r in added:
        print(f"    + {r['email']} -> {r['project_name']} (roles={r['roles']}, level={r['level']})")

    print(f"\n  Updated: {len(updated)}")
    for r in updated:
        print(f"    ~ {r['email']} -> {r['project_name']} ({r['reason']})")

    print(f"\n  Skipped: {len(skipped)}")
    for r in skipped:
        print(f"    - {r['email']} -> {r['project_name']}")

    print(f"\n  Failed: {len(failed)}")
    for r in failed:
        print(f"    x {r['email']} -> {r['project_name']} ({r['reason']})")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser( description="Provision ACC users from a CSV file.")
    parser.add_argument("csv_file", help="Path to the input CSV file")
    parser.add_argument("target", nargs="?", default=None, help="Environment (TST/AG) or hub key (e.g. Swissgrid_TST)")
    #action="store_true" -- if the user includes --dry-run, set it to True. If they don't, it defaults to False
    #python acc_provisioner.py data.csv --dry-run    # dry_run = True  → only simulates, no API calls
    parser.add_argument("--dry-run", action="store_true",help="Validate everything but skip the actual user import API call",) 
    parser.add_argument(
        "--add-only",
        action="store_true",
        help="Only add missing users. If user email already exists in project, skip (no update).",
    )
    parser.add_argument(
        "--create-companies",
        action="store_true",
        help="If a CSV company is missing in the hub, create it at account level and assign it.",
    )
    args, extras = parser.parse_known_args()
    if extras:
        if args.target is None and len(extras) == 1:
            args.target = extras[0]
        else:
            parser.error(f"unrecognized arguments: {' '.join(extras)}")

    # Usage examples:
    #   python src\acc_provisioner.py DATA_user_import\FAKE_one_user.csv                    -> current auth env default hub
    #   python src\acc_provisioner.py DATA_user_import\FAKE_one_user.csv TST                -> TST hub
    #   python src\acc_provisioner.py DATA_user_import\FAKE_one_user.csv AG                 -> AG hub
    #   python src\acc_provisioner.py DATA_user_import\FAKE_one_user.csv --dry-run TST      -> TST dry-run
    #   python src\acc_provisioner.py DATA_user_import\FAKE_one_user.csv --dry-run AG       -> AG dry-run
    #   python src\acc_provisioner.py --help                                                 -> show all options


    csv_path = args.csv_file
    target = (args.target or "").strip()
    dry_run = args.dry_run
    add_only = args.add_only
    create_companies = args.create_companies

    if not target:
        env = auth.ACC_ENV
        hub_key = auth.HUB_KEY
    elif target.upper() in {"TST", "AG"}:
        env = target.upper()
        hub_key = f"Swissgrid_{env}"
    elif target in {"Swissgrid_TST", "Swissgrid_AG"}:
        hub_key = target
        env = target.split("_")[-1].upper()
    else:
        print("Error: target must be TST, AG, Swissgrid_TST, or Swissgrid_AG")
        sys.exit(1)

    auth.set_acc_env(env)

    print(f"\n  Environment: {auth.ACC_ENV} (hub: {hub_key})")

    if dry_run:
        print("  *** DRY-RUN MODE — no users will actually be imported ***")
    if add_only:
        print("  *** ADD-ONLY MODE — existing users are always skipped (by email) ***")
    if create_companies:
        print("  *** CREATE-COMPANIES MODE — missing companies will be created ***")

    hub_id = os.getenv(hub_key, "")
    if not hub_id:
        print(f"Error: Hub key '{hub_key}' not found in .env")
        sys.exit(1)

    # --- Parse CSV ---
    print(f"\nLoading CSV: {csv_path}")
    rows = parse_csv(csv_path)
    print(f"  {len(rows)} rows loaded")

    # --- Load role map from env-specific JSON ---
    role_json_path = get_role_json_path_for_env(auth.ACC_ENV)
    print(f"\nLoading role map from: {role_json_path}")
    role_map = load_role_map_from_json(role_json_path)
    print(f"  {len(role_map)} roles loaded")

    # --- Fetch projects and build name -> ID map ---
    print(f"\nFetching projects for hub: {hub_key} ({hub_id})...")
    acc_project_map = build_project_map(hub_id)
    print(f"  {len(acc_project_map)} projects found")

    # --- Fetch companies at account/hub level ---
    print(f"\nFetching companies for account...")
    acc_company_map = fetch_account_companies(hub_id)
    print(f"  {len(acc_company_map)} companies found")

    # --- Pre-fetch project users per project ---
    #
    # NOTE: This can be very slow for CSVs targeting many projects (N000 exports).
    # In add-only mode we can skip prefetch entirely and let the import endpoint
    # tell us if the user already exists; we'll then classify that as "skipped".
    acc_member_cache = {}  # project_id -> acc_user_map (email -> user details)

    if add_only:
        print("\nSkipping pre-fetch of project users (add-only mode).")
    else:
        unique_projects = set()
        for row in rows:
            unique_projects.add(row["project_name"].strip().lower())

        print(f"\nPre-fetching users for {len(unique_projects)} unique projects...")
        for proj_name in unique_projects:
            proj = acc_project_map.get(proj_name)
            if not proj:
                continue
            pid = proj["id"]
            if pid not in acc_member_cache:
                print(f"  Fetching users for: {proj['name']}...")
                acc_user_map = fetch_project_users(pid)
                acc_member_cache[pid] = acc_user_map
                print(f"    {len(acc_user_map)} users")

    # --- Process rows ---
    added = []
    updated = []
    skipped = []
    failed = []
    seen = set()

    pending_adds_by_project = {}  # project_id -> list of pending add dicts

    total = len(rows)
    print(f"\nProcessing {total} rows...\n")

    for i, row in enumerate(rows, 1):
        email = row["email"]
        project_name = row["project_name"]
        label = f"[{i}/{total}] {email} -> {project_name}"

        dedup_key = (email, project_name.lower())
        if dedup_key in seen:
            print(f"  {label} ... SKIPPED (duplicate row)")
            skipped.append({"email": email, "project_name": project_name, "reason": "duplicate row"})
            continue
        seen.add(dedup_key)

        # 1. Resolve project
        proj = acc_project_map.get(project_name.strip().lower())
        if not proj:
            print(f"  {label} ... FAILED (project not found)")
            failed.append({"email": email, "project_name": project_name, "reason": "project not found"})
            continue

        project_id = proj["id"]
        acc_user_map = acc_member_cache.get(project_id, {})

        # 2. Resolve roles from JSON (CSV has name, JSON maps name -> id)
        role_ids = []
        for role_name in row["roles"]:
            rid = role_map.get(role_name.strip().lower())
            if rid:
                role_ids.append(rid)

        unresolved_roles = [
            r for r in row["roles"]
            if r.strip().lower() not in role_map
        ]
        if unresolved_roles:
            unresolved_str = ", ".join(unresolved_roles)
            reason = f"role does not exist in the HUB, user skipped ({unresolved_str})"
            print(f"  {label} ... SKIPPED ({reason})")
            skipped.append({"email": email, "project_name": project_name, "reason": reason})
            continue

        # 3. Resolve companyId
        company = row["company"]
        company_id = acc_company_map.get(company.strip().lower()) if company else None
        if company and not company_id:
            print(f"    (!) Company not found: {company}")
            if create_companies:
                if dry_run:
                    print(f"    (dry-run) Would create company at account level: {company}")
                else:
                    created_id, create_err = create_account_company(hub_id, company)
                    if create_err:
                        print(f"    (!) Company create failed: {create_err}")
                    # Re-fetch companies to pick up the new one (or pick up a pre-existing one if 409).
                    acc_company_map = fetch_account_companies(hub_id)
                    company_id = acc_company_map.get(company.strip().lower()) if company else None
                    if company_id:
                        print(f"    (+) Company resolved after create: {company} -> {company_id}")

        # 4. Check if user already exists in the project
        if email in acc_user_map:
            if add_only:
                reason = "already exists in project (add-only mode)"
                print(f"  {label} ... SKIPPED ({reason})")
                skipped.append({"email": email, "project_name": project_name, "reason": reason})
                continue

            existing_user = acc_user_map[email]
            changes, reasons = _detect_changes(
                existing_user,
                row["first_name"],
                row["last_name"],
                role_ids,
                company_id,
                row["access_level"],
            )

            if not changes:
                print(f"  {label} ... SKIPPED (no changes needed)")
                skipped.append({"email": email, "project_name": project_name, "reason": "no changes needed"})
                continue # in case there are no changes, we skip the user

            reason_str = ", ".join(reasons)

            if dry_run:
                print(f"  {label} ... WOULD UPDATE ({reason_str})")
                updated.append({"email": email, "project_name": project_name, "reason": reason_str})
            else:
                success, err_msg = update_user_in_project(
                    project_id, existing_user["id"], changes
                )
                if success:
                    print(f"  {label} ... UPDATED ({reason_str})")
                    updated.append({"email": email, "project_name": project_name, "reason": reason_str})
                else:
                    print(f"  {label} ... FAILED ({err_msg})")
                    failed.append({"email": email, "project_name": project_name, "reason": err_msg})
                time.sleep(0.3)
            continue

        # 5. User not in project -> Import (or simulate in dry-run)
        level = "Administrator" if _is_admin(row["access_level"]) else "Member"
        role_names = ";".join(row["roles"]) if row["roles"] else "N/A"

        if dry_run:
            print(f"  {label} ... WOULD ADD (level={level}, roles={role_names}, company={company_id or 'N/A'})")
            added.append({"email": email, "project_name": project_name, "roles": role_names, "level": level})
        else:
            user_payload = _build_import_user_payload(email, role_ids, row["access_level"], company_id)
            pending_adds_by_project.setdefault(project_id, []).append(
                {
                    "email": email,
                    "project_name": project_name,
                    "roles": role_names,
                    "level": level,
                    "payload": user_payload,
                }
            )
            print(f"  {label} ... QUEUED FOR ADD (batch)")

    # --- Execute queued add imports in batches (real run only) ---
    if not dry_run and pending_adds_by_project:
        print(f"\nImporting queued users in batches of {IMPORT_BATCH_SIZE}...")
        for project_id, pending_entries in pending_adds_by_project.items():
            project_total = len(pending_entries)
            for start in range(0, project_total, IMPORT_BATCH_SIZE):
                batch_entries = pending_entries[start:start + IMPORT_BATCH_SIZE]
                batch_payloads = [e["payload"] for e in batch_entries]
                success_set, failure_map, batch_err = import_users_batch_to_project(project_id, batch_payloads)

                if batch_err:
                    for e in batch_entries:
                        failed.append(
                            {
                                "email": e["email"],
                                "project_name": e["project_name"],
                                "reason": batch_err,
                            }
                        )
                    continue

                for e in batch_entries:
                    email = e["email"]
                    if email in failure_map:
                        reason = failure_map[email]
                        # In add-only mode, treat "already in project" responses as skips.
                        if add_only and any(s in (reason or "").lower() for s in ["already", "exists", "member"]):
                            skipped.append(
                                {
                                    "email": email,
                                    "project_name": e["project_name"],
                                    "reason": f"already exists in project (add-only mode): {reason}",
                                }
                            )
                            continue

                        failed.append(
                            {
                                "email": email,
                                "project_name": e["project_name"],
                                "reason": reason,
                            }
                        )
                    elif email in success_set:
                        added.append(
                            {
                                "email": email,
                                "project_name": e["project_name"],
                                "roles": e["roles"],
                                "level": e["level"],
                            }
                        )
                        acc_member_cache.get(project_id, {})[email] = {}
                    else:
                        failed.append(
                            {
                                "email": email,
                                "project_name": e["project_name"],
                                "reason": "batch result missing status for user",
                            }
                        )

    # --- Summary ---
    print_summary(added, updated, skipped, failed)
    if dry_run:
        print("  (dry-run: nothing was actually changed)\n")

    # --- Write report CSV ---
    mode_label = "dryrun" if dry_run else "report"
    report_dir = os.path.join(_PROJECT_ROOT, "_Reports")
    os.makedirs(report_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(report_dir, f"provisioner_{mode_label}_{timestamp}.csv")
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "project_name", "status", "reason"])
        for r in added:
            status = "would_add" if dry_run else "added"
            writer.writerow([r["email"], r["project_name"], status, ""])
        for r in updated:
            status = "would_update" if dry_run else "updated"
            writer.writerow([r["email"], r["project_name"], status, r["reason"]])
        for r in skipped:
            writer.writerow([r["email"], r["project_name"], "skipped", r.get("reason", "")])
        for r in failed:
            writer.writerow([r["email"], r["project_name"], "failed", r["reason"]])

    print(f"\n  Report saved: {os.path.abspath(report_path)}")


if __name__ == "__main__":
    main()
