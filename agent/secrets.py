"""Secrets: the macOS Keychain on the Mac (service "job-hunt-agent"), environment variables in
GitHub Actions.

Values are returned to the caller only; nothing here logs or prints them.
"""
import os
from functools import lru_cache

SERVICE = "job-hunt-agent"


class MissingSecret(RuntimeError):
    pass


@lru_cache(maxsize=None)
def get(name: str, required: bool = True) -> str:
    value = os.environ.get(name)
    if not value:
        try:
            import keyring
            value = keyring.get_password(SERVICE, name)
        except Exception:
            value = None
    if not value and required:
        raise MissingSecret(f"secret {name} is not set")
    return value or ""


def set_local(name: str, value: str) -> None:
    """Update a Keychain value (Mac only)."""
    import keyring
    keyring.set_password(SERVICE, name, value)
    get.cache_clear()


def redact(text: str) -> str:
    """Remove any known secret value from a string before it is logged or sent."""
    for name in ("TELEGRAM_BOT_TOKEN", "GOOGLE_SA_JSON", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
                 "GMAIL_CLIENT_ID"):
        try:
            v = get(name, required=False)
        except Exception:
            v = ""
        if v and len(v) > 8:
            text = text.replace(v, f"<{name}>")
    return text
