"""Telegram batch alerts: all new relevant jobs of a run in one batch, best first."""
import re

from agent import telegram as tg
from agent.sheet import Sheet, now_iso
from agent.sources import board_mail

BATCH_SIZE = 10


def job_text(r: dict, number: int) -> str:
    e = tg.esc
    place = r["City"] or ("location?" if r["Location flag"] else r["Location"][:40]) or "location?"
    lines = [f"<b>{number}. {e(r['Title'])}</b>",
             f"🏢 {e(r['Company'])} · 📍 {e(place)} · ⭐ <b>{e(r['Score'])}</b>"]
    fit = (r.get("Fit") or "").split("\n")
    if fit and fit[0]:
        lines.append(f"✅ {e(fit[0])}")
    if len(fit) > 1 and fit[1]:
        lines.append(f"     {e(fit[1])}")
    if r.get("Gaps"):
        lines.append(f"⚠️ Gaps: {e(r['Gaps'])}")
    tags = []
    if r.get("Stretch") == "yes":
        tags.append("🔸 stretch (asks for more experience)")
    if r.get("Source") == "linkedin":
        tags.append("🔗 LinkedIn only, not verified on the company site")
    if r.get("Source") == "telegram":
        own = re.match(r"https?://(?!(?:www\.)?(?:linkedin|indeed|glassdoor|facebook|t\.me|"
                       r"instagram|x\.com|twitter))", r.get("URL") or "", re.I)
        tags.append("📨 you sent it from the company site" if own else "📨 from a post, not verified on the company site")
    boards = board_mail.boards()
    if r.get("Source") in boards:
        name = boards[r["Source"]][1]
        tags.append(f"📰 from {'an' if name[:1].lower() in 'aeiou' else 'a'} {name} alert, not verified on the company site")
    if tags:
        lines.append(" · ".join(tags))
    m = TWIN_RE.search(r.get("Reason") or "")
    if m:
        lines.append(f"⚠️ <b>Maybe the same job</b> as {e(m.group(2))} ({e(m.group(3))}). Worth checking before you apply.")
    return "\n".join(lines)


TWIN_RE = re.compile(r"maybe same job: (\S+) \| (.+?) \| ([^;]+)")


def twin_note(app: dict) -> str:
    """Stored in the Jobs row's Reason, so the card can show which application it may repeat."""
    when = f"applied {app['Date applied']}" if app["Date applied"] else f"status {app['Status']}"
    return f"maybe same job: {app['App ID']} | {app['Role']} · {app['Company']} | {when}"


def job_buttons(job_id: str, twin: bool = False, url: str = "", closable: bool = False) -> list:
    # on a card that already warns "maybe the same job", Approve goes straight to the CV;
    # a job the agent cannot check itself gets 🔒 Closed, so the user can say it is closed
    row = [("✅ Approve", f"{'apy' if twin else 'ap'}:{job_id}"), ("⏭ Skip", f"sk:{job_id}")]
    if closable:
        row.append(("🔒 Closed", f"closed:{job_id}"))
    return [row] + tg.link_row(url)


def card_buttons(r: dict) -> list:
    """The buttons of an open job's card (a Jobs row)."""
    from agent.openness import checkable
    return job_buttons(r["Job ID"], bool(TWIN_RE.search(r.get("Reason") or "")), r.get("URL", ""), not checkable(r))


def _send_rows(sheet: Sheet, rows: list, start_number: int) -> None:
    jobs = sheet.t("Jobs")
    try:
        for i, r in enumerate(rows):
            if r.get("Alert msg ID"):
                continue                              # already sent: never twice
            msg = tg.send_message(job_text(r, start_number + i),
                                  buttons=card_buttons(r))
            jobs.update(r, **{"Decision": "alerted", "Alert msg ID": msg["message_id"], "Updated": now_iso()})
    finally:
        sheet.flush()                                 # one write for the whole batch


def send_batch(sheet: Sheet, batch_id: str) -> int:
    jobs = sheet.t("Jobs")
    rows = sorted([r for r in jobs.rows if r["Batch ID"] == batch_id], key=lambda r: int(r["Rank"] or 0))
    first = [r for r in rows if r["Decision"] in ("alerting",)]
    more = [r for r in rows if r["Decision"] == "more_pending"]
    total = len(rows)
    header = f"🆕 <b>{total} new jobs</b>, by fit score"
    if more:
        header += f" (showing the first {len(first)})"
    tg.send_message(header)
    _send_rows(sheet, first, 1)
    if more:
        tg.send_message(f"{len(more)} more jobs in this round.", buttons=[[("➕ More", f"more:{batch_id}")]])
    return len(first)


def send_more(sheet: Sheet, batch_id: str) -> int:
    jobs = sheet.t("Jobs")
    rows = sorted([r for r in jobs.rows if r["Batch ID"] == batch_id], key=lambda r: int(r["Rank"] or 0))
    pending = [r for r in rows if r["Decision"] == "more_pending"]
    nxt = pending[:BATCH_SIZE]
    if not nxt:
        tg.send_message("No more jobs in this round.")
        return 0
    for r in nxt:
        jobs.update(r, Decision="alerting")
    sheet.flush()
    _send_rows(sheet, nxt, int(nxt[0]["Rank"]))
    left = len(pending) - len(nxt)
    if left:
        tg.send_message(f"{left} more jobs.", buttons=[[("➕ More", f"more:{batch_id}")]])
    return len(nxt)


def resend_unsent(sheet: Sheet) -> int:
    """After a crash between recording and sending: send rows marked 'alerting' with no message."""
    jobs = sheet.t("Jobs")
    rows = sorted([r for r in jobs.rows if r["Decision"] == "alerting" and not r["Alert msg ID"]],
                  key=lambda r: (r["Batch ID"], int(r["Rank"] or 0)))
    if rows:
        tg.send_message("🔁 Jobs from the last round that weren't sent:")
        _send_rows(sheet, rows, 1)
    return len(rows)


SENT_DAYS = 60
REACHED = ("alerted", "alerting", "more_pending", "forwarded")


class Sent:
    """Jobs that already reached the user (sent, sent by them, approved or skipped), by company: the
    same opening seen again through another link or source is not sent twice."""

    def __init__(self, sheet: Sheet):
        from datetime import datetime, timedelta
        from agent.sheet import TZ
        from agent.sources.linkedin import company_key
        since = (datetime.now(TZ) - timedelta(days=SENT_DAYS)).strftime("%Y-%m-%d")
        self.by_company = {}
        for r in sheet.t("Jobs").rows:
            if (r["Decision"] in REACHED or r["User action"]) and r["First seen"][:10] >= since:
                self.add(r["Company"], r["Title"], r["First seen"][:10], r["Source ref"], company_key)

    def add(self, company: str, title: str, day: str, ref: str = "", key=None) -> None:
        from agent.sources.linkedin import company_key
        k = (key or company_key)(company)
        if k:
            self.by_company.setdefault(k, []).append((same_words(title), day, ref))

    def before(self, company: str, title: str, ref: str = "") -> str:
        """'already sent <day>' when this company's job with the same title (spacing and
        punctuation aside) reached the user. Exact on purpose: "Engineer, Annapurna Labs" and
        "Engineer - Nitro, Annapurna Labs" are two openings. Two postings on the company's own
        board with different IDs are two openings too (NVIDIA lists several "Firmware Engineer")."""
        from agent.sources.linkedin import company_key
        words = same_words(title)
        for w, day, r in self.by_company.get(company_key(company), []):
            if w != words:
                continue
            if ref and r and ref != r and ref.split(":")[0] == r.split(":")[0]:
                continue
            return f"already sent {day}"
        return ""


def same_words(title: str) -> str:
    from agent.jobs import norm_text
    return re.sub(r"[\W_]+", "", norm_text(title))


PENDING_DAYS = 7                     # an alert left untouched this long quietly leaves /open


def pending_rows(sheet: Sheet) -> list:
    """Jobs sent (or waiting behind More) that the user has neither approved nor skipped and that are not
    closed: the newest first (posting date, else the day it was found), best score first within a day."""
    from datetime import datetime, timedelta
    from agent.sheet import TZ
    since = (datetime.now(TZ) - timedelta(days=PENDING_DAYS)).strftime("%Y-%m-%d")
    rows = [r for r in sheet.t("Jobs").rows
            if r["Decision"] in ("alerted", "alerting", "more_pending", "forwarded") and not r["User action"]
            and r["First seen"][:10] >= since and not r.get("Closed")]
    return sorted(rows, key=lambda r: ((r["Posted"] or r["First seen"])[:10], int(r["Score"] or 0)), reverse=True)


def send_pending(sheet: Sheet, offset: int = 0) -> int:
    """Re-send the open jobs as cards with Approve / Skip, 10 at a time."""
    rows = pending_rows(sheet)
    if not rows:
        tg.send_message("✅ No open jobs: you've handled every job sent to you.")
        return 0
    page = rows[offset:offset + BATCH_SIZE]
    if offset == 0:
        tg.send_message(f"📋 <b>{len(rows)} jobs you haven't handled yet</b>, newest first")
    jobs = sheet.t("Jobs")
    try:
        from agent.bot import same_job_app
        for i, r in enumerate(page):
            twin = same_job_app(sheet, r["Company"], r["Title"], r["Job ID"])
            shown = {**r, "Reason": twin_note(twin)} if twin else r          # warn, never hide
            msg = tg.send_message(job_text(shown, offset + i + 1), buttons=card_buttons(shown))
            changes = {"Alert msg ID": msg["message_id"], "Updated": now_iso()}
            if r["Decision"] in ("alerting", "more_pending"):
                changes["Decision"] = "alerted"
            jobs.update(r, **changes)
    finally:
        sheet.flush()
    left = len(rows) - offset - len(page)
    if left > 0:
        tg.send_message(f"{left} more jobs.", buttons=[[("➕ More", f"pend:{offset + len(page)}")]])
    return len(page)


def send_waiting_cvs(sheet: Sheet) -> int:
    """CVs that are ready but not marked as applied, each with its buttons (📨 I applied)."""
    from agent.bot import cv_buttons
    apps = [a for a in sheet.t("Applications").rows if a["Status"] in ("CV ready", "Approved")]
    apps.sort(key=lambda a: (a["Date approved"], a["Last update"]), reverse=True)        # newest first
    if not apps:
        tg.send_message("📄 No CVs waiting for you to apply.")
        return 0
    tg.send_message(f"📄 <b>{len(apps)} ready CVs you haven't marked as applied</b>")
    for a in apps:
        link = ""
        state = "CV ready" if a["Status"] == "CV ready" else "being built"
        if a["CV path"]:
            from agent import config
            link += f"\n{config.where_saved(a['CV path'])}"
        tg.send_message(f"📄 <b>{tg.esc(a['Role'])}</b> · {tg.esc(a['Company'])}\n{state}{link}",
                        buttons=cv_buttons(a["App ID"]) + tg.link_row(a["Job link"]))
    return len(apps)
