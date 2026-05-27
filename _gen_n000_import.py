"""Build provisioner CSV: unique users × AG hub projects whose name contains N000."""
import argparse
import csv
import os
import re
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import auth  # noqa: E402
from acc_provisioner import build_project_map  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HR = os.path.join(
    ROOT,
    "DATA_user_to_Import",
    "archiv",
    "USER_to_ACC_310326(USERS_TO_ACC_U).csv",
)
DEFAULT_USERS_CSV = os.path.join(ROOT, "DATA_user_to_Import", "new Users till April.csv")
DEFAULT_OUT = os.path.join(
    ROOT,
    "DATA_user_to_Import",
    "REAL_USERS_N000_projects_AG.csv",
)
DEFAULT_OUT_ONLY_ALWAYS = os.path.join(
    ROOT,
    "DATA_user_to_Import",
    "ADD_ONLY_philipp_fabian_N000_AG.csv",
)

ROLES = "swissgrid_intern"
COMPANY = "Swissgrid AG"
ACCESS = "Member"

# Always include these users on every N000 project export.
ALWAYS_INCLUDE_EMAILS = [
    "philipp.zihlmann@swissgrid.ch",
    "fabian.wellauer@swissgrid.ch",
]


def derive_names(email: str) -> tuple[str, str]:
    local = email.split("@", 1)[0].strip()
    parts = [p for p in re.split(r"[._\-]+", local) if p]
    if not parts:
        return "", ""
    first = parts[0].title()
    last = " ".join(p.title() for p in parts[1:]) if len(parts) > 1 else ""
    return first, last


def load_users_from_provisioner_csv(path: str) -> list[tuple[str, str, str]]:
    """Unique emails with first/last from first occurrence (UTF-8 comma CSV)."""
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            em = (row.get("email") or "").strip().lower()
            if not em or em in seen:
                continue
            seen.add(em)
            fn = (row.get("first_name") or "").strip()
            ln = (row.get("last_name") or "").strip()
            out.append((fn, ln, em))
    return out


def ensure_users_present(
    users: list[tuple[str, str, str]],
    emails: list[str],
    name_lookup: dict[str, tuple[str, str]],
) -> list[tuple[str, str, str]]:
    """Append missing emails (with names) while preserving existing order."""
    existing = {em.lower() for _, _, em in users}
    out = list(users)
    for raw in emails:
        em = raw.strip().lower()
        if not em or em in existing:
            continue
        fn, ln = name_lookup.get(em, derive_names(em))
        out.append((fn, ln, em))
        existing.add(em)
    return out


def load_hr_emails(path: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    with open(path, newline="", encoding="cp1252", errors="replace") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            raw = (row.get("E-mail") or row.get("E-Mail") or "").strip()
            if not raw:
                continue
            em = raw.lower()
            if em not in seen:
                seen.add(em)
                ordered.append(em)
    return ordered


def load_name_lookup(path: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    if not os.path.isfile(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            em = (row.get("email") or "").strip().lower()
            if not em or em in out:
                continue
            fn = (row.get("first_name") or "").strip()
            ln = (row.get("last_name") or "").strip()
            if fn or ln:
                out[em] = (fn, ln)
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Build user × AG hub projects (default: name contains N000)."
    )
    parser.add_argument(
        "--users-csv",
        metavar="PATH",
        help="Provisioner-style CSV (comma); unique emails, names from first row per email.",
    )
    parser.add_argument(
        "--hr",
        metavar="PATH",
        help="HR export (; delimited, E-mail column). Names from --names-csv or derived.",
    )
    parser.add_argument(
        "--names-csv",
        metavar="PATH",
        help="Optional UTF-8 CSV with first_name,last_name,email for name fallback.",
    )
    parser.add_argument("-o", "--output", metavar="PATH", help="Output CSV path.")
    parser.add_argument(
        "--only-always-include",
        action="store_true",
        help="Output only the ALWAYS_INCLUDE_EMAILS users (ignores --users-csv/--hr inputs).",
    )
    parser.add_argument(
        "--all-hub-projects",
        action="store_true",
        help="Include every hub project (ignore N000 filter).",
    )
    args = parser.parse_args()

    auth.set_acc_env("AG")
    hub_id = os.getenv("Swissgrid_AG", "").strip()
    if not hub_id:
        print("Error: Swissgrid_AG not set in .env")
        sys.exit(1)

    pmap = build_project_map(hub_id)
    all_names = {p["name"] for p in pmap.values()}
    if args.all_hub_projects:
        project_names = sorted(all_names, key=str.lower)
        print(f"AG hub: {len(project_names)} projects (all)")
    else:
        project_names = sorted(
            {n for n in all_names if "n000" in n.lower()},
            key=str.lower,
        )
        print(
            f"AG hub: {len(all_names)} projects total, "
            f"{len(project_names)} with 'N000' in name"
        )

    out_path = args.output or (
        DEFAULT_OUT_ONLY_ALWAYS if args.only_always_include else DEFAULT_OUT
    )
    names_path = args.names_csv or DEFAULT_USERS_CSV
    lookup = load_name_lookup(names_path)

    if args.only_always_include:
        users = []
    elif args.users_csv:
        users = load_users_from_provisioner_csv(args.users_csv)
        # Fill missing names from lookup / derive
        resolved = []
        for fn, ln, em in users:
            if not fn and not ln:
                fn, ln = lookup.get(em, derive_names(em))
            resolved.append((fn, ln, em))
        users = resolved
        print(f"Users from {args.users_csv}: {len(users)} unique emails")
    else:
        hr_path = args.hr or DEFAULT_HR
        emails = load_hr_emails(hr_path)
        users = []
        for em in emails:
            fn, ln = lookup.get(em, derive_names(em))
            users.append((fn, ln, em))
        print(f"Users from HR {hr_path}: {len(users)} unique emails")

    users = ensure_users_present(users, ALWAYS_INCLUDE_EMAILS, lookup)
    print(
        "Ensured users present: "
        + ", ".join(ALWAYS_INCLUDE_EMAILS)
    )

    rows_out = []
    for fn, ln, em in users:
        for pname in project_names:
            rows_out.append([fn, ln, em, pname, ROLES, COMPANY, ACCESS])

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "first_name",
                "last_name",
                "email",
                "project_name",
                "roles",
                "company",
                "access_level",
            ]
        )
        w.writerows(rows_out)

    print(f"Wrote {len(rows_out)} rows -> {out_path}")


if __name__ == "__main__":
    main()
