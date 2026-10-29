"""Tracking (step 6): company replies from Gmail, follow-up reminders, the 09:00 open jobs, the
18:00 digest and the 22:00 email. The steps run inside the normal runs (cloud or Mac), never as extra runs.

- mail_scan (about hourly, cloud or Mac): new emails whose sender or text names a company
  the user applied to through the agent become one `mail_event` task each (job-alert senders
  are skipped). Emails about applications the agent did not handle are left alone.
- mail_event (Mac, Claude): classifies the email, adds an Events row, updates the
  Application's Status, and tells the user on Telegram.
- follow-ups: 7 days after Applied with no reply, one reminder with a "draft" button; the
  draft (Mac, Claude) is for the user to send.
- 18:00 digest on Telegram and the 22:00 email to the user's own address, once a day each.
"""
import json
import re
from datetime import datetime, timedelta

from agent import claude_cli, config, gmail
from agent import telegram as tg
from agent.sheet import TZ, Sheet, now_iso
from agent.sources.linkedin import company_key

def alert_senders() -> list:
    """Job-alert senders: never a company reply."""
    from agent.sources import board_mail
    return ["linkedin.com"] + board_mail.senders()


SCAN_EVERY = timedelta(minutes=55)
FIRST_LOOKBACK_DAYS = 10
ACTIVE = ("Approved", "CV ready", "Applied", "Assessment", "Interview", "Offer", "No response")
# reply type -> Status (None = no change); a Status never moves back from a later stage
TYPE_STATUS = {"acknowledgment": None, "assessment": "Assessment", "interview": "Interview",
               "rejection": "Rejected", "offer": "Offer", "other": None}
STAGE = {s: i for i, s in enumerate(["Found", "Approved", "CV ready", "Applied", "No response", "Assessment",
                                     "Interview", "Offer"])}
TYPE_LABEL = {"acknowledgment": "📬 Application received", "assessment": "📝 Home assignment / assessment",
              "interview": "🗓 Interview invitation", "rejection": "❌ Rejection", "offer": "🎉 Job offer",
              "other": "✉️ Update"}


# --- 1. mail scan ------------------------------------------------------------------------
def _sender_domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    return m.group(1).lower() if m else ""


def match_app(msg: dict, apps: list):
    """The Application this email is about, or None. The company must be named in the sender
    (address or display name) or in the subject; the body only counts together with an ATS
    sender (Greenhouse, Lever, Comeet, Workday, Ashby...), which writes on the company's behalf."""
    sender = (msg.get("from") or "").lower()
    head = company_key(sender) + " " + re.sub(r"[^\w]+", "", (msg.get("subject") or "").lower())
    domain = _sender_domain(sender)
    ats_sender = re.search(r"greenhouse|lever|comeet|workday|ashby|smartrecruiters|icims|jobvite|taleo", domain)
    body = re.sub(r"[^\w]+", "", (msg.get("text") or "")[:3000].lower())
    received = datetime.fromtimestamp(msg["received_ms"] / 1000, TZ).strftime("%Y-%m-%d") if msg.get("received_ms") else ""
    for a in apps:
        k = company_key(a["Company"])
        if len(k) < 3:
            continue
        if received and a.get("Date approved") and received < a["Date approved"]:
            continue                                   # about an earlier application, not this one
        if k in head or k in domain.replace(".", "") or (ats_sender and k in body):
            return a
    return None


def mail_scan(sheet: Sheet, log=print, force: bool = False) -> int:
    """Queue one mail_event task per new company email. Returns how many were queued."""
    now = datetime.now(TZ)
    try:
        if not force and now - datetime.fromisoformat(sheet.kv_get("Health", "mail_scan_at", "")) < SCAN_EVERY:
            return 0
    except ValueError:
        pass
    sheet.load("Applications", "Events", "Queue")
    apps = [a for a in sheet.t("Applications").rows if a["Status"] in ACTIVE]
    after = sheet.kv_get("Health", "mail_after", "") or \
        str(int((now - timedelta(days=FIRST_LOOKBACK_DAYS)).timestamp()))
    newest = int(after)
    queued = 0
    if apps:
        skip = " ".join(f"-from:{d}" for d in alert_senders())
        known = {r["Message ID"] for r in sheet.t("Events").rows}
        q = sheet.t("Queue")
        for mid in reversed(gmail.search(f"after:{after} -in:sent -in:chats {skip}", limit=150)):
            if mid in known or q.find("Task ID", f"mail-{mid}"):
                continue
            m = gmail.message(mid)
            newest = max(newest, m["received_ms"] // 1000)
            app = match_app(m, apps)
            if app:
                from agent.bot import queue_task
                queue_task(sheet, "mail_event", app["Job ID"], app["App ID"], {"message_id": mid}, f"mail-{mid}")
                queued += 1
    sheet.kv_set("Health", "mail_after", str(newest), flush=False)
    sheet.kv_set("Health", "mail_scan_at", now_iso(), flush=False)
    sheet.flush()
    if queued:
        log(f"mail scan: {queued} company email(s) queued")
    return queued


# --- 2. one company email (Mac) ----------------------------------------------------------
CLASSIFY = """An email arrived about a job application to {company} for the role "{role}".
Classify it and summarize it. Answer with JSON only:
{{"type": "acknowledgment|assessment|interview|rejection|offer|other",
  "summary": "one short sentence: what the email says and what (if anything) to do",
  "deadline": "YYYY-MM-DD if the email states a date to act by, else empty"}}
acknowledgment = only confirms the application was received. assessment = a test, home
assignment or online assessment to do. interview = an invitation or scheduling of any interview
or call with the company. rejection = they will not move forward. offer = a job offer.

FROM: {sender}
SUBJECT: {subject}
EMAIL:
{text}"""


def classify(m: dict, app: dict) -> dict:
    out = claude_cli.ask(CLASSIFY.format(company=app["Company"], role=app["Role"], sender=m["from"],
                                         subject=m["subject"], text=m["text"][:6000]))
    found = re.search(r"\{.*\}", out, re.S)
    data = json.loads(found.group(0)) if found else {}
    t = data.get("type") if data.get("type") in TYPE_STATUS else "other"
    return {"type": t, "summary": str(data.get("summary") or "").strip()[:300],
            "deadline": str(data.get("deadline") or "").strip()[:10]}


def apply_event(sheet: Sheet, app: dict, m: dict, c: dict) -> str:
    """Add the Events row and move the Status forward. Returns the new Status."""
    events, apps = sheet.t("Events"), sheet.t("Applications")
    if not events.find("Message ID", m["id"]):
        events.append({"Date": datetime.fromtimestamp(m["received_ms"] / 1000, TZ).isoformat(sep=" ", timespec="minutes"),
                       "App ID": app["App ID"], "Type": c["type"], "Sender": m["from"][:120],
                       "Summary": c["summary"], "Email link": m["link"], "Message ID": m["id"]})
    status = app["Status"]
    changes = {"Last update": now_iso()}
    new = TYPE_STATUS[c["type"]]
    if new == "Rejected" and status not in ("Offer",):
        status = "Rejected"
    elif new and STAGE.get(new, 0) > STAGE.get(status, 0):
        status = new
    if c["type"] == "acknowledgment" and status in ("Approved", "CV ready"):
        status = "Applied"                                     # the email proves the user applied
        day = datetime.fromtimestamp(m["received_ms"] / 1000, TZ)
        changes.update({"Date applied": app["Date applied"] or day.strftime("%Y-%m-%d"),
                        "Follow-up due": app["Follow-up due"] or (day + timedelta(days=7)).strftime("%Y-%m-%d")})
    next_step = {"Assessment": "do the assessment", "Interview": "prepare for the interview", "Rejected": "—",
                 "Offer": "review the offer", "Applied": "wait for a reply"}.get(status, app["Next step"])
    if c["deadline"]:
        next_step += f" (by {c['deadline']})"
    changes.update({"Status": status, "Next step": next_step})
    apps.update(app, **changes)
    sheet.flush()
    return status


def mail_event(sheet: Sheet, task: dict, payload: dict, log) -> str:
    sheet.load("Applications", "Events")
    app = sheet.t("Applications").find("App ID", task["App ID"])
    if not app:
        return "no application"
    if sheet.t("Events").find("Message ID", payload["message_id"]):
        return "already recorded"
    m = gmail.message(payload["message_id"])
    c = classify(m, app)
    before = app["Status"]
    status = apply_event(sheet, app, m, c)
    lines = [f"{TYPE_LABEL[c['type']]} · <b>{tg.esc(app['Company'])}</b> · {tg.esc(app['Role'])}",
             tg.esc(c["summary"])]
    if status != before:
        lines.append(f"Status: {tg.esc(before)} → <b>{tg.esc(status)}</b>")
    # The Gmail app cannot be opened from Telegram (it drops googlegmail:// links; tested
    # 2026-10-24): the subject is shown tap-to-copy, to paste into the app's search.
    sender = re.sub(r"\s*<[^>]*>", "", m["from"]).strip(' "') or m["from"]
    lines.append(f"✉️ From: {tg.esc(sender[:60])}\nSubject: <code>{tg.esc(m['subject'][:150])}</code>")
    if c["type"] in ("assessment", "interview") and status != before:
        lines.append("📚 Preparing interview prep for you, it will come in a few minutes.")
    tg.send_message("\n".join(lines), buttons=tg.link_row(m["link"], "📧 Open email"))
    if c["type"] in ("assessment", "interview") and status != before:
        from agent.bot import queue_task
        queue_task(sheet, "prep", app["Job ID"], app["App ID"], {"company": app["Company"]}, f"prep-{m['id']}")
        sheet.flush()
    return json.dumps(c, ensure_ascii=False)


# --- 3. follow-up reminders --------------------------------------------------------------
def followups(sheet: Sheet, log=print) -> int:
    """Applied rows whose follow-up date has come and that had no real reply: one reminder."""
    now = datetime.now(TZ)
    if now.hour < 9:
        return 0
    today = now.strftime("%Y-%m-%d")
    if sheet.kv_get("Health", "followups_date", "") == today:
        return 0
    sheet.kv_set("Health", "followups_date", today)
    sheet.load("Applications", "Events")
    replied = {e["App ID"] for e in sheet.t("Events").rows if e["Type"] not in ("acknowledgment", "ignored", "")}
    sent = 0
    for a in sheet.t("Applications").rows:
        key = f"followup:{a['App ID']}"
        if a["Status"] != "Applied" or not a["Follow-up due"] or a["Follow-up due"] > today \
                or a["App ID"] in replied or sheet.kv_get("Health", key, ""):
            continue
        sheet.kv_set("Health", key, today)                     # recorded first: never twice
        tg.send_message(f"⏰ It's been 7 days since you applied to <b>{tg.esc(a['Company'])}</b> ({tg.esc(a['Role'])}) "
                        f"with no reply.\nYou could send the recruiter a short follow-up email.",
                        buttons=[[("✍️ Follow-up draft", f"fu:{a['App ID']}")], [("🙅 Not needed", f"funo:{a['App ID']}")]])
        sent += 1
    return sent


FOLLOWUP = """Write a short follow-up email (under 90 words) from {name} about their application
to {company} for "{role}" on {applied}. Polite, natural, specific to the role, not pushy, not
template-like. English. Only facts from this profile summary may be used:
{facts}
Return the subject line on the first line as "Subject: ...", then the body."""


def followup_draft(sheet: Sheet, task: dict, payload: dict, log) -> str:
    sheet.load("Applications")
    a = sheet.t("Applications").find("App ID", task["App ID"])
    if not a:
        return "no application"
    import yaml
    p = config.profile()                                       # never private_never_on_cv
    facts = yaml.safe_dump({"education": p.get("education"), "experience": p.get("experience"),
                            "skills": p["skills"]["strong"], "projects": [x.get("name") for x in p.get("projects", [])],
                            "writing_rules (obey them)": p.get("writing_rules")},
                           allow_unicode=True, sort_keys=False)
    from agent.truth_check import check_text
    problems = []
    for _ in range(3):
        text = claude_cli.ask(FOLLOWUP.format(name=config.name(), company=a["Company"], role=a["Role"],
                                              applied=a["Date applied"], facts=facts[:3000])
                              + (f"\nYour previous draft broke these rules, fix them: {problems}" if problems else ""))
        problems = check_text(text)
        if not problems:
            break
    if problems:
        raise RuntimeError(f"follow-up draft failed the truth check: {problems}")
    tg.send_message(f"✍️ <b>Follow-up draft</b> for {tg.esc(a['Company'])} (you send it, not me):\n\n"
                    f"<pre>{tg.esc(text.strip()[:3000])}</pre>")
    return "sent"


# --- 4. 18:00 digest and 22:00 email -----------------------------------------------------
def _today_rows(sheet: Sheet) -> dict:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    jobs = [r for r in sheet.t("Jobs").rows if r["First seen"][:10] == today]
    apps = sheet.t("Applications").rows
    return {"today": today,
            "found": len(jobs),
            "alerted": [r for r in jobs if r["Decision"] in ("alerted", "alerting", "more_pending", "forwarded")],
            "approved": [a for a in apps if a["Date approved"] == today],
            "applied": [a for a in apps if a["Date applied"] == today],
            "waiting": [a for a in apps if a["Status"] in ("Approved", "CV ready")],
            "due": [a for a in apps if a["Status"] == "Applied" and a["Follow-up due"] and a["Follow-up due"] <= today],
            "events": [e for e in sheet.t("Events").rows if e["Date"][:10] == today]}


def digest(sheet: Sheet, log=print) -> bool:
    now = datetime.now(TZ)
    if now.hour < 18 or sheet.kv_get("Health", "digest_date", "") == now.strftime("%Y-%m-%d"):
        return False
    sheet.load("Jobs", "Applications", "Events")
    d = _today_rows(sheet)
    sheet.kv_set("Health", "digest_date", d["today"])          # recorded first: never twice
    e = tg.esc
    lines = [f"🗒 <b>Today's summary</b> ({d['today']})",
             f"🔎 {d['found']} new jobs checked · {len(d['alerted'])} sent to you",
             f"✅ approved {len(d['approved'])} · 📨 applied {len(d['applied'])}"]
    for a in d["applied"]:
        lines.append(f"   • {e(a['Role'])} · {e(a['Company'])}")
    if d["events"]:
        lines.append(f"✉️ Company replies today: {len(d['events'])}")
    if d["waiting"]:
        lines.append(f"⏳ Waiting for you to apply ({len(d['waiting'])}):")
        lines += [f"   • {e(a['Role'])} · {e(a['Company'])}" for a in d["waiting"][:8]]
    if d["due"]:
        lines.append(f"⏰ Time to follow up ({len(d['due'])}):")
        lines += [f"   • {e(a['Company'])} (applied {e(a['Date applied'])})" for a in d["due"][:8]]
    tg.send_message("\n".join(lines))
    return True


MORNING_HOUR = 9


def morning_jobs(sheet: Sheet, log=print) -> bool:
    """Once a morning: the jobs not handled yet, 10 cards with ➕ More (as /open)."""
    from agent import alerts
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour < MORNING_HOUR or sheet.kv_get("Health", "morning_date", "") == today:
        return False
    sheet.kv_set("Health", "morning_date", today)                # recorded first: never twice
    if sheet.kv_get("Settings", "paused", "no") == "yes":
        return False
    sheet.load("Jobs", "Applications")
    if not alerts.pending_rows(sheet):
        return False
    tg.send_message("☀️ <b>Good morning!</b> These jobs are still waiting for your decision:")
    alerts.send_pending(sheet)
    return True


def daily_email(sheet: Sheet, log=print) -> bool:
    now = datetime.now(TZ)
    if now.hour < 22 or sheet.kv_get("Health", "email_date", "") == now.strftime("%Y-%m-%d"):
        return False
    sheet.load("Jobs", "Applications", "Events")
    d = _today_rows(sheet)
    sheet.kv_set("Health", "email_date", d["today"])           # recorded first: never twice
    if not d["applied"]:
        return False                                           # nothing applied today: no email
    import html as h
    rows = "".join(f'<tr><td>{h.escape(a["Company"])}</td><td>{h.escape(a["Role"])}</td>'
                   f'<td><a href="{h.escape(a["Job link"])}">job</a></td><td>{h.escape(a["CV path"].split("/")[-2] if a["CV path"] else "")}</td></tr>'
                   for a in d["applied"])
    body = (f"<p>Applied today ({d['today']}): {len(d['applied'])}</p>"
            f'<table border="1" cellpadding="6" style="border-collapse:collapse">'
            f"<tr><th>Company</th><th>Role</th><th>Link</th><th>CV folder</th></tr>{rows}</table>"
            f"<p>Follow-up reminders come on Telegram 7 days after each application.</p>")
    gmail.send_to_self(f"Job search: applied today {d['today']} ({len(d['applied'])})", body)
    log(f"daily email sent ({len(d['applied'])} applications)")
    return True
