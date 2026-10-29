"""Copy secrets from the macOS Keychain (service "job-hunt-agent") into your repo's GitHub
Actions Secrets (only for the optional cloud mode). Values go through stdin to `gh secret set`
and are never printed.

Usage: .venv/bin/python scripts/push_secret.py NAME [NAME ...]
       .venv/bin/python scripts/push_secret.py --all
"""
import subprocess
import sys
from pathlib import Path

import keyring

SERVICE = "job-hunt-agent"
ALL = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_SA_JSON", "SHEET_ID", "GMAIL_CLIENT_ID",
       "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_TOKEN_ISSUED",
       "PROFILE_YAML", "SETTINGS_YAML", "COMPANIES_YAML"]
# your own files, for the cloud run (they are never committed)
FILES = {"PROFILE_YAML": "PROFILE.yaml", "SETTINGS_YAML": "settings.yaml", "COMPANIES_YAML": "companies.yaml"}
ROOT = Path(__file__).resolve().parent.parent


def repo() -> str:
    r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit("No GitHub repo found for this folder: run this inside your own (private) copy of the repo")
    return r.stdout.strip()


def main() -> None:
    REPO = repo()
    for name in (ALL if sys.argv[1:] == ["--all"] else sys.argv[1:]):
        f = ROOT / FILES[name] if name in FILES else None
        value = f.read_text(encoding="utf-8") if f and f.exists() else keyring.get_password(SERVICE, name)
        if not value:
            print(f"{name}: not in the Keychain, skipped")
            continue
        r = subprocess.run(["gh", "secret", "set", name, "-R", REPO], input=value,
                           text=True, capture_output=True)
        print(f"{name}: {'set' if r.returncode == 0 else 'FAILED (exit ' + str(r.returncode) + ')'}")


if __name__ == "__main__":
    main()
