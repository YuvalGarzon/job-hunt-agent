"""Minimal Telegram Bot API client. Sends only to the owner's own chat (TELEGRAM_CHAT_ID)."""
import json
import re
import threading
from pathlib import Path

import requests

from agent import secrets

API = "https://api.telegram.org"
TIMEOUT = 20


class TelegramError(RuntimeError):
    pass


CONNECT_TIMEOUT = 4          # a connection that does not open in 4 s is stuck: try again
_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_local, "s"):                        # one kept-alive session per thread
        _local.s = requests.Session()
    return _local.s


def _call(method: str, data: dict = None, files: dict = None, timeout: int = TIMEOUT) -> dict:
    url = f"{API}/bot{secrets.get('TELEGRAM_BOT_TOKEN')}/{method}"
    for attempt in range(3):
        try:
            r = _session().post(url, data=data, files=files, timeout=(CONNECT_TIMEOUT, timeout))
            body = r.json()
            break
        except requests.ConnectTimeout:                 # nothing was sent: safe to retry
            _local.__dict__.pop("s", None)
            if attempt == 2:
                raise TelegramError(f"{method} failed: could not connect") from None
        except (requests.RequestException, ValueError) as e:
            _local.__dict__.pop("s", None)
            raise TelegramError(secrets.redact(f"{method} failed: {e.__class__.__name__}")) from None
    if not body.get("ok"):
        raise TelegramError(f"{method} failed: {body.get('description')}")
    return body["result"]


MAX_FILE = 20 * 1024 * 1024       # the Bot API serves files up to 20 MB


def get_file(file_id: str) -> bytes:
    """The bytes of a photo or file the user sent. The download link carries the bot token, so it is
    never logged or put in an error."""
    path = _call("getFile", {"file_id": file_id})["file_path"]
    try:
        r = _session().get(f"{API}/file/bot{secrets.get('TELEGRAM_BOT_TOKEN')}/{path}",
                           timeout=(CONNECT_TIMEOUT, 60))
        r.raise_for_status()
    except requests.RequestException as e:
        _local.__dict__.pop("s", None)
        raise TelegramError(f"file download failed: {e.__class__.__name__}") from None
    return r.content


def chat_id() -> str:
    return secrets.get("TELEGRAM_CHAT_ID")


def _is_link(d: str) -> bool:
    return d.startswith(("https://", "http://"))


def keyboard(rows: list) -> str:
    """rows: [[(text, callback_data or link), ...], ...] -> reply_markup JSON. A link becomes a
    button that opens it."""
    return json.dumps({"inline_keyboard": [[{"text": t, "url": d} if _is_link(d) else
                                            {"text": t, "callback_data": d} for t, d in row] for row in rows]})


def link_row(url: str, label: str = "🔗 Open job") -> list:
    """A button row that opens a link. Buttons open on every Telegram app, while a link inside
    right-to-left text does not open in Telegram for macOS."""
    return [[(label, url)]] if url and _is_link(url) else []


def _without_links(rows: list) -> list:
    return [r for r in ([(t, d) for t, d in row if not _is_link(d)] for row in rows or []) if r]


def _send(method: str, data: dict, buttons: list, **kw) -> dict:
    """A link Telegram refuses as a button never stops the message: it is sent without it."""
    if buttons:
        data["reply_markup"] = keyboard(buttons)
    try:
        return _call(method, data, **kw)
    except TelegramError as e:
        if not (buttons and any(_is_link(d) for row in buttons for _, d in row) and
                re.search(r"url|button", str(e), re.I)):
            raise
    data["reply_markup"] = keyboard(_without_links(buttons))
    return _call(method, data, **kw)


def send_message(text: str, buttons: list = None, html: bool = True, disable_preview: bool = True) -> dict:
    data = {"chat_id": chat_id(), "text": text, "disable_web_page_preview": disable_preview}
    if html:
        data["parse_mode"] = "HTML"
    return _send("sendMessage", data, buttons)


def send_document(path: Path, caption: str = "", buttons: list = None) -> dict:
    data = {"chat_id": chat_id(), "caption": caption, "parse_mode": "HTML"}
    with open(path, "rb") as f:
        return _send("sendDocument", data, buttons, files={"document": (Path(path).name, f, "application/pdf")},
                     timeout=60)


def keep_links(message_id: int, reply_markup: dict) -> None:
    """Remember the link buttons of a message whose button was just tapped: edit_buttons keeps
    them, so a card never loses its link when its other buttons change."""
    rows = [[(b["text"], b["url"]) for b in row if b.get("url")]
            for row in (reply_markup or {}).get("inline_keyboard", [])]
    _local.links = {message_id: [r for r in rows if r]}


def edit_buttons(message_id: int, buttons: list = None) -> None:
    buttons = list(buttons or [])
    if not any(_is_link(d) for row in buttons for _, d in row):
        buttons += getattr(_local, "links", {}).get(message_id, [])
    data = {"chat_id": chat_id(), "message_id": message_id,
            "reply_markup": keyboard(buttons)}
    try:
        _call("editMessageReplyMarkup", data)
    except TelegramError as e:
        if "not modified" not in str(e):
            raise


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except TelegramError:
        pass  # callbacks older than ~15 minutes can no longer be answered; harmless


def get_updates(offset: int = None, timeout: int = 0) -> list:
    data = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        data["offset"] = offset
    return _call("getUpdates", data, timeout=timeout + TIMEOUT)


def send_chat_action(action: str = "typing") -> None:
    try:
        _call("sendChatAction", {"chat_id": chat_id(), "action": action})
    except TelegramError:
        pass


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def set_menu(commands: list) -> None:
    """The ☰ Menu button next to the message box: the list of commands, one tap each.
    commands = [(command without '/', description)]."""
    _call("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in commands],
                                                   ensure_ascii=False)})
    _call("setChatMenuButton", {"chat_id": chat_id(), "menu_button": json.dumps({"type": "commands"})})
