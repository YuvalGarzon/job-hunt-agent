"""Telegram updates: every consumer writes them to the Inbox tab first (keyed by update_id),
then handles them from the Inbox. Handlers check state before acting, so handling an update
twice never duplicates anything. Only the owner's own chat is ever served.
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import alerts, config, sources
from agent import telegram as tg
from agent.sheet import TZ, Sheet, now_iso
from agent.sources import board_mail

HELP = """<b>Commands</b>
/open – jobs you haven't handled yet + CVs waiting for you to apply
/mail – check your email now (company replies and job alerts)
/status – overall status
/today – what happened today
/threshold &lt;number&gt; – the fit score a job needs to be sent to you (now: {thr})
/pause – pause the job search
/resume – resume the job search
/sources – where jobs come from
/job – check a job you found (link, text, screenshot, PDF or Word)
/cv – a tailored CV for a job you found (same)
/prep &lt;company&gt; – interview prep
/answer – an answer to an application-form question (text or screenshot), with a bank of saved answers
/skipped – jobs you skipped and CVs you cancelled, to bring back
/cancel – cancel what I'm waiting for (a fix, a job, a question, a mock interview)
/help – this help

📥 Saw a job? Send it to me in any form: a link, a post forwarded from a group, its text, a screenshot or photo (several together too), or a PDF / Word file. I'll read it, look for it on the company's site and send you a card with Approve / Skip.
↩️ You can Reply to any of my messages and I'll know what you're talking about, e.g. "I already applied" or "this job is closed".
🔒 I check once a day whether jobs from company sites are still open. For jobs from LinkedIn, job boards and Telegram, tap 🔒 Closed.
💬 Any other text is a free chat with me: questions about your search, a draft message to a recruiter, and more."""

LATER = "This isn't ready yet. It will be built later."


def app_id_for(job_id: str) -> str:
    return "A" + job_id[:8]


# --- Inbox -----------------------------------------------------------------------------
def record(sheet: Sheet, updates: list, by: str) -> int:
    inbox = sheet.t("Inbox")
    have = {r["Update ID"] for r in inbox.rows}
    n = 0
    for u in updates:
        uid = str(u["update_id"])
        if uid in have:
            continue
        kind = "callback" if "callback_query" in u else "message" if "message" in u else "other"
        inbox.append({"Update ID": uid, "Received": now_iso(), "Kind": kind,
                      "Data": json.dumps(u, ensure_ascii=False)[:45000], "Handled": "", "By": by})
        have.add(uid)
        n += 1
    sheet.flush()
    return n


def handle_pending(sheet: Sheet, by: str, mac_online: bool, log=print) -> int:
    # Fresh data, but only what is needed: other tabs load on first use (sheet.t), so /threshold
    # does not wait for the 1,700-row Jobs tab.
    sheet.forget()
    sheet.load("Inbox", "Settings", "Health")
    inbox = sheet.t("Inbox")
    pending = sorted([r for r in inbox.rows if not r["Handled"]], key=lambda r: int(r["Update ID"]))
    for r in pending:
        try:
            t0 = time.time()
            handle(sheet, json.loads(r["Data"]), by=by, mac_online=mac_online, log=log)
            if time.time() - t0 > 8:
                log(f"slow update {r['Update ID']}: handled in {time.time() - t0:.0f}s")
            inbox.update(r, Handled="Y", **{"Handled at": now_iso(), "By": by})
        except tg.TelegramError as e:
            log(f"telegram error on update {r['Update ID']}: {e}")
            inbox.update(r, Handled="error", **{"Handled at": now_iso(), "By": by})
        sheet.flush()
    return len(pending)


# --- Dispatch --------------------------------------------------------------------------
def _from_owner(u: dict) -> bool:
    owner = str(tg.chat_id())
    if "callback_query" in u:
        cq = u["callback_query"]
        return str(cq.get("from", {}).get("id")) == owner
    if "message" in u:
        return str(u["message"].get("chat", {}).get("id")) == owner
    return False


def handle(sheet: Sheet, u: dict, by: str, mac_online: bool, log=print) -> None:
    if not _from_owner(u):
        log("ignored an update from another chat")
        return
    ctx = {"by": by, "mac_online": mac_online or by == "mac", "update_id": u.get("update_id"),
           "msg": u.get("message") or {}}
    if "callback_query" in u:
        cq = u["callback_query"]
        data = cq.get("data", "")
        action, _, arg = data.partition(":")
        msg_id = cq.get("message", {}).get("message_id")
        tg.keep_links(msg_id, cq.get("message", {}).get("reply_markup"))
        fn = CALLBACKS.get(action)
        if fn:
            fn(sheet, arg, msg_id, ctx)
        tg.answer_callback(cq["id"])
        return
    msg = u.get("message", {})
    text = (msg.get("text") or msg.get("caption") or "").strip()
    from agent import intake
    media = intake.media_in(msg)
    replied_msg = msg.get("reply_to_message") or {}
    replied = replied_msg.get("message_id")
    if not text.startswith("/") and (text or media) and on_answer_input(sheet, msg, text, media, replied, ctx):
        return
    reply_job = job_for_message(sheet, replied) if replied and not text.startswith("/") else ""
    if reply_job and (media or intake.looks_like_posting(text)):
        # a reply to a job's card or CV with the posting itself (pasted, a screenshot, a file):
        # it becomes that job's description, and the CV is built from it
        on_job_post(sheet, text, intake.urls_in(msg), u.get("update_id"), ctx, media=media,
                    group=msg.get("media_group_id", ""), for_job=reply_job)
        return
    if media and not text.startswith("/"):              # a screenshot, photo, PDF or Word file
        on_job_post(sheet, text, intake.urls_in(msg), u.get("update_id"), ctx,
                    media=media, group=msg.get("media_group_id", ""))
        return
    if not text:
        kind = intake.unreadable_kind(msg)
        if kind:
            tg.send_message(f"🤷 I got {tg.esc(kind)}, but I can't read that.\n"
                            "You can send me a job as a link, text, forwarded post, screenshot, photo, PDF or Word file.")
        return
    urls = intake.urls_in(msg)
    if not text.startswith("/") and (intake.is_forward(msg) or urls or _waiting_for_job(sheet)):
        on_job_post(sheet, text, urls, u.get("update_id"), ctx)
        return
    if replied and not text.startswith("/") and on_reply(sheet, replied, text, ctx):
        return
    if replied:                                       # a reply to any bot message: chat knows what it answers
        ctx["job_id"] = reply_job or job_for_message(sheet, replied)
        ctx["replied_text"] = (replied_msg.get("text") or replied_msg.get("caption") or "")[:1500]
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        fn = COMMANDS.get(cmd.split("@")[0].lower())
        if fn:
            fn(sheet, arg.strip(), ctx)
        else:
            tg.send_message("Unknown command. /help")
        return
    on_text(sheet, text, ctx)


# --- Job buttons -----------------------------------------------------------------------
def queue_task(sheet: Sheet, task_type: str, job_id: str, app_id: str, payload: dict, task_id: str) -> bool:
    q = sheet.t("Queue")
    if q.find("Task ID", task_id):
        return False
    q.append({"Task ID": task_id, "Type": task_type, "Job ID": job_id, "App ID": app_id,
              "Payload": json.dumps(payload, ensure_ascii=False), "Status": "pending", "Attempts": 0,
              "Created": now_iso(), "Updated": now_iso()})
    return True


def same_job_app(sheet: Sheet, company: str, title: str, job_id: str = ""):
    """An application (other than this job's own) for the same company and nearly the same title:
    the same opening reached through another source or link."""
    from agent.sources.linkedin import company_key, same_title
    k = company_key(company)
    for a in sheet.t("Applications").rows:
        if a["Job ID"] != job_id and a["Status"] != "Withdrawn" and company_key(a["Company"]) == k \
                and same_title(a["Role"], title):
            return a
    return None


def on_approve(sheet: Sheet, job_id: str, msg_id, ctx, confirmed: bool = False) -> None:
    jobs, apps = sheet.t("Jobs"), sheet.t("Applications")
    job = jobs.find("Job ID", job_id)
    if not job:
        tg.send_message("I couldn't find that job in the Sheet.")
        return
    app_id = app_id_for(job_id)
    app = apps.find("App ID", app_id)
    twin = None if (app or confirmed) else same_job_app(sheet, job["Company"], job["Title"], job_id)
    if twin:
        when = f"you applied on {twin['Date applied']}" if twin["Date applied"] else f"you approved it on {twin['Date approved']} (status: {twin['Status']})"
        tg.send_message(f"⚠️ This looks like the same job: <b>{tg.esc(twin['Role'])}</b> · {tg.esc(twin['Company'])}. "
                        f"Already {tg.esc(when)}.\nBuild another CV anyway?",
                        buttons=[[("✅ Yes, it's another job", f"apy:{job_id}"), ("⏭ No, skip", f"sk:{job_id}")]])
        return
    if not app:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        apps.append({"App ID": app_id, "Company": job["Company"], "Role": job["Title"],
                     "Location": job["City"] or job["Location"][:60], "Source": job["Source"],
                     "Job link": job["URL"], "Fit score": job["Score"], "Date found": job["First seen"][:10],
                     "Date approved": today, "Cover letter (Y/N)": "N", "Status": "Approved",
                     "Last update": now_iso(), "Next step": "tailored CV", "Job ID": job_id})
        jobs.update(job, **{"User action": "approved", "Updated": now_iso()})
        queue_task(sheet, "build_cv", job_id, app_id, {"mode": "new"}, f"cv-{app_id}-1")
        sheet.flush()
        if ctx["mac_online"]:
            tg.send_message(f"✅ Approved: <b>{tg.esc(job['Title'])}</b> · {tg.esc(job['Company'])}\nBuilding a tailored CV…")
        else:
            tg.send_message(f"✅ Approved: <b>{tg.esc(job['Title'])}</b> · {tg.esc(job['Company'])}\n"
                            "⏳ Queued: the CV will come when the Mac is back.")
    if msg_id:
        tg.edit_buttons(msg_id, [[("✅ Approved", "noop:")]])


def on_skip(sheet: Sheet, job_id: str, msg_id, ctx) -> None:
    job = sheet.t("Jobs").find("Job ID", job_id)
    if job and job["User action"] != "approved":
        sheet.t("Jobs").update(job, **{"User action": "skipped", "Updated": now_iso()})
        sheet.flush()
    if msg_id:
        from agent import learn
        tg.edit_buttons(msg_id, [[("⏭ Skipped · ↩️ Undo", f"unsk:{job_id}")]] + learn.reason_buttons(job_id))


def on_skip_reason(sheet: Sheet, arg: str, msg_id, ctx) -> None:
    """skr:<reason>:<job id>, offered only after Skip. Later searches learn from it (agent/learn.py)."""
    from agent import learn
    code, _, job_id = arg.partition(":")
    job = sheet.t("Jobs").find("Job ID", job_id)
    if not job or code not in learn.REASONS:
        return
    learn.record(sheet, job, code)
    sheet.flush()
    if msg_id:
        tg.edit_buttons(msg_id, [[(f"⏭ Skipped · {learn.REASONS[code]} · ↩️ Undo", f"unsk:{job_id}")]])
    if code == "co":
        tg.send_message(f"🏢 I won't send you more jobs from <b>{tg.esc(job['Company'])}</b>.",
                        buttons=[[("↩️ Keep sending its jobs", f"unbl:{job_id}")]])


def on_unblock(sheet: Sheet, job_id: str, msg_id, ctx) -> None:
    from agent import learn
    job = sheet.t("Jobs").find("Job ID", job_id)
    if job:
        learn.block(sheet, job["Company"], on=False)
        sheet.flush()
        tg.send_message(f"↩️ I'll keep sending jobs from <b>{tg.esc(job['Company'])}</b>.")
    if msg_id:
        tg.edit_buttons(msg_id, [])


def on_company_cv(sheet: Sheet, key: str, msg_id, ctx) -> None:
    from agent import scout
    sheet.load("Boards")
    scout.general_cv(sheet, key, ctx)


def on_company_drop(sheet: Sheet, key: str, msg_id, ctx) -> None:
    from agent import scout
    sheet.load("Boards")
    scout.drop(sheet, key)
    if msg_id:
        tg.edit_buttons(msg_id, [[("🚫 Not interested", "noop:")]])


def on_answer_save(sheet, qid, msg_id, ctx):
    from agent import answers
    row = answers.save(sheet, qid)
    if row and msg_id:
        tg.edit_buttons(msg_id, [[("✅ Saved to your answers", "noop:")]])


def on_answer_redo(mode):
    def fn(sheet, qid, msg_id, ctx):
        from agent import answers
        if not answers.for_message(sheet, msg_id) and not sheet.t("Answers").find("Q ID", qid):
            return
        queue_answer(sheet, {"qid": qid, "mode": mode}, f"answer-{mode}-{qid}-{ctx.get('update_id') or now_iso()}", ctx)
    return fn


def on_unskip(sheet: Sheet, job_id: str, msg_id, ctx) -> None:
    """Undo a Skip: the job is open again, with its Approve / Skip buttons back."""
    job = sheet.t("Jobs").find("Job ID", job_id)
    if not job:
        tg.send_message("I couldn't find that job in the Sheet.")
        return
    if job["User action"] == "skipped":
        if job.get("Skip reason") == "co":
            from agent import learn
            learn.block(sheet, job["Company"], on=False)
        sheet.t("Jobs").update(job, **{"User action": "", "Skip reason": "", "Updated": now_iso()})
        sheet.flush()
    if msg_id:
        tg.edit_buttons(msg_id, alerts.card_buttons(job))
    tg.send_message(f"↩️ Skip undone: <b>{tg.esc(job['Title'])}</b> · {tg.esc(job['Company'])} is back on your open jobs.")


def cmd_skipped(sheet, arg, ctx):
    from datetime import datetime as _dt
    since = (_dt.now(TZ) - timedelta(days=14)).strftime("%Y-%m-%d")
    rows = [r for r in sheet.t("Jobs").rows if r["User action"] == "skipped" and r["Updated"][:10] >= since]
    cancelled = [a for a in sheet.t("Applications").rows if a["Status"] == "Withdrawn" and a["Last update"][:10] >= since
                 and not a["Company"].startswith("TEST")]
    for a in cancelled:
        where = f"\n{config.where_saved(a['CV path'])}" if a["CV path"] else ""
        tg.send_message(f"🚫 <b>{tg.esc(a['Role'])}</b> · {tg.esc(a['Company'])}\nYou cancelled this application (the files were kept).{where}",
                        buttons=[[("↩️ Resume", f"cvres:{a['App ID']}")]])
    if not rows:
        if not cancelled:
            tg.send_message("You haven't skipped any jobs or cancelled any applications in the last two weeks.")
        return
    rows = sorted(rows, key=lambda r: r["Updated"], reverse=True)[:15]
    tg.send_message(f"↩️ <b>{len(rows)} jobs you skipped</b> (newest first). Tap to bring a job back:")
    for i, r in enumerate(rows, 1):
        send_job_card(sheet, r, i)


def on_more(sheet: Sheet, batch_id: str, msg_id, ctx) -> None:
    if msg_id:
        tg.edit_buttons(msg_id, [])
    alerts.send_more(sheet, batch_id)


# --- CV buttons ------------------------------------------------------------------------
def cv_buttons(app_id: str) -> list:
    return [[("✅ Good", f"good:{app_id}"), ("🔁 Try again", f"retry:{app_id}")],
            [("✏️ Fix", f"fix:{app_id}"), ("📝 Cover letter?", f"cl:{app_id}")],
            [("📨 I applied", f"applied:{app_id}"), ("🚫 Cancel", f"cvx:{app_id}"), ("🔒 Closed", f"aclosed:{app_id}")]]


def on_cv_cancel(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    """Not applying to this job after all. Nothing is deleted: the CV, the cover letter and the
    folder stay; the status becomes Withdrawn until Resume."""
    app = sheet.t("Applications").find("App ID", app_id)
    if not app:
        tg.send_message("I couldn't find that application in the Sheet.")
        return
    if app["Status"] != "Withdrawn":
        sheet.t("Applications").update(app, Status="Withdrawn", **{"Next step": "—", "Last update": now_iso()})
        sheet.flush()
    if msg_id:
        tg.edit_buttons(msg_id, [[("🚫 Cancelled · ↩️ Resume", f"cvres:{app_id}")]])
    where = f"\n{config.where_saved(app['CV path'])}" if app["CV path"] else ""
    tg.send_message(f"🚫 Cancelled: <b>{tg.esc(app['Role'])}</b> · {tg.esc(app['Company'])}. "
                    f"Nothing was deleted, the files stay.{where}\nBring it back with ↩️ Resume.")


def on_cv_resume(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    app = sheet.t("Applications").find("App ID", app_id)
    if not app:
        tg.send_message("I couldn't find that application in the Sheet.")
        return
    if app["Status"] == "Withdrawn":
        status = "Applied" if app["Date applied"] else "CV ready" if app["CV path"] else "Approved"
        nxt = {"Applied": "wait for a reply", "CV ready": "review and apply", "Approved": "tailored CV"}[status]
        sheet.t("Applications").update(app, Status=status, **{"Next step": nxt, "Last update": now_iso()})
        sheet.flush()
    if msg_id:
        tg.edit_buttons(msg_id, cv_buttons(app_id))
    tg.send_message(f"↩️ Back: <b>{tg.esc(app['Role'])}</b> · {tg.esc(app['Company'])} (status: {tg.esc(app['Status'])}).")


def _next_cv_task_id(sheet: Sheet, app_id: str) -> str:
    n = len([r for r in sheet.t("Queue").rows if r["Task ID"].startswith(f"cv-{app_id}-")])
    return f"cv-{app_id}-{n + 1}"


def on_good(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    app = sheet.t("Applications").find("App ID", app_id)
    if app and app["Status"] in ("CV ready", "Approved"):
        sheet.t("Applications").update(app, **{"Next step": "apply", "Last update": now_iso()})
        sheet.flush()
    tg.send_message("👍 Great. After you apply, tap 📨 I applied.")


def on_retry(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    app = sheet.t("Applications").find("App ID", app_id)
    if not app:
        return
    pending = [r for r in sheet.t("Queue").rows if r["App ID"] == app_id and r["Type"] == "build_cv"
               and r["Status"] in ("pending", "in_progress")]
    if pending:
        tg.send_message("Already building a version of this CV. One moment.")
        return
    queue_task(sheet, "build_cv", app["Job ID"], app_id, {"mode": "retry"}, _next_cv_task_id(sheet, app_id))
    sheet.flush()
    tg.send_message("🔁 Building a new version…" if ctx["mac_online"] else "🔁 Queued: the new version will come when the Mac is back.")


def on_fix(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    sheet.kv_set("Settings", "awaiting_fix", f"{app_id}|{now_iso()}")
    tg.send_message("✏️ Tell me what to change in the CV, in your own words.", buttons=[[("❌ Cancel", "cancel:")]])


def cover_languages() -> list:
    """The languages a cover letter can be written in (settings.yaml cover_letter_languages)."""
    return [str(x) for x in config.settings().get("cover_letter_languages") or ["English"]][:3]


def on_cover_letter(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    langs = cover_languages()
    tg.send_message("📝 Write a cover letter for this job?",
                    buttons=[[(f"✅ Yes, in {lang}", f"clw:{i}:{app_id}") for i, lang in enumerate(langs)],
                             [("No", "noop:")]])


def _cl_task(sheet: Sheet, app_id: str, payload: dict, ctx, note: str) -> None:
    app = sheet.t("Applications").find("App ID", app_id)
    if not app:
        tg.send_message("I couldn't find that application in the Sheet.")
        return
    if any(t["Type"] == "cover_letter" and t["App ID"] == app_id and t["Status"] in ("pending", "in_progress")
           for t in sheet.t("Queue").rows):
        tg.send_message("📝 Already writing a letter for this job, one moment.")
        return
    queue_task(sheet, "cover_letter", app["Job ID"], app_id, payload, f"cl-{app_id}-{ctx.get('update_id') or now_iso()}")
    sheet.flush()
    tg.send_message(note if ctx["mac_online"] else "📝 Queued: the letter will come when the Mac is back.")


def on_cl_write(sheet: Sheet, arg: str, msg_id, ctx) -> None:
    lang, _, app_id = arg.partition(":")
    if msg_id:
        tg.edit_buttons(msg_id, [])
    _cl_task(sheet, app_id, {"lang": lang, "mode": "new"}, ctx, "📝 Writing a cover letter…")


def on_cl_ok(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    app = sheet.t("Applications").find("App ID", app_id)
    if app:
        sheet.t("Applications").update(app, **{"Cover letter (Y/N)": "Y", "Last update": now_iso()})
        sheet.flush()
    if msg_id:
        tg.edit_buttons(msg_id, [[("✅ Approved", "noop:")]])
    folder = Path(app["CV path"]).parent if app and app["CV path"] else None
    where = f"\n{config.where_saved(config.ROOT / folder / 'Cover Letter.txt')}" if folder else ""
    tg.send_message(f"👍 Saved as Cover Letter.txt in the job's folder.{where}")


def on_cl_retry(sheet: Sheet, arg: str, msg_id, ctx) -> None:
    lang, _, app_id = arg.partition(":")
    _cl_task(sheet, app_id, {"lang": lang, "mode": "retry"}, ctx, "🔁 Writing another version…")


def on_cl_fix(sheet: Sheet, arg: str, msg_id, ctx) -> None:
    lang, _, app_id = arg.partition(":")
    sheet.kv_set("Settings", "awaiting_fix", f"cl:{lang}:{app_id}|{now_iso()}")
    tg.send_message("✏️ Tell me what to change in the letter, in your own words.", buttons=[[("❌ Cancel", "cancel:")]])


def on_mock(sheet: Sheet, key: str, msg_id, ctx) -> None:
    from agent import writing
    writing.start_mock(sheet, key)
    queue_task(sheet, "chat", "", "", {"text": "Start the mock interview: ask the first question."},
               f"chat-mock-{ctx.get('update_id') or now_iso()}")
    sheet.flush()
    tg.send_message("🎤 The mock interview starts. Answer in the chat as usual. To finish, write: stop", buttons=[[("⏹ End interview", "mockend:")]])


def on_mock_end(sheet: Sheet, arg: str, msg_id, ctx) -> None:
    queue_task(sheet, "chat", "", "", {"text": "stop"}, f"chat-mockend-{ctx.get('update_id') or now_iso()}")
    sheet.flush()


def on_applied(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    apps = sheet.t("Applications")
    app = apps.find("App ID", app_id)
    if not app:
        tg.send_message("I couldn't find that application in the Sheet.")
        return
    if app["Status"] != "Applied" and not app["Date applied"]:
        today = datetime.now(TZ)
        due = (today + timedelta(days=7)).strftime("%Y-%m-%d")
        apps.update(app, **{"Status": "Applied", "Date applied": today.strftime("%Y-%m-%d"),
                            "Follow-up due": due, "Next step": "wait for a reply", "Last update": now_iso()})
        sheet.flush()
        tg.send_message(f"📨 Recorded: you applied to <b>{tg.esc(app['Company'])}</b> ✅\nIf there's no reply, I'll remind you on {due}.")
    else:
        tg.send_message("This is already marked as applied.")
    if msg_id:
        tg.edit_buttons(msg_id, [[("📨 Applied ✅", "noop:"), ("📝 Cover letter?", f"cl:{app_id}")]])


def on_noop(sheet, arg, msg_id, ctx):
    pass


def on_job_applied(sheet: Sheet, job_id: str, msg_id, ctx) -> None:
    """The user applied to a job (said so in chat) that has no application row yet: record it."""
    jobs, apps = sheet.t("Jobs"), sheet.t("Applications")
    job = jobs.find("Job ID", job_id)
    if not job:
        tg.send_message("I couldn't find that job in the Sheet.")
        return
    app = apps.find("Job ID", job_id)
    if not app:
        app_id = app_id_for(job_id)
        apps.append({"App ID": app_id, "Company": job["Company"], "Role": job["Title"],
                     "Location": job["City"] or job["Location"][:60], "Source": job["Source"],
                     "Job link": job["URL"], "Fit score": job["Score"], "Date found": job["First seen"][:10],
                     "Date approved": datetime.now(TZ).strftime("%Y-%m-%d"), "Cover letter (Y/N)": "N",
                     "Status": "Approved", "Last update": now_iso(), "Job ID": job_id})
        jobs.update(job, **{"User action": "approved", "Updated": now_iso()})
        sheet.flush()
        app = apps.find("App ID", app_id)
    on_applied(sheet, app["App ID"], msg_id, ctx)


def on_closed(sheet: Sheet, job_id: str, msg_id, ctx) -> None:
    """The user says the job is closed (the agent cannot check LinkedIn, job boards or posts)."""
    from agent import openness
    job = sheet.t("Jobs").find("Job ID", job_id)
    if not job:
        tg.send_message("I couldn't find that job in the Sheet.")
        return
    if not job["Closed"]:
        openness.mark_closed(sheet, job, "you", notify=False)
        sheet.flush()
    if msg_id:
        tg.edit_buttons(msg_id, [[("🔒 Closed · ↩️ Undo", f"reopen:{job_id}")]])
    tg.send_message(f"🔒 Marked as closed: <b>{tg.esc(job['Title'])}</b> · {tg.esc(job['Company'])}. "
                    "It won't show in /open anymore.")


def on_app_closed(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    app = sheet.t("Applications").find("App ID", app_id)
    if app and app["Job ID"]:
        on_closed(sheet, app["Job ID"], msg_id, ctx)
    else:
        tg.send_message("I couldn't find that application in the Sheet.")


def on_reopen(sheet: Sheet, job_id: str, msg_id, ctx) -> None:
    from agent import openness
    job = sheet.t("Jobs").find("Job ID", job_id)
    if not job:
        tg.send_message("I couldn't find that job in the Sheet.")
        return
    openness.reopen(sheet, job)
    sheet.flush()
    if msg_id:
        tg.edit_buttons(msg_id, [[("↩️ Open again", "noop:")]])
    tg.send_message("↩️ The job is open again:")
    send_job_card(sheet, job)


def on_followup(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    app = sheet.t("Applications").find("App ID", app_id)
    if not app:
        return
    mine = [t for t in sheet.t("Queue").rows if t["Type"] == "followup_draft" and t["App ID"] == app_id]
    if any(t["Status"] in ("pending", "in_progress") for t in mine):
        tg.send_message("✍️ Already writing a draft, it will come in a moment.")
    elif queue_task(sheet, "followup_draft", app["Job ID"], app_id, {}, f"fu-{app_id}-{ctx.get('update_id') or now_iso()}"):
        sheet.flush()
        tg.send_message(("✍️ Writing a new draft…" if mine else "✍️ Writing a draft…") if ctx["mac_online"]
                        else "✍️ The draft will come when the Mac is back.")
    if msg_id:
        tg.edit_buttons(msg_id, [])


def on_followup_no(sheet: Sheet, app_id: str, msg_id, ctx) -> None:
    if msg_id:
        tg.edit_buttons(msg_id, [[("👌 OK, no follow-up", "noop:")]])


def on_threshold_pick(sheet: Sheet, n: str, msg_id, ctx) -> None:
    if msg_id:
        tg.edit_buttons(msg_id, [])
    cmd_threshold(sheet, n, ctx)


def on_prep_pick(sheet: Sheet, company: str, msg_id, ctx) -> None:
    if msg_id:
        tg.edit_buttons(msg_id, [])
    cmd_prep(sheet, company, ctx)


def cancel_waiting(sheet: Sheet) -> list:
    """Stop waiting for anything: a fix text, a link after /cv, a mock interview."""
    stopped = []
    for key, what in (("awaiting_fix", "a fix"), ("awaiting_link", "a job"), ("awaiting_answer", "a question"),
                      ("mock", "the mock interview")):
        if sheet.kv_get("Settings", key, ""):
            sheet.kv_set("Settings", key, "", flush=False)
            stopped.append(what)
    sheet.flush()
    return stopped


def on_cancel(sheet: Sheet, arg: str, msg_id, ctx) -> None:
    cancel_waiting(sheet)
    if msg_id:
        tg.edit_buttons(msg_id, [[("❌ Cancelled", "noop:")]])


def cmd_cancel(sheet, arg, ctx):
    stopped = cancel_waiting(sheet)
    tg.send_message("❌ Cancelled: " + ", ".join(stopped) + "." if stopped else "Nothing is waiting to be cancelled.")


def on_pending_more(sheet: Sheet, offset: str, msg_id, ctx) -> None:
    if msg_id:
        tg.edit_buttons(msg_id, [])
    alerts.send_pending(sheet, int(offset or 0))


def on_approve_confirmed(sheet: Sheet, job_id: str, msg_id, ctx) -> None:
    if msg_id:
        tg.edit_buttons(msg_id, [])
    on_approve(sheet, job_id, None, ctx, confirmed=True)


CALLBACKS = {"cvx": on_cv_cancel, "cvres": on_cv_resume, "unsk": on_unskip, "skr": on_skip_reason, "unbl": on_unblock,
             "cocv": on_company_cv, "anss": on_answer_save, "ansh": on_answer_redo("shorter"),
             "ansr": on_answer_redo("retry"), "cono": on_company_drop, "apy": on_approve_confirmed, "pend": on_pending_more, "cancel": on_cancel, "thr": on_threshold_pick, "prep": on_prep_pick, "clw": on_cl_write, "clok": on_cl_ok, "clre": on_cl_retry, "clfx": on_cl_fix, "mock": on_mock,
             "mockend": on_mock_end, "fu": on_followup, "funo": on_followup_no, "ap": on_approve, "sk": on_skip, "more": on_more, "good": on_good, "retry": on_retry,
             "fix": on_fix, "cl": on_cover_letter, "applied": on_applied, "noop": on_noop,
             "japplied": on_job_applied, "closed": on_closed, "aclosed": on_app_closed, "reopen": on_reopen,
             "mailnow": lambda sheet, arg, msg_id, ctx: cmd_mail(sheet, "", ctx)}


# --- Replies to the bot's own messages ---------------------------------------------------
RESEND = re.compile(r"again|resend|send", re.I)


def send_job_card(sheet: Sheet, job: dict, number: int = None) -> dict:
    """A job sent again: always with the buttons that fit its state now."""
    text = alerts.job_text(job, number or int(job["Rank"] or 1))
    action = job["User action"]
    if job.get("Closed"):
        text += "\n🔒 <i>This job is closed.</i>"
        buttons = [[("↩️ It's still open", f"reopen:{job['Job ID']}")]]
    elif action == "skipped":
        text += "\n⏭ <i>You skipped this job.</i>"
        buttons = [[("✅ Approve", f"apy:{job['Job ID']}"), ("↩️ Undo skip", f"unsk:{job['Job ID']}")]]
    elif action == "approved":
        app = sheet.t("Applications").find("App ID", app_id_for(job["Job ID"]))
        if app and app["Status"] == "Withdrawn":
            text += "\n🚫 <i>You cancelled the application to this job (the files were kept).</i>"
            buttons = [[("↩️ Resume", f"cvres:{app['App ID']}")]]
        elif app:
            text += f"\n✅ <i>Approved · status: {tg.esc(app['Status'])}</i>"
            buttons = cv_buttons(app["App ID"])
        else:
            buttons = alerts.card_buttons(job)
    else:
        buttons = alerts.card_buttons(job)
    msg = tg.send_message(text, buttons=buttons + tg.link_row(job.get("URL", "")))
    sheet.t("Jobs").update(job, **{"Alert msg ID": msg["message_id"], "Updated": now_iso()})
    sheet.flush()
    return msg


def job_for_message(sheet: Sheet, msg_id) -> str:
    """The job a bot message was about: a job card, or a CV sent for it ('' if neither)."""
    job = sheet.t("Jobs").find("Alert msg ID", msg_id)
    if job:
        return job["Job ID"]
    for t in sheet.t("Queue").rows:
        if t["Type"] == "build_cv" and f'"sent_msg": {msg_id}' in t["Result"]:
            return t["Job ID"]
    return ""


def on_reply(sheet: Sheet, replied_id: int, text: str, ctx) -> bool:
    """A reply to a job alert or a CV: resend it when asked. Returns True if handled."""
    job = sheet.t("Jobs").find("Alert msg ID", replied_id)
    if job:
        if RESEND.search(text) and len(text) < 80:      # "send it again", not a pasted posting
            send_job_card(sheet, job)
            return True
        return False
    for t in sheet.t("Queue").rows:
        if t["Type"] == "build_cv" and f'"sent_msg": {replied_id}' in t["Result"] and RESEND.search(text) \
                and len(text) < 80:
            from agent import config
            path = json.loads(t["Result"]).get("pdf")
            app = sheet.t("Applications").find("App ID", t["App ID"])
            if path and (config.ROOT / path).exists():
                tg.send_document(config.ROOT / path, caption=f"📄 {tg.esc(app['Role'] if app else '')} · "
                                 f"{tg.esc(app['Company'] if app else '')}",
                                 buttons=cv_buttons(t["App ID"]) + tg.link_row(app["Job link"] if app else ""))
                return True
    return False


# --- Text and commands -----------------------------------------------------------------
def on_text(sheet: Sheet, text: str, ctx) -> None:
    waiting = sheet.kv_get("Settings", "awaiting_fix", "")
    if waiting:
        app_id, _, since = waiting.partition("|")
        cl_lang = ""
        if app_id.startswith("cl:"):
            _, cl_lang, app_id = app_id.split(":", 2)
        try:
            fresh = datetime.now(timezone.utc) - datetime.fromisoformat(since) < timedelta(hours=24)
        except ValueError:
            fresh = False
        sheet.kv_set("Settings", "awaiting_fix", "", flush=False)
        app = sheet.t("Applications").find("App ID", app_id)
        if fresh and app and cl_lang:
            _cl_task(sheet, app_id, {"lang": cl_lang, "mode": "fix", "feedback": text[:2000]}, ctx,
                     "✏️ Got it. Fixing the letter…")
            return
        if fresh and app:
            queue_task(sheet, "build_cv", app["Job ID"], app_id, {"mode": "fix", "feedback": text[:2000]},
                       _next_cv_task_id(sheet, app_id))
            sheet.flush()
            tg.send_message("✏️ Got it. Building a fixed version…" if ctx["mac_online"]
                            else "✏️ Got it. The fix is queued, and the version will come when the Mac is back.")
            return
        sheet.flush()
    queue_task(sheet, "chat", "", "", {"text": text[:4000], "job_id": ctx.get("job_id", ""),
                                       "replied_text": ctx.get("replied_text", "")},
               f"chat-{ctx.get('update_id') or now_iso()}")
    sheet.flush()
    if not ctx["mac_online"]:
        tg.send_message("💬 The Mac is off right now. I'll answer when it's back.")


def _waiting_for_job(sheet: Sheet) -> bool:
    """A bare /job or /cv in the last 30 minutes: the next message is the job."""
    waiting, _, since = sheet.kv_get("Settings", "awaiting_link", "").partition("|")
    try:
        return bool(waiting) and datetime.now(TZ) - datetime.fromisoformat(since) < timedelta(minutes=30)
    except ValueError:
        return False


def on_job_post(sheet: Sheet, text: str, urls: list, update_id, ctx, media: list = None, group: str = "",
                for_job: str = "") -> None:
    """A post forwarded from a Telegram group, a bare link, /job <link>, or a screenshot / photo /
    file of a job post: checked on the Mac. Right after a bare /cv, the job also gets a tailored CV.
    Photos sent together (an album) arrive one message each and become one task."""
    if group:
        q = sheet.t("Queue")
        task = q.find("Task ID", f"intake-mg-{group}")
        if task and task["Status"] == "pending":
            payload = json.loads(task["Payload"] or "{}")
            if not any(m["file_id"] == media[0]["file_id"] for m in payload.get("media", [])):
                payload["media"] = payload.get("media", []) + media
                payload["text"] = "\n\n".join(t for t in (payload.get("text", ""), text) if t)[:6000]
                payload["urls"] = list(dict.fromkeys(payload.get("urls", []) + urls))[:10]
                q.update(task, Payload=json.dumps(payload, ensure_ascii=False), Updated=now_iso())
                sheet.flush()
            return
        if not task:
            update_id = f"mg-{group}"
    approve = False
    waiting = sheet.kv_get("Settings", "awaiting_link", "").partition("|")[0]
    if waiting:
        approve = waiting == "cv" and _waiting_for_job(sheet)
        sheet.kv_set("Settings", "awaiting_link", "", flush=False)
    payload = {"text": text[:6000], "urls": urls[:10]}
    if approve:
        payload["approve"] = True
    if media:
        payload["media"] = media
    if group:
        payload["group"] = group
    if for_job:
        payload["for_job"] = for_job
    if not queue_task(sheet, "intake", for_job, "", payload, f"intake-{update_id}"):
        return                                         # the same update again
    sheet.flush()
    if for_job:
        job = sheet.t("Jobs").find("Job ID", for_job)
        tg.send_message(f"📝 Got the job description of <b>{tg.esc(job['Title'])}</b> · {tg.esc(job['Company'])}. "
                        + ("Saving it, updating the score and building a CV from it…" if ctx["mac_online"]
                           else "The Mac is off, I'll handle it when it's back."))
        return
    what = "the image" if media and media[0]["mime"].startswith("image/") else "the file" if media else ""
    tg.send_message((f"📥 Got it. Reading {what} and checking the job…" if what else "📥 Got it. Checking the job…")
                    if ctx["mac_online"] else "📥 Got it. The Mac is off, I'll check the job when it's back.")


ANSWER_WAIT = timedelta(minutes=30)


def _waiting_for_answer(sheet: Sheet) -> str:
    """After a bare /answer: "<job id>" (may be empty) for 30 minutes, else None."""
    raw = sheet.kv_get("Settings", "awaiting_answer", "")
    since, _, job_id = raw.partition("|")
    try:
        return job_id if raw and datetime.now(TZ) - datetime.fromisoformat(since) < ANSWER_WAIT else None
    except ValueError:
        return None


def queue_answer(sheet: Sheet, payload: dict, task_id: str, ctx) -> None:
    if queue_task(sheet, "answer", payload.get("job_id", ""), "", payload, task_id):
        sheet.flush()
        tg.send_message("📝 Writing an answer…" if ctx["mac_online"] else "📝 Queued: I'll write the answer when the Mac is back.")


def on_answer_input(sheet: Sheet, msg: dict, text: str, media: list, replied, ctx) -> bool:
    """The question after a bare /answer (text, screenshot, album), or a Reply to an answer with
    what to change. True when handled."""
    from agent import answers
    group = msg.get("media_group_id", "")
    if group and media:                                  # more photos of a question's album
        task = sheet.t("Queue").find("Task ID", f"answer-mg-{group}")
        if task:
            if task["Status"] == "pending":
                p = json.loads(task["Payload"] or "{}")
                if not any(m["file_id"] == media[0]["file_id"] for m in p.get("media", [])):
                    p["media"] = p.get("media", []) + media
                    sheet.t("Queue").update(task, Payload=json.dumps(p, ensure_ascii=False), Updated=now_iso())
                    sheet.flush()
            return True
    if replied and text:
        row = answers.for_message(sheet, replied)
        if row:
            queue_answer(sheet, {"qid": row["Q ID"], "mode": "fix", "feedback": text[:1000]},
                         f"answer-fix-{ctx.get('update_id') or now_iso()}", ctx)
            return True
    job_id = _waiting_for_answer(sheet)
    if job_id is None:
        return False
    sheet.kv_set("Settings", "awaiting_answer", "", flush=False)
    payload = {"text": text[:4000], "job_id": job_id}
    if media:
        payload["media"] = media
        payload["group"] = group
    queue_answer(sheet, payload, f"answer-mg-{group}" if group else f"answer-{ctx.get('update_id') or now_iso()}", ctx)
    return True


def cmd_answer(sheet, arg, ctx):
    from agent import answers
    job_id = ctx.get("job_id", "")                      # /answer as a Reply to a job card or CV
    if arg.strip():
        queue_answer(sheet, {"text": arg[:4000], "job_id": job_id},
                     f"answer-{ctx.get('update_id') or now_iso()}", ctx)
        return
    sheet.kv_set("Settings", "awaiting_answer", f"{now_iso()}|{job_id}")
    n = len(answers.bank(sheet))
    tg.send_message("📝 Now send me the question from the form: paste the text or send a screenshot.\n"
                    + (f"You have {n} saved answers, and I'll use them when a question comes again." if n else
                       "An answer you save (✅ Save) is used the next time the question comes again."),
                    buttons=[[("❌ Cancel", "cancel:")]])


def cmd_job(sheet, arg, ctx):
    from agent import intake
    urls = intake.urls_in(ctx.get("msg") or {}) or intake.URL.findall(arg)
    if not urls and len(arg) > 80:                     # a pasted post without links: read the text
        on_job_post(sheet, arg, [], ctx.get("update_id") or now_iso(), ctx)
        return
    if not urls:
        sheet.kv_set("Settings", "awaiting_link", f"job|{now_iso()}")
        tg.send_message("📥 Now send me the job: a link, a forwarded post, its text, a screenshot or photo, "
                        "or a PDF / Word file. I'll check it.",
                        buttons=[[("❌ Cancel", "cancel:")]])
        return
    on_job_post(sheet, arg, urls, ctx.get("update_id") or now_iso(), ctx)


MENU = [("open", "📋 Open jobs and CVs to apply"), ("mail", "📧 Check my mail now"), ("status", "📊 Status"),
        ("today", "📅 Today"),
        ("job", "📥 Check a job: link, text, screenshot, PDF"), ("cv", "📄 CV for a job: link, text, screenshot"),
        ("prep", "📚 Interview prep (company)"), ("answer", "📝 Answer a form question: text or screenshot"), ("threshold", "🎯 Fit score threshold"),
        ("pause", "⏸ Pause job search"), ("resume", "▶️ Resume job search"), ("sources", "🔎 Job sources"),
        ("skipped", "↩️ Skipped jobs and cancelled CVs"), ("cancel", "❌ Cancel what's waiting"), ("help", "❓ Help")]


def setup_menu() -> None:
    tg.set_menu(MENU)


def cmd_help(sheet, arg, ctx):
    from agent.pipeline import threshold
    tg.send_message(HELP.format(thr=threshold(sheet)))


def cmd_status(sheet, arg, ctx):
    from agent.pipeline import threshold
    apps = sheet.t("Applications").rows
    q = sheet.t("Queue").rows
    by_status = {}
    for a in apps:
        by_status[a["Status"]] = by_status.get(a["Status"], 0) + 1
    waiting = [r for r in q if r["Status"] in ("pending", "in_progress")]
    hb = sheet.kv_get("Health", "mac_heartbeat", "")
    paused = sheet.kv_get("Settings", "paused", "no") == "yes"
    last = sheet.kv_get("Health", "last_find_run", "—")
    lines = ["<b>Status</b>",
             f"Mac: {'🟢 online' if ctx['mac_online'] else '🔴 off'}" + (f" (last heartbeat {tg.esc(hb[11:16])})" if hb else ""),
             f"Search: {'⏸ paused' if paused else '▶️ on'} · threshold: {threshold(sheet)}",
             f"Last run: {tg.esc(last)}",
             "Applications: " + (", ".join(f"{k} {v}" for k, v in by_status.items()) or "none yet"),
             f"Tasks in the queue: {len(waiting)}"]
    from agent import claude_cli
    if claude_cli.COUNT_FILE.exists():                   # the Mac answers: it makes the Claude calls
        lines.append(f"Claude calls today: {claude_cli.calls_today()}")
    tg.send_message("\n".join(lines), buttons=[[("📧 Check mail now", "mailnow:")]])


def cmd_today(sheet, arg, ctx):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    jobs = [r for r in sheet.t("Jobs").rows if r["First seen"][:10] == today]
    alerted = [r for r in jobs if r["Decision"] in ("alerted", "alerting", "more_pending")]
    apps = sheet.t("Applications").rows
    approved = [a for a in apps if a["Date approved"] == today]
    applied = [a for a in apps if a["Date applied"] == today]
    lines = [f"<b>Today ({today})</b>", f"Jobs checked: {len(jobs)} · sent to you: {len(alerted)}",
             f"Approved: {len(approved)} · applied: {len(applied)}"]
    for a in applied:
        lines.append(f"📨 {tg.esc(a['Role'])} · {tg.esc(a['Company'])}")
    tg.send_message("\n".join(lines))


def cmd_threshold(sheet, arg, ctx):
    try:
        n = int(arg)
        if not 0 <= n <= 100:
            raise ValueError
    except ValueError:
        from agent.pipeline import threshold
        cur = threshold(sheet)
        row = lambda ns: [(f"{'• ' if n == cur else ''}{n}", f"thr:{n}") for n in ns]
        tg.send_message(f"🎯 The threshold is now <b>{cur}</b>: jobs with this score or higher are sent to you.\n"
                        "Lower = more jobs, higher = only the best fits. Pick a new threshold:",
                        buttons=[row([40, 45, 50]), row([55, 60, 70]), [("❌ Cancel", "cancel:")]])
        return
    sheet.kv_set("Settings", "fit_threshold", n)
    tg.send_message(f"✅ The threshold is now {n}. Jobs scoring {n} or higher will be sent to you.")


def cmd_pause(sheet, arg, ctx):
    sheet.kv_set("Settings", "paused", "yes")
    tg.send_message("⏸ The search is paused. /resume to resume.")


def cmd_resume(sheet, arg, ctx):
    sheet.kv_set("Settings", "paused", "no")
    tg.send_message("▶️ The search is back on.")


def cmd_sources(sheet, arg, ctx):
    comps = sources.companies()
    by = {}
    for c in comps:
        by[c["ats"]] = by.get(c["ats"], 0) + 1
    last = sheet.kv_get("Health", "source_errors", "")
    mail = (["LinkedIn"] if (config.settings().get("email_alerts") or {}).get("linkedin", True) else []) + \
        [name for _, name in board_mail.boards().values()]
    lines = [f"<b>Sources</b>: {len(comps)} companies",
             " · ".join(f"{k}: {v}" for k, v in sorted(by.items())),
             "📧 Email alerts: " + (", ".join(mail) or "none")]
    if last:
        lines.append(f"Errors in the last run: {tg.esc(last[:300])}")
    tg.send_message("\n".join(lines))


def cmd_prep(sheet, arg, ctx):
    if not arg.strip():
        apps = [a for a in sheet.t("Applications").rows if a["Status"] not in ("Withdrawn", "Rejected", "")]
        names = list(dict.fromkeys(a["Company"] for a in reversed(apps)))[:8]
        buttons = [[(n, f"prep:{n[:40]}")] for n in names]
        tg.send_message("📚 Prep for which company? Pick one, or write: /prep &lt;company name&gt;", buttons=buttons + [[("❌ Cancel", "cancel:")]])
        return
    queue_task(sheet, "prep", "", "", {"company": arg.strip()[:80]}, f"prep-{ctx.get('update_id') or now_iso()}")
    sheet.flush()
    tg.send_message("📚 Preparing your interview prep…" if ctx["mac_online"] else "📚 Queued: the prep will come when the Mac is back.")


def cmd_cv(sheet, arg, ctx):
    from agent import intake
    urls = intake.urls_in(ctx.get("msg") or {}) or intake.URL.findall(arg)
    if not urls:
        sheet.kv_set("Settings", "awaiting_link", f"cv|{now_iso()}")
        tg.send_message("📄 Now send me the job: a link, a forwarded post, its text, a screenshot or photo, "
                        "or a PDF / Word file. I'll build a tailored CV for it.", buttons=[[("❌ Cancel", "cancel:")]])
        return
    queue_task(sheet, "intake", "", "", {"text": arg[:6000], "urls": urls[:10], "approve": True},
               f"intake-{ctx.get('update_id') or now_iso()}")
    sheet.flush()
    tg.send_message("📥 Got it. Checking the job and building a CV…" if ctx["mac_online"]
                    else "📥 Queued: I'll handle it when the Mac is back.")


def cmd_mail(sheet, arg, ctx):
    """Read Gmail now: company replies and job-alert emails (normally hourly / every ~15 minutes)."""
    stamp = ctx.get("update_id") or now_iso()
    if any(t["Type"] == "mail_now" and t["Status"] in ("pending", "in_progress") for t in sheet.t("Queue").rows):
        tg.send_message("📧 Already checking your email, the answer will come in a moment.")
        return
    queue_task(sheet, "mail_now", "", "", {}, f"mailnow-{stamp}")
    sheet.flush()
    tg.send_message("📧 Checking your email now: company replies and job alerts…" if ctx["mac_online"]
                    else "📧 The Mac is off. I'll check your email when it's back.")


def cmd_open(sheet, arg, ctx):
    alerts.send_pending(sheet)
    alerts.send_waiting_cvs(sheet)


def cmd_later(sheet, arg, ctx):
    tg.send_message(LATER)


COMMANDS = {"/start": cmd_help, "/help": cmd_help, "/status": cmd_status, "/today": cmd_today,
            "/threshold": cmd_threshold, "/pause": cmd_pause, "/resume": cmd_resume,
            "/sources": cmd_sources, "/job": cmd_job, "/cv": cmd_cv, "/prep": cmd_prep, "/cancel": cmd_cancel, "/open": cmd_open, "/skipped": cmd_skipped, "/mail": cmd_mail, "/answer": cmd_answer}
