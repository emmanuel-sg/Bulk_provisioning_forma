"""
ACC Docs - Create folder structure from a .7z archive.

The .7z is expected to contain a nested folder tree (optionally with files).
This script reads the archive paths, derives all folder paths, and creates
those folders under the project's "Project Files" top folder.

Usage:
  python src\\acc_docs_folder_structure.py project_structure.7z TST DOMI --dry-run
  python src\\acc_docs_folder_structure.py project_structure.7z TST DOMI
"""

from __future__ import annotations

import argparse
import os
import time
from collections import defaultdict

import requests
import py7zr

import auth
from auth import BASE_URL, get_auth_headers
from acc_hub_projects import get_projects


REQUEST_TIMEOUT = (5, 30)
MAX_RETRIES = 5


def _api_get(url: str, params: dict | None = None, accept: str | None = None):
    for attempt in range(MAX_RETRIES + 1):
        try:
            headers = get_auth_headers()
            if accept:
                headers["Accept"] = accept
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 10))
                continue
            return None, str(e)

        if resp.status_code == 429 and attempt < MAX_RETRIES:
            wait = int(resp.headers.get("Retry-After", 5))
            print(f"  [429] Rate limited (GET). Waiting {wait}s...")
            time.sleep(wait)
            continue
        return resp, ""
    return None, "exhausted retries"


def _api_post_jsonapi(url: str, body: dict):
    for attempt in range(MAX_RETRIES + 1):
        try:
            headers = get_auth_headers()
            headers["Content-Type"] = "application/vnd.api+json"
            headers["Accept"] = "application/vnd.api+json"
            resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 10))
                continue
            return None, str(e)

        if resp.status_code == 429 and attempt < MAX_RETRIES:
            wait = int(resp.headers.get("Retry-After", 5))
            print(f"  [429] Rate limited (POST). Waiting {wait}s...")
            time.sleep(wait)
            continue
        return resp, ""
    return None, "exhausted retries"


def find_project_id_by_name(hub_id: str, project_name: str) -> str:
    projects = get_projects(hub_id)
    wanted = project_name.strip().lower()
    for p in projects:
        name = (p.get("attributes", {}).get("name") or "").strip().lower()
        if name == wanted:
            return p.get("id", "")
    return ""


def get_top_folders(hub_id: str, project_id: str) -> list[dict]:
    url = f"{BASE_URL}/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders"
    resp, err = _api_get(url, accept="application/vnd.api+json")
    if err or resp is None or resp.status_code != 200:
        raise RuntimeError(f"topFolders failed: {err or (resp.text if resp is not None else 'no response')}")
    return resp.json().get("data", [])


def pick_project_files_top_folder(top_folders: list[dict]) -> str:
    # Prefer "Project Files" (Docs) folder.
    for f in top_folders:
        name = (f.get("attributes", {}).get("name") or "").strip().lower()
        if name == "project files":
            return f.get("id", "")
    # Fallback to first.
    return top_folders[0].get("id", "") if top_folders else ""


def list_child_folders(project_id: str, folder_id: str) -> dict[str, str]:
    """Return mapping child_folder_name_lower -> child_folder_id for a parent folder."""
    url = f"{BASE_URL}/data/v1/projects/{project_id}/folders/{folder_id}/contents"
    params = {"page[limit]": 200}
    resp, err = _api_get(url, params=params, accept="application/vnd.api+json")
    if err or resp is None or resp.status_code != 200:
        # If we can't list, return empty so we'll try create and rely on API errors.
        return {}
    payload = resp.json()

    out: dict[str, str] = {}
    for item in payload.get("data", []):
        if item.get("type") != "folders":
            continue
        name = (item.get("attributes", {}).get("name") or "").strip()
        fid = item.get("id", "")
        if name and fid:
            out[name.lower()] = fid
    return out


def create_folder(project_id: str, parent_folder_id: str, name: str) -> tuple[str, str]:
    # Folder creation uses POST /data/v1/projects/:project_id/folders (not /contents).
    url = f"{BASE_URL}/data/v1/projects/{project_id}/folders"
    body = {
        "jsonapi": {"version": "1.0"},
        "data": {
            "type": "folders",
            "attributes": {
                "name": name,
                "extension": {
                    "type": "folders:autodesk.bim360:Folder",
                    "version": "1.0",
                },
            },
            "relationships": {"parent": {"data": {"type": "folders", "id": parent_folder_id}}},
        },
    }
    resp, err = _api_post_jsonapi(url, body)
    if err or resp is None:
        return "", err or "no response"
    if resp.status_code == 409:
        # Folder already exists under this parent.
        return "", "already_exists"
    if resp.status_code not in (200, 201):
        return "", f"HTTP {resp.status_code}: {resp.text[:300]}"
    try:
        data = resp.json().get("data", {})
    except ValueError:
        data = {}
    return str(data.get("id", "")).strip(), ""


def folder_paths_from_7z(archive_path: str) -> list[str]:
    with py7zr.SevenZipFile(archive_path, mode="r") as z:
        names = z.getnames()

    folders: set[str] = set()
    for raw in names:
        if not raw:
            continue
        # Normalize separators.
        p = raw.replace("\\", "/").strip("/")
        if not p:
            continue

        parts = [x for x in p.split("/") if x]
        if not parts:
            continue

        # Ignore a single leading container folder (common in archives)
        if parts and parts[0].lower() in {"project_structure", "project-structure"}:
            parts = parts[1:]
        if not parts:
            continue

        # If it's a file path, we still want its parent folders.
        # We can't reliably detect files without extracting, so we treat every segment as folder candidates.
        # Build all prefixes.
        prefix = []
        for seg in parts:
            prefix.append(seg)
            folders.add("/".join(prefix))

    # Sort by depth then name.
    return sorted(folders, key=lambda s: (s.count("/"), s.lower()))


def main():
    parser = argparse.ArgumentParser(description="Create ACC Docs folder structure from a .7z archive.")
    parser.add_argument("archive_7z", help="Path to project_structure.7z")
    parser.add_argument("target", help="TST/AG or hub key like Swissgrid_TST")
    parser.add_argument("project_name", help="Exact ACC project name (case-insensitive)")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be created")
    parser.add_argument(
        "--min-delay",
        type=float,
        default=0.35,
        help="Minimum delay (seconds) between create/list operations to reduce 429 rate limiting.",
    )
    parser.add_argument(
        "--adaptive-backoff",
        action="store_true",
        help="After a 429, temporarily increase delay for subsequent operations.",
    )
    args = parser.parse_args()

    target = (args.target or "").strip()
    if target.upper() in {"TST", "AG"}:
        env = target.upper()
        hub_key = f"Swissgrid_{env}"
    elif target in {"Swissgrid_TST", "Swissgrid_AG"}:
        hub_key = target
        env = target.split("_")[-1].upper()
    else:
        raise SystemExit("target must be TST, AG, Swissgrid_TST, or Swissgrid_AG")

    auth.set_acc_env(env)
    hub_id = os.getenv(hub_key, "").strip()
    if not hub_id:
        raise SystemExit(f"Hub key '{hub_key}' not found in .env")

    project_id = find_project_id_by_name(hub_id, args.project_name)
    if not project_id:
        raise SystemExit(f"Project not found in hub {hub_key}: {args.project_name}")

    print(f"\nEnvironment: {env} (hub: {hub_key})")
    print(f"Project: {args.project_name} ({project_id})")
    if args.dry_run:
        print("*** DRY-RUN MODE — no folders will actually be created ***")

    paths = folder_paths_from_7z(args.archive_7z)
    print(f"\nArchive folders discovered: {len(paths)}")

    top = get_top_folders(hub_id, project_id)
    top_folder_id = pick_project_files_top_folder(top)
    if not top_folder_id:
        raise SystemExit("Could not determine top folder (Project Files).")
    print(f"Top folder (Project Files): {top_folder_id}")

    # Cache children by parent folder id.
    child_cache: dict[str, dict[str, str]] = {}
    # Current delay used between API calls.
    delay_s = max(0.0, float(args.min_delay or 0.0))

    created = 0
    skipped = 0
    failed = 0
    rate_limited = 0

    # Group by parent to reduce list calls (still idempotent).
    for idx, path in enumerate(paths, 1):
        segments = [s for s in path.split("/") if s]
        parent_id = top_folder_id
        ok = True

        for depth, seg in enumerate(segments, 1):
            seg_clean = seg.strip()
            if not seg_clean:
                continue

            if parent_id not in child_cache:
                time.sleep(delay_s)
                child_cache[parent_id] = list_child_folders(project_id, parent_id)

            children = child_cache[parent_id]
            existing_id = children.get(seg_clean.lower())
            if existing_id:
                parent_id = existing_id
                continue

            if args.dry_run:
                print(f"[{idx}/{len(paths)}] WOULD CREATE: {'/'.join(segments[:depth])}")
                # We cannot know id in dry-run; set dummy id so deeper folders still show.
                dummy = f"dryrun:{parent_id}:{seg_clean.lower()}"
                children[seg_clean.lower()] = dummy
                parent_id = dummy
                created += 1
                continue

            new_id, err = create_folder(project_id, parent_id, seg_clean)
            if err == "already_exists":
                # Refresh children and continue with the existing id.
                time.sleep(delay_s)
                child_cache[parent_id] = list_child_folders(project_id, parent_id)
                existing_id = child_cache[parent_id].get(seg_clean.lower())
                if existing_id:
                    parent_id = existing_id
                    continue
                print(
                    f"[{idx}/{len(paths)}] FAILED CREATE: {'/'.join(segments[:depth])} "
                    f"(409 already exists but could not resolve id)"
                )
                failed += 1
                ok = False
                break

            if err or not new_id:
                print(f"[{idx}/{len(paths)}] FAILED CREATE: {'/'.join(segments[:depth])} ({err})")
                failed += 1
                ok = False
                break

            children[seg_clean.lower()] = new_id
            parent_id = new_id
            created += 1
            time.sleep(delay_s)

        if ok:
            skipped += 0

        # Progress heartbeat every 25 paths to avoid looking "stuck".
        if idx % 25 == 0 and not args.dry_run:
            print(
                f"  Progress: {idx}/{len(paths)} paths processed "
                f"(created={created}, failed={failed}, delay={delay_s:.2f}s)"
            )

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Created: {created}")
    print(f"Failed:  {failed}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()

