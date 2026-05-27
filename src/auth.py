# Autodesk APS (formerly Forge) Authentication Configuration
# Automatically fetches and refreshes OAuth2 tokens using client credentials.
#
# Environment selection:
#   ACC_ENV = "TST"  (default)  ->  uses APS_CLIENT_ID_TST / APS_CLIENT_SECRET_TST / Swissgrid_TST
#   ACC_ENV = "AG"              ->  uses APS_CLIENT_ID_AG  / APS_CLIENT_SECRET_AG  / Swissgrid_AG

import os
import time
import requests
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)

# --- Environment selection ---
# You can override by setting ACC_ENV in .env to TST or AG.
DEFAULT_ACC_ENV = os.getenv("ACC_ENV", "TST").strip().upper()
if DEFAULT_ACC_ENV not in {"TST", "AG"}:
    DEFAULT_ACC_ENV = "TST"

ACC_ENV = DEFAULT_ACC_ENV
CLIENT_ID = ""
CLIENT_SECRET = ""
USER_ID = ""
HUB_KEY = ""
HUB_ID = ""

BASE_URL = "https://developer.api.autodesk.com"
TOKEN_URL = f"{BASE_URL}/authentication/v2/token"

_token_cache_by_env = {}


def _current_cache():
    """Return the token cache for the currently selected environment."""
    if ACC_ENV not in _token_cache_by_env:
        _token_cache_by_env[ACC_ENV] = {"access_token": None, "expires_at": 0}
    return _token_cache_by_env[ACC_ENV]


def set_acc_env(env_name):
    """Switch active auth environment at runtime (TST or AG)."""
    global ACC_ENV, CLIENT_ID, CLIENT_SECRET, USER_ID, HUB_KEY, HUB_ID

    env = (env_name or "").strip().upper()
    if env not in {"TST", "AG"}:
        raise ValueError("Invalid environment. Use 'TST' or 'AG'.")

    ACC_ENV = env
    CLIENT_ID = os.getenv(f"APS_CLIENT_ID_{ACC_ENV}", "")
    CLIENT_SECRET = os.getenv(f"APS_CLIENT_SECRET_{ACC_ENV}", "")
    USER_ID = os.getenv(f"APS_USER_ID_{ACC_ENV}", "")
    HUB_KEY = f"Swissgrid_{ACC_ENV}"
    HUB_ID = os.getenv(HUB_KEY, "")
    _current_cache()  # Ensure cache exists for this env.


# Initialize env-specific globals once at import time.
set_acc_env(DEFAULT_ACC_ENV)


def _fetch_new_token():
    """Request a new 2-legged OAuth2 token using client credentials."""
    print(f"  [Auth] Requesting new access token (env={ACC_ENV})...")

    if not CLIENT_ID or not CLIENT_SECRET:
        raise Exception(
            f"Missing credentials: APS_CLIENT_ID_{ACC_ENV} or APS_CLIENT_SECRET_{ACC_ENV} not set in .env"
        )

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            # data:write is required for creating Docs folders (Data Management API).
            "scope": "data:read data:write account:read account:write",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if response.status_code != 200:
        raise Exception(f"Failed to get token: {response.status_code} - {response.text}")

    token_data = response.json()
    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 3600)

    cache = _current_cache()
    cache["access_token"] = access_token
    cache["expires_at"] = time.time() + expires_in - 60

    print(f"  [Auth] Token acquired (expires in {expires_in // 60} minutes)")
    return access_token


def get_access_token():
    """Return a valid access token, refreshing automatically if expired."""
    cache = _current_cache()
    if cache["access_token"] and time.time() < cache["expires_at"]:
        return cache["access_token"]
    return _fetch_new_token()


def get_auth_headers():
    """Return the authorization headers for API calls."""
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }
