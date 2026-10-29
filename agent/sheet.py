"""The Google Sheet: the single source of truth for state, shared by the cloud and the Mac.

Access: a service account with scope spreadsheets only, opening one Sheet by ID. Rows are
never deleted, so row numbers stay stable and both halves can update by row.
"""
import json
import time
from datetime import datetime, timezone, tzinfo

import gspread
from google.oauth2.service_account import Credentials

from agent import config, secrets

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TABS = {
    "Applications": ["App ID", "Company", "Role", "Location", "Source", "Job link", "Fit score",
                     "Date found", "Date approved", "Date applied", "CV path", "Cover letter (Y/N)",
                     "Status", "Last update", "Next step", "Follow-up due", "Notes", "Job ID"],
    "Events": ["Date", "App ID", "Type", "Sender", "Summary", "Email link", "Message ID"],
    "Jobs": ["Job ID", "First seen", "Source", "Source ref", "Company", "Title", "Location", "City",
             "Distance km", "Posted", "URL", "Decision", "Reason", "Score", "Score parts", "Stretch",
             "Location flag", "Fit", "Gaps", "Batch ID", "Rank", "Alert msg ID", "User action", "Updated",
             "Closed", "Open check", "Skip reason"],
    "Queue": ["Task ID", "Type", "Job ID", "App ID", "Payload", "Status", "Attempts", "Created",
              "Updated", "Result", "Error"],
    "Inbox": ["Update ID", "Received", "Kind", "Data", "Handled", "Handled at", "By"],
    "Health": ["Key", "Value", "Updated"],
    "Settings": ["Key", "Value", "Updated"],
    "Geo": ["Place", "Lat", "Lon", "Distance km", "Source", "Updated", "Country"],
    "Answers": ["Q ID", "Question", "Answer", "Status", "Job ID", "Msg ID", "Created", "Updated", "Uses"],
    "Boards": ["Company", "ATS", "Board", "Status", "Checked", "About", "Careers", "Found by"],
}


class _LocalTime(tzinfo):
    """The user's time zone (settings.yaml timezone), looked up when first used."""

    def utcoffset(self, dt):
        return config.tz().utcoffset(dt)

    def dst(self, dt):
        return config.tz().dst(dt)

    def tzname(self, dt):
        return config.tz().tzname(dt)

    def fromutc(self, dt):
        return dt.replace(tzinfo=timezone.utc).astimezone(config.tz()).replace(tzinfo=self)


TZ = _LocalTime()


def now_iso() -> str:
    """Local time with its UTC offset, e.g. '2026-10-22 23:06:07+03:00': readable in the Sheet,
    and still exact for datetime.fromisoformat."""
    return datetime.now(TZ).isoformat(sep=" ", timespec="seconds")


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class SheetError(RuntimeError):
    pass


def _retry(fn, *args, **kwargs):
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            code = getattr(e.response, "status_code", 0)
            if code == 403:
                raise SheetError("Sheets returned 403: the Sheet is probably no longer shared with the "
                                 "service account, or its key was deleted") from None
            if code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(2 ** attempt * 3)
                continue
            raise


class Table:
    """One tab loaded into memory. Changes are queued and written by Sheet.flush()."""

    def __init__(self, sheet: "Sheet", name: str, values: list):
        self.sheet, self.name = sheet, name
        self.header = TABS[name]
        self.rows = []                      # list of dicts, each with "_row" (1-based sheet row)
        for i, raw in enumerate(values[1:], start=2):
            raw = list(raw) + [""] * (len(self.header) - len(raw))
            d = dict(zip(self.header, raw))
            d["_row"] = i
            self.rows.append(d)
        self._next_row = len(values) + 1 if values else 2

    def find(self, key: str, value) -> dict:
        value = str(value)
        for r in self.rows:
            if r.get(key) == value:
                return r
        return None

    def where(self, **kw) -> list:
        return [r for r in self.rows if all(r.get(k) == str(v) for k, v in kw.items())]

    def append(self, record: dict) -> dict:
        row = {h: "" for h in self.header}
        row.update({k: ("" if v is None else str(v)) for k, v in record.items() if k in self.header})
        row["_row"] = None
        self.rows.append(row)
        self.sheet._appends.setdefault(self.name, []).append(row)
        return row

    def update(self, row: dict, **changes) -> None:
        for k, v in changes.items():
            if k not in self.header:
                raise KeyError(f"{self.name} has no column {k}")
            row[k] = "" if v is None else str(v)
        if row["_row"] is None:
            return                                    # still pending append; written with it
        for k in changes:
            c = self.header.index(k) + 1
            self.sheet._updates.append((f"'{self.name}'!{col_letter(c)}{row['_row']}", row[k]))


class Sheet:
    def __init__(self, sheet_id: str = None):
        info = json.loads(secrets.get("GOOGLE_SA_JSON"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.gc.set_timeout((4, 15))       # connect, read                 # never hang on a dead connection (e.g. after sleep)
        self.sh = _retry(self.gc.open_by_key, sheet_id or secrets.get("SHEET_ID"))
        self.tables = {}
        self._appends, self._updates = {}, []

    # --- setup -------------------------------------------------------------------------
    def ensure_tabs(self) -> None:
        existing = {ws.title: ws for ws in _retry(self.sh.worksheets)}
        for name, header in TABS.items():
            ws = existing.get(name)
            if ws is None:
                ws = _retry(self.sh.add_worksheet, title=name, rows=200, cols=len(header))
                _retry(ws.update, [header], "A1")
                _retry(ws.freeze, rows=1)
            else:
                first = _retry(ws.row_values, 1)
                if first != header:
                    # allowed: an empty first row, or new columns added at the end of the header
                    if any(first) and first != header[:len(first)]:
                        raise SheetError(f"tab {name} has unexpected headers: {first}")
                    if ws.col_count < len(header):
                        _retry(ws.add_cols, len(header) - ws.col_count)
                    _retry(ws.update, [header], "A1")
                    _retry(ws.freeze, rows=1)

    # --- reading -----------------------------------------------------------------------
    def load(self, *names) -> None:
        names = names or tuple(TABS)
        res = _retry(self.sh.values_batch_get, [f"'{n}'!A1:{col_letter(len(TABS[n]))}" for n in names])
        for name, vr in zip(names, res.get("valueRanges", [])):
            self.tables[name] = Table(self, name, vr.get("values", []))

    def forget(self) -> None:
        """Drop every cached tab; each is read again on first use."""
        self.tables = {}

    def t(self, name: str) -> Table:
        if name not in self.tables:
            self.load(name)
        return self.tables[name]

    # --- writing -----------------------------------------------------------------------
    def flush(self) -> None:
        for name, rows in list(self._appends.items()):
            if not rows:
                continue
            header = TABS[name]
            ws = self.sh.worksheet(name)
            values = [[r.get(h, "") for h in header] for r in rows]
            res = _retry(ws.append_rows, values, value_input_option="RAW", insert_data_option="INSERT_ROWS",
                         table_range="A1")
            rng = (res or {}).get("updates", {}).get("updatedRange", "")
            start = int("".join(ch for ch in rng.split("!")[-1].split(":")[0] if ch.isdigit()) or 0)
            for i, r in enumerate(rows):
                r["_row"] = start + i if start else None
        self._appends = {}
        if self._updates:
            data = [{"range": a1, "values": [[v]]} for a1, v in self._updates]
            _retry(self.sh.values_batch_update, {"valueInputOption": "RAW", "data": data})
            self._updates = []

    # --- key/value tabs ----------------------------------------------------------------
    def kv_get(self, tab: str, key: str, default: str = "") -> str:
        r = self.t(tab).find("Key", key)
        return r["Value"] if r else default

    def kv_set(self, tab: str, key: str, value, flush: bool = True) -> None:
        tbl = self.t(tab)
        r = tbl.find("Key", key)
        if r:
            tbl.update(r, Value=value, Updated=now_iso())
        else:
            tbl.append({"Key": key, "Value": value, "Updated": now_iso()})
        if flush:
            self.flush()


def set_setting(key: str, value) -> None:
    """Convenience for scripts: write one Settings value."""
    s = Sheet()
    s.load("Settings")
    s.kv_set("Settings", key, value)
