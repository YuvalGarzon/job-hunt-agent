"""Move a Google service-account key from ~/Downloads into the Keychain, then delete the file.

Usage (in the Terminal app, from the project folder):
    .venv/bin/python scripts/import_sa_key.py [path/to/key.json]

Without a path it takes the newest service-account key (*.json) in ~/Downloads, stores its
content in the Keychain as GOOGLE_SA_JSON (service "job-hunt-agent"), and deletes the file
directly (not to the Trash). The key content is never printed.
"""
import json
import sys
from pathlib import Path

import keyring

SERVICE = "job-hunt-agent"


def find_key(arg: str = None) -> tuple:
    """(path, data) of the service-account key to import, or exits."""
    candidates = [Path(arg).expanduser()] if arg else sorted(Path.home().joinpath("Downloads").glob("*.json"))
    keys = []
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "service_account" and data.get("private_key"):
            keys.append((path, data))
    if not keys:
        sys.exit("No service-account key (.json) found" + ("" if arg else " in ~/Downloads"))
    return max(keys, key=lambda k: k[0].stat().st_mtime)


def import_key(path: Path, data: dict) -> str:
    """Store the key, check it, delete the file. Returns the service account's email."""
    keyring.set_password(SERVICE, "GOOGLE_SA_JSON", json.dumps(data))
    if json.loads(keyring.get_password(SERVICE, "GOOGLE_SA_JSON"))["private_key_id"] != data["private_key_id"]:
        sys.exit("Keychain write could not be verified; the file was NOT deleted")
    path.unlink()
    print(f"deleted {path.name}")
    print(f"saved GOOGLE_SA_JSON in the Keychain (key id ends ...{data['private_key_id'][-4:]})")
    return data["client_email"]


def main() -> None:
    path, data = find_key(sys.argv[1] if len(sys.argv) > 1 else None)
    email = import_key(path, data)
    print(f"Share your Google Sheet with {email} (Editor).")


if __name__ == "__main__":
    main()
