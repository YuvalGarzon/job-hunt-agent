"""Is a job still open?

Jobs from a company's own careers system (Greenhouse, Lever, Ashby, Comeet, Workday, Microsoft,
Amazon, Google, Apple) are checked once a day through the same public APIs that found them. A
posting that is missing on two checks at least 20 hours apart is closed (one miss can be an
outage). Jobs from LinkedIn, job-board emails, Telegram posts and careers pages are never opened
(rule 4), so the user marks those with the 🔒 Closed button.

A closed job leaves /open. An approved job not applied to yet becomes Status "Closed" and the
user gets a short message; an application already sent keeps its status (companies still answer
after a posting closes) and gets a note.
"""
import concurrent.futures as cf
from datetime import datetime, timedelta

from agent import sources
from agent import telegram as tg
from agent.sheet import TZ, Sheet, now_iso

MISS_GAP = timedelta(hours=20)
MAX_CHECKS = 80                       # postings checked per day
NOT_APPLIED = ("Approved", "CV ready")
STILL_TRACKED = ("Approved", "CV ready", "Applied", "Assessment", "Interview", "No response")


def checkable(row: dict) -> bool:
    kind = (row.get("Source ref") or "").split(":", 1)[0]
    return kind in sources.ONE and kind == row.get("Source")


def is_closed(row: dict) -> bool:
    return bool(row.get("Closed"))


def _to_check(sheet: Sheet) -> list:
    from agent.alerts import PENDING_DAYS
    since = (datetime.now(TZ) - timedelta(days=PENDING_DAYS)).strftime("%Y-%m-%d")
    apps = {a["Job ID"]: a for a in sheet.t("Applications").rows}
    out = []
    for r in sheet.t("Jobs").rows:
        if is_closed(r) or not checkable(r):
            continue
        app = apps.get(r["Job ID"])
        pending = (r["Decision"] in ("alerted", "alerting", "more_pending", "forwarded") and not r["User action"]
                   and r["First seen"][:10] >= since)
        if pending or (app and app["Status"] in STILL_TRACKED):
            out.append(r)
    return out[-MAX_CHECKS:]


def _open(row: dict):
    """True / False (gone) / None (could not check now)."""
    try:
        return sources.fetch_one(row["Source ref"]) is not None
    except Exception:
        return None


def mark_closed(sheet: Sheet, job: dict, by: str, notify: bool = True) -> None:
    jobs, apps = sheet.t("Jobs"), sheet.t("Applications")
    jobs.update(job, Closed=f"{now_iso()[:10]} {by}", Updated=now_iso())
    app = apps.find("Job ID", job["Job ID"])
    if app and app["Status"] in NOT_APPLIED:
        apps.update(app, Status="Closed", **{"Next step": "—", "Last update": now_iso()})
        if notify:
            tg.send_message(f"🔒 <b>The job closed</b>: {tg.esc(job['Title'])} · {tg.esc(job['Company'])}\n"
                            "It's no longer on the company's site, so I removed it from /open. The files were kept.",
                            buttons=[[("↩️ It's still open", f"reopen:{job['Job ID']}")]] + tg.link_row(job["URL"]))
    elif app and app["Status"] in STILL_TRACKED:
        note = f"posting closed {now_iso()[:10]}"
        if note not in app["Notes"]:
            apps.update(app, Notes=(f"{app['Notes']}; {note}" if app["Notes"] else note)[:500],
                        **{"Last update": now_iso()})


def reopen(sheet: Sheet, job: dict) -> None:
    sheet.t("Jobs").update(job, Closed="", **{"Open check": ""}, Updated=now_iso())
    app = sheet.t("Applications").find("Job ID", job["Job ID"])
    if app and app["Status"] == "Closed":
        sheet.t("Applications").update(app, Status="CV ready" if app["CV path"] else "Approved",
                                       **{"Next step": "review and apply", "Last update": now_iso()})


def daily_check(sheet: Sheet, log=print) -> int:
    """Once a day from 10:00: check the postings that can be checked. Returns how many closed."""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour < 10 or sheet.kv_get("Health", "open_check_date", "") == today:
        return 0
    sheet.kv_set("Health", "open_check_date", today)
    sheet.load("Jobs", "Applications")
    rows = _to_check(sheet)
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_open, rows))
    closed = unknown = 0
    jobs = sheet.t("Jobs")
    for row, ok in zip(rows, results):
        if ok is None:
            unknown += 1
            continue
        if ok:
            if row.get("Open check"):
                jobs.update(row, **{"Open check": ""})
            continue
        first = (row.get("Open check") or "").removeprefix("missing ")
        try:
            gone_long = now - datetime.fromisoformat(first) >= MISS_GAP
        except ValueError:
            gone_long = False
        if gone_long:
            mark_closed(sheet, row, "auto")
            closed += 1
        elif not first:
            jobs.update(row, **{"Open check": f"missing {now_iso()}"})
    sheet.flush()
    log(f"open check: {len(rows)} postings, {closed} closed, {unknown} could not be checked")
    return closed
