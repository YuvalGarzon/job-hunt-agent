"""Queue tasks that need the computer (Claude, the CV renderer). Each task is resumable:
status pending -> in_progress -> done | failed, with attempts and updated_at.
A task stuck in_progress for more than 15 minutes (shutdown mid-build) is run again.
"""
import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import config, cv_builder, secrets, sources
from agent import telegram as tg
from agent.bot import cv_buttons
from agent.claude_cli import ClaudeError, PlanLimitError
from agent.sheet import Sheet, now_iso
from agent.sources import board_mail

STALE = timedelta(minutes=15)
MAX_ATTEMPTS = 3


def _age(ts: str) -> timedelta:
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except ValueError:
        return timedelta(days=999)


def reset_stale(sheet: Sheet, log=print, startup: bool = False) -> int:
    """Re-run interrupted tasks. At worker startup every in_progress task belongs to a worker
    that died (only one worker can hold the lock), so it is resumed at once."""
    q = sheet.t("Queue")
    n = 0
    for r in q.rows:
        if r["Status"] == "in_progress" and (startup or _age(r["Updated"]) > STALE):
            q.update(r, Status="pending", Updated=now_iso(), Error="resumed after interruption")
            n += 1
    if n:
        sheet.flush()
        log(f"resumed {n} interrupted task(s)")
    return n


def next_task(sheet: Sheet) -> dict:
    q = sheet.t("Queue")
    for r in sorted(q.rows, key=lambda r: r["Created"]):
        if r["Status"] == "pending":
            retry_at = json.loads(r["Payload"] or "{}").get("retry_at")
            if retry_at and _age(retry_at) < timedelta(0):
                continue
            return r
    return None


def run_one(sheet: Sheet, log=print) -> bool:
    """Claim and run the oldest pending task. Returns True if a task ran."""
    sheet.load("Queue")
    reset_stale(sheet, log)
    task = next_task(sheet)
    if not task:
        return False
    sheet.load("Queue", "Applications", "Jobs", "Settings")
    task = sheet.t("Queue").find("Task ID", task["Task ID"])
    if task["Status"] != "pending":
        return False
    q = sheet.t("Queue")
    attempts = int(task["Attempts"] or 0) + 1
    q.update(task, Status="in_progress", Attempts=attempts, Updated=now_iso())
    sheet.flush()
    payload = json.loads(task["Payload"] or "{}")
    try:
        fn = HANDLERS[task["Type"]]
        result = fn(sheet, task, payload, log)
        q.update(task, Status="done", Updated=now_iso(), Result=result or "ok", Error="")
    except PlanLimitError as e:
        q.update(task, Status="failed", Updated=now_iso(), Error=f"plan/usage limit: {str(e)[:300]}")
        tg.send_message("⚠️ Claude stopped because of your plan's usage limit. I didn't try another model.\n"
                        "When the limit resets, tap 🔁 Try again (or approve again).")
    except cv_builder.LinkCheckPending as e:
        retry = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds")
        payload["retry_at"] = retry
        q.update(task, Status="pending", Updated=now_iso(), Payload=json.dumps(payload, ensure_ascii=False),
                 Error=f"GitHub links not verified yet: {str(e)[:200]}")
        if attempts == 1:
            tg.send_message("⏳ I couldn't verify the GitHub links on the CV (GitHub is not reachable). "
                            "I don't send a CV without checking. I'll try again in 30 minutes.")
    except cv_builder.CVBuildError as e:
        # the CV did not pass its checks in time: go on at once from its last version (no wait,
        # nothing is down); the last attempt fails like any other task
        err = secrets.redact(f"{e.__class__.__name__}: {e}")[:500]
        log(traceback.format_exc())
        final = attempts >= MAX_ATTEMPTS
        if not final and e.last:
            payload["resume"] = {"content": str(e.last), "problems": e.problems}
            payload.pop("retry_at", None)
        elif not final:
            payload["retry_at"] = (datetime.now(timezone.utc) + timedelta(minutes=2 ** attempts)).isoformat(timespec="seconds")
        q.update(task, Status="failed" if final else "pending", Updated=now_iso(), Error=err,
                 Payload=json.dumps(payload, ensure_ascii=False))
        if final:
            tg.send_message(f"❌ A task failed ({tg.esc(task['Type'])}): {tg.esc(err[:300])}")
    except Exception as e:
        err = secrets.redact(f"{e.__class__.__name__}: {e}")[:500]
        log(traceback.format_exc())
        final = attempts >= MAX_ATTEMPTS or isinstance(e, ClaudeError) and "not allowed" in str(e)
        if not final:                                  # back off: 2, 4 minutes
            payload["retry_at"] = (datetime.now(timezone.utc) + timedelta(minutes=2 ** attempts)).isoformat(timespec="seconds")
        q.update(task, Status="failed" if final else "pending", Updated=now_iso(), Error=err,
                 Payload=json.dumps(payload, ensure_ascii=False))
        if final:
            tg.send_message(f"❌ A task failed ({tg.esc(task['Type'])}): {tg.esc(err[:300])}")
    sheet.flush()
    return True


# --- build_cv ----------------------------------------------------------------------------
def _job_from_sheet(sheet: Sheet, job_id: str) -> dict:
    """The job as the CV builder takes it: company, title, location, url, description."""
    from agent.intake import post_text
    row = sheet.t("Jobs").find("Job ID", job_id)
    if not row:
        raise RuntimeError(f"job {job_id} not in the Jobs tab")
    given = post_text(job_id)       # a post the user sent, a job-board email, or a posting pasted for this job
    if given:
        return {"company": row["Company"], "title": row["Title"], "location": row["Location"], "url": row["URL"],
                "description": given}
    if row["Source"] == "telegram":
        return {"company": row["Company"], "title": row["Title"], "location": row["Location"], "url": row["URL"],
                "description": f"{row['Title']} at {row['Company']}."}
    if row["Source"] in board_mail.boards():
        # a job-board alert: the board is never opened; the alert email's text is the posting
        return {"company": row["Company"], "title": row["Title"], "location": row["Location"], "url": row["URL"],
                "description": board_mail.description(row) or f"{row['Title']} at {row['Company']}."}
    if row["Source"] == "linkedin":
        # LinkedIn-only job: LinkedIn is never opened, so the CV is tailored to the title alone
        return {"company": row["Company"], "title": row["Title"], "location": row["Location"], "url": row["URL"],
                "description": f"{row['Title']} at {row['Company']}. (Only the title is known; the full "
                               "posting is on LinkedIn.)"}
    fresh = sources.fetch_one(row["Source ref"]) if row["Source ref"] else None
    if fresh is None:
        raise RuntimeError("the posting is no longer available from its source")
    return {"company": row["Company"], "title": row["Title"], "location": row["Location"],
            "url": row["URL"], "description": fresh["description"]}


def build_cv(sheet: Sheet, task: dict, payload: dict, log) -> str:
    apps = sheet.t("Applications")
    app = apps.find("App ID", task["App ID"])
    job = _job_from_sheet(sheet, task["Job ID"])
    result_prev = json.loads(task["Result"]) if task["Result"].startswith("{") else {}
    if result_prev.get("sent_msg"):
        return task["Result"]                       # PDF already sent before an interruption

    mode = payload.get("mode", "new")
    out_dir = Path(app["CV path"]).parent if app and app["CV path"] else cv_builder.job_dir(job, date=app["Date approved"] or None)
    if not out_dir.is_absolute():
        out_dir = config.ROOT / out_dir
    previous = out_dir / "content.yaml"
    feedback = None
    if mode == "fix":
        feedback = payload.get("feedback", "")
    elif mode == "retry":
        feedback = ("The user asked for a different version. Choose a different emphasis or main project "
                    "where that is still true and relevant, and different wording.")
    notes = config.STATE_DIR / "work" / out_dir.name / "notes.json"
    final_pdf = out_dir / config.cv_file_name()
    resumable = False
    if final_pdf.exists() and notes.exists():
        try:
            built = datetime.fromisoformat(json.loads(notes.read_text(encoding="utf-8"))["built"])
            resumable = built >= datetime.fromisoformat(task["Created"])
        except (ValueError, KeyError, TypeError):
            resumable = False
    if resumable and int(task["Attempts"] or 1) > 1:
        # built and checked before an interruption, never recorded as sent: send it, do not rebuild
        n = json.loads(notes.read_text(encoding="utf-8"))
        res = {"pdf": final_pdf, "fit": n["fit"], "gaps": n["gaps"], "cover": n.get("cover")}
        log(f"resuming {task['Task ID']}: PDF was already built, sending it")
    else:
        tg.send_chat_action("upload_document")
        resume = payload.get("resume") or {}
        last = Path(resume.get("content", ""))
        if resume and last.is_file():
            log(f"{task['Task ID']}: going on from the last version of the previous attempt")
            res = cv_builder.build(job, out_dir=out_dir, feedback=feedback, previous_content=last,
                                   failed=resume.get("problems") or [], log=log)
        else:
            res = cv_builder.build(job, out_dir=out_dir, feedback=feedback,
                                   previous_content=previous if mode == "fix" and previous.exists() else None, log=log)

    gaps = ", ".join(res["gaps"]) if res["gaps"] else "none"
    label = {"new": "Tailored CV", "retry": "New version", "fix": "Fixed version",
             "posting": "CV from the full job description"}[mode]
    head = f"📄 <b>{label}</b>: {tg.esc(job['title'])} · {tg.esc(job['company'])}\n"
    cover = res.get("cover") or {}
    asked = len(cover.get("shown", [])) + len(cover.get("left_out", [])) + len(cover.get("lacking", []))
    keys = f"\n🔑 {len(cover.get('shown', []))}/{asked} of the job's tools are on the CV" if asked else ""
    terms = len(cover.get("terms_shown", [])) + len(cover.get("terms_missing", []))
    if terms:
        keys += f"\n🎯 {len(cover['terms_shown'])}/{terms} of the job's keywords (true for you) appear in its own wording"
    tail = f'{keys}\n{config.where_saved(res["pdf"])}'
    room = max(120, 1024 - len(head) - len(tail) - 30)     # the location line is never cut off
    fit, gap = tg.esc(res["fit"])[:room * 55 // 100], tg.esc(gaps)[:room * 45 // 100]
    caption = f"{head}✅ {fit}\n⚠️ Gaps: {gap}{tail}"
    msg = tg.send_document(res["pdf"], caption=caption[:1024], buttons=cv_buttons(task["App ID"]) + tg.link_row(job["url"]))
    result = json.dumps({"sent_msg": msg["message_id"], "pdf": str(res["pdf"].relative_to(config.ROOT))},
                        ensure_ascii=False)
    sheet.t("Queue").update(task, Result=result)      # record the send at once
    sheet.flush()
    if app:
        changes = {"CV path": str(res["pdf"].relative_to(config.ROOT)), "Last update": now_iso(),
                   "Notes": f"gaps: {gaps}"[:500]}
        if app["Status"] in ("Approved", "CV ready", ""):
            changes.update({"Status": "CV ready", "Next step": "review and apply"})
        apps.update(app, **changes)
    return result


def chat(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import writing
    return writing.chat(sheet, task, payload, log)


def cover_letter_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import writing
    return writing.cover_letter(sheet, task, payload, log)


def prep_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import writing
    return writing.prep(sheet, task, payload, log)


def mail_now(sheet: Sheet, task: dict, payload: dict, log) -> str:
    """/mail: read Gmail now. Company replies first (each becomes a mail_event task, handled right
    after this), then a search run, which reads the LinkedIn and job-board alert emails."""
    import time
    from agent import run, tracking
    token = ""
    for _ in range(6):                                  # the hourly scan may hold the lock briefly
        token = run.claim_find(sheet, "mac", key="daily_lock")
        if token:
            break
        time.sleep(10)
    if not token:
        tg.send_message("📧 Another email check is running right now. Try again in a minute.")
        return "busy"
    try:
        sheet.load("Health")
        queued = tracking.mail_scan(sheet, log=log, force=True)
    finally:
        run.release_find(sheet, token, key="daily_lock")
    tg.send_chat_action("typing")
    stats = run.find_step(sheet, "mac", send=True, force=True, log=log)
    lines = ["📧 <b>Done checking your email</b>",
             f"• {queued} new emails from companies you applied to. Processing them now, the messages will come in a moment."
             if queued else "• No new emails from companies you applied to."]
    if stats is None:
        lines.append("• A job search is running right now. The jobs it finds will come in a moment.")
    elif stats.get("paused"):
        lines.append("• The search is paused (/resume to resume), so I didn't check job alerts.")
    else:
        lines.append(f"• {stats.get('alert', 0)} new jobs sent to you (from email alerts and company sites)."
                     if stats.get("alert") else "• No new jobs that pass the filters.")
    tg.send_message("\n".join(lines))
    return f"mail {queued}, jobs {stats and stats.get('alert')}"


def intake_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import intake
    return intake.run_task(sheet, task, payload, log)


def mail_event_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import tracking
    return tracking.mail_event(sheet, task, payload, log)


def followup_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import tracking
    return tracking.followup_draft(sheet, task, payload, log)


def answer_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import answers
    return answers.write(sheet, task, payload, log)


def scout_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent import scout
    return scout.run_task(sheet, task, payload, log)


HANDLERS = {"build_cv": build_cv, "chat": chat, "intake": intake_task, "mail_event": mail_event_task,
            "mail_now": mail_now,
            "scout": scout_task, "answer": answer_task, "followup_draft": followup_task, "cover_letter": cover_letter_task, "prep": prep_task}
