"""Gmail API over plain HTTPS.

The refresh token comes from the system keychain (computer) or GitHub Secrets (cloud); the short-lived
access tokens live only in memory. Reading uses a token narrowed to gmail.readonly; sending
uses one narrowed to gmail.send, and send_to_self() can only write to the user's own address
(read from the Gmail profile; there is no recipient parameter). The one change it makes to mail:
mark_read() marks the job-alert emails it has read as read (gmail.modify, used only to remove
the UNREAD label), so unread alerts in the inbox do not look like jobs that were missed. Nothing
here deletes, moves or archives mail.
"""
import base64
import email.message
import html as htmllib
import re

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from agent import secrets

API = "https://gmail.googleapis.com/gmail/v1/users/me"
READONLY = ["https://www.googleapis.com/auth/gmail.readonly"]
SEND = ["https://www.googleapis.com/auth/gmail.send"]
MODIFY = ["https://www.googleapis.com/auth/gmail.modify"]
TIMEOUT = 20
_creds = {}


class GmailAuthError(RuntimeError):
    """The refresh token was revoked or expired (invalid_grant): the user must sign in again."""


def _headers(scopes: list = READONLY) -> dict:
    key = tuple(scopes)
    c = _creds.get(key)
    if c is None or not c.valid:
        c = Credentials(None, refresh_token=secrets.get("GMAIL_REFRESH_TOKEN"),
                        client_id=secrets.get("GMAIL_CLIENT_ID"), client_secret=secrets.get("GMAIL_CLIENT_SECRET"),
                        token_uri="https://oauth2.googleapis.com/token", scopes=list(scopes))
        try:
            c.refresh(Request())
        except RefreshError as e:
            if "invalid_grant" in str(e):
                raise GmailAuthError("Gmail access expired or was revoked (invalid_grant)") from None
            raise
        _creds[key] = c
    return {"Authorization": f"Bearer {c.token}"}


def mark_read(ids: list, log=print) -> int:
    """Mark these emails as read (remove the UNREAD label, nothing else). Returns how many; 0 and
    one log line if the sign-in has no permission for it yet (an older sign-in: sign in again)."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return 0
    try:
        headers = _headers(MODIFY)
    except (RefreshError, GmailAuthError) as e:
        log(f"gmail: emails not marked as read: the Gmail sign-in has no permission for it yet "
            f"(run scripts/gmail_auth.py again) [{e.__class__.__name__}]")
        return 0
    done = 0
    for i in range(0, len(ids), 1000):                  # batchModify takes up to 1000 ids
        r = requests.post(f"{API}/messages/batchModify", json={"ids": ids[i:i + 1000], "removeLabelIds": ["UNREAD"]},
                          headers=headers, timeout=TIMEOUT)
        if r.status_code == 403:
            log("gmail: emails not marked as read: no permission (run scripts/gmail_auth.py again)")
            return done
        r.raise_for_status()
        done += len(ids[i:i + 1000])
    return done


def search(query: str, limit: int = 100) -> list:
    """Message IDs matching a Gmail search, newest first."""
    ids, page = [], None
    while len(ids) < limit:
        params = {"q": query, "maxResults": min(100, limit - len(ids))}
        if page:
            params["pageToken"] = page
        r = requests.get(f"{API}/messages", params=params, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids


def _parts(p: dict):
    yield p
    for q in p.get("parts") or []:
        yield from _parts(q)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def message(mid: str) -> dict:
    """{"id", "received_ms", "subject", "from", "text", "html"}; text is the plain-text body (or the
    HTML body reduced to text when there is no plain part)."""
    r = requests.get(f"{API}/messages/{mid}", params={"format": "full"}, headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    m = r.json()
    hdr = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
    plain = html = ""
    for p in _parts(m["payload"]):
        data = (p.get("body") or {}).get("data")
        if not data:
            continue
        if p.get("mimeType") == "text/plain" and not plain:
            plain = _decode(data)
        elif p.get("mimeType") == "text/html" and not html:
            html = _decode(data)
    if not plain and html:
        plain = htmllib.unescape(re.sub(r"<[^>]+>", "\n", html))
    return {"id": m["id"], "thread": m.get("threadId", ""), "received_ms": int(m.get("internalDate", 0)),
            "subject": hdr.get("subject", ""), "from": hdr.get("from", ""), "text": plain, "html": html,
            "link": f"https://mail.google.com/mail/u/0/#all/{m.get('threadId', m['id'])}"}


def own_address() -> str:
    r = requests.get(f"{API}/profile", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["emailAddress"]


def send_to_self(subject: str, html: str) -> str:
    """Send one email to the user's own address and nowhere else. Returns the message id."""
    me = own_address()
    msg = email.message.EmailMessage()
    msg["To"] = me
    msg["From"] = me
    msg["Subject"] = subject
    msg.set_content("This email needs an HTML view.")
    msg.add_alternative(html, subtype="html")
    if msg["To"] != me or msg.get_all("Cc") or msg.get_all("Bcc"):
        raise RuntimeError("refusing to send: the only allowed recipient is the owner")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = requests.post(f"{API}/messages/send", json={"raw": raw}, headers=_headers(SEND), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["id"]
