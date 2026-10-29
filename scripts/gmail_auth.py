"""Gmail sign-in (once, and again weekly while the OAuth app is in Testing).

Usage (in the Terminal app, from the project folder):
    .venv/bin/python scripts/gmail_auth.py
    .venv/bin/python scripts/gmail_auth.py --production   # after publishing the app: no 7-day expiry
    .venv/bin/python scripts/gmail_auth.py --github       # also copy the values to GitHub Secrets (cloud mode)

1. If a downloaded OAuth client file (client_secret_*.json) is in ~/Downloads, moves its
   client id and secret into the Keychain and deletes the file.
2. Opens the browser so you sign in yourself, with only gmail.readonly and gmail.send.
3. Stores the refresh token in the Keychain (and, with --github, in your repo's GitHub Secrets).
Nothing secret is printed or written to disk.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import keyring
import yaml
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "job-hunt-agent"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def import_client_file() -> None:
    files = sorted(Path.home().joinpath("Downloads").glob("client_secret_*.json"))
    found = []
    for path in files:
        try:
            data = json.loads(path.read_text()).get("installed")
        except (OSError, ValueError):
            continue
        if data and data.get("client_id") and data.get("client_secret"):
            found.append((path, data))
    if not found:
        return
    path, data = max(found, key=lambda f: f[0].stat().st_mtime)
    keyring.set_password(SERVICE, "GMAIL_CLIENT_ID", data["client_id"])
    keyring.set_password(SERVICE, "GMAIL_CLIENT_SECRET", data["client_secret"])
    for p, _ in found:
        p.unlink()
        print(f"deleted {p.name}")
    print("saved GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in the Keychain")


def repo() -> str:
    """This folder's GitHub repo (owner/name), for --github."""
    r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip()


def push(name: str, value: str, target: str) -> None:
    r = subprocess.run(["gh", "secret", "set", name, "-R", target], input=value, text=True, capture_output=True)
    print(f"GitHub secret {name}: {'set' if r.returncode == 0 else 'FAILED'}")


def own_email() -> str:
    try:
        return yaml.safe_load((ROOT / "PROFILE.yaml").read_text(encoding="utf-8"))["identity"]["contact"]["email"]
    except (OSError, KeyError, TypeError, ValueError):
        return ""


def main() -> None:
    import_client_file()
    client_id = keyring.get_password(SERVICE, "GMAIL_CLIENT_ID")
    client_secret = keyring.get_password(SERVICE, "GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("No OAuth client found: download the Desktop client JSON to ~/Downloads first (see the README)")
    config = {"installed": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}
    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    extra = {"login_hint": own_email()} if own_email() else {}
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  authorization_prompt_message="Opening the browser to sign in to Google...",
                                  success_message="Done. You can close this tab and go back to the Terminal.",
                                  **extra)
    if not creds.refresh_token:
        sys.exit("Google returned no refresh token; run again")
    granted = set(creds.granted_scopes or SCOPES)
    if granted - set(SCOPES):
        sys.exit(f"Unexpected extra scopes granted: {granted - set(SCOPES)}; not saved")
    # In Testing, Google ends the sign-in after 7 days (the agent reminds on day 6). A published
    # ("In production") app keeps it, so "production" turns the reminder off.
    issued = "production" if "--production" in sys.argv else datetime.now(timezone.utc).isoformat(timespec="seconds")
    keyring.set_password(SERVICE, "GMAIL_REFRESH_TOKEN", creds.refresh_token)
    keyring.set_password(SERVICE, "GMAIL_TOKEN_ISSUED", issued)
    print("saved GMAIL_REFRESH_TOKEN in the Keychain")
    if "--github" in sys.argv:
        target = repo()
        if not target:
            print("no GitHub repo found for this folder (gh repo view); GitHub Secrets not set")
        else:
            for name, value in (("GMAIL_CLIENT_ID", client_id), ("GMAIL_CLIENT_SECRET", client_secret),
                                ("GMAIL_REFRESH_TOKEN", creds.refresh_token), ("GMAIL_TOKEN_ISSUED", issued)):
                push(name, value, target)
    try:
        sys.path.insert(0, str(ROOT))
        from agent import sheet
        sheet.set_setting("gmail_token_issued", issued)
        print("recorded the sign-in date in the Sheet")
    except Exception as e:  # the Sheet may not be connected yet during setup
        print(f"(sign-in date not recorded in the Sheet: {e.__class__.__name__})")


if __name__ == "__main__":
    main()
