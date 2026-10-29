"""One run: the worker on your computer (every 15 min by day, every 2 h at night, while it is awake),
the optional GitHub Actions backup (same rhythm), or by hand:

    python -m agent.run [--no-send] [--force]

GitHub's schedule is best effort (it often skips slots), so the worker on your computer is the main
searcher while it is awake and the cloud is an optional backup: the search
step runs only when it is due (no search in the last 14 min by day / 115 min at night) and
only under a lock in the Sheet, so the two never search at once and never alert twice.

1. Telegram failover: if the computer's heartbeat is older than 10 minutes, this run consumes
   Telegram updates (Inbox first) and handles them; CV work waits in the Queue.
2. Re-sends any alert recorded but not sent (crash safety), then finds and alerts new jobs.
3. Once a day: checks that every GitHub link on the CVs is still public.
4. Counts this month's Actions minutes and warns on Telegram at 80% of the free 2,000.
Later steps add the Gmail check, reminders and the daily digest here, never as extra runs.
"""
import math
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from agent import alerts, bot, config, pipeline, secrets
from agent import telegram as tg
from agent.sheet import TZ, Sheet, now_iso

HEARTBEAT_STALE = timedelta(minutes=10)
FREE_MINUTES = 2000


FIND_GAP_DAY = timedelta(minutes=14)
FIND_GAP_NIGHT = timedelta(minutes=115)
LOCK_TTL = timedelta(minutes=5)


def say(m) -> None:
    print(secrets.redact(str(m)), flush=True)


def mac_online(sheet: Sheet) -> bool:
    hb = sheet.kv_get("Health", "mac_heartbeat", "")
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(hb) < HEARTBEAT_STALE
    except ValueError:
        return False


def telegram_failover(sheet: Sheet) -> None:
    if mac_online(sheet):
        return
    try:
        updates = tg.get_updates(timeout=0)
    except tg.TelegramError as e:
        if "409" in str(e) or "Conflict" in str(e):
            say("telegram: the computer is polling after all; leaving updates to it")
            return
        raise
    if updates:
        sheet.load("Inbox")
        bot.record(sheet, updates, by="cloud")
        tg.get_updates(offset=max(u["update_id"] for u in updates) + 1, timeout=0)   # confirm
    sheet.load("Inbox")
    if any(not r["Handled"] for r in sheet.t("Inbox").rows):
        n = bot.handle_pending(sheet, by="cloud", mac_online=False, log=say)
        say(f"telegram failover: handled {n} update(s)")


def gmail_expiry_reminder(sheet: Sheet) -> None:
    """While the Gmail OAuth app is in Testing, Google ends the sign-in 7 days after it was given.
    From day 6, remind the user once a day, with the one command that renews it."""
    issued = secrets.get("GMAIL_TOKEN_ISSUED", required=False)
    if not issued or issued == "production":             # a published app: the sign-in does not expire
        return
    age = datetime.now(timezone.utc) - datetime.fromisoformat(issued)
    if age < timedelta(days=6):
        return
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if sheet.kv_get("Health", "gmail_reminded", "") == today:
        return
    left = timedelta(days=7) - age
    when = (f"expires in about {max(1, int(left.total_seconds() // 3600))} hours" if left > timedelta(0)
            else "has expired, so the job-alert emails are not being read")
    tg.send_message(f"🔑 The agent's Gmail access {when}.\n"
                    "To renew it (one minute), in a terminal on your computer:\n"
                    f"<code>cd \"{tg.esc(config.ROOT)}\" && \"{tg.esc(sys.executable)}\" scripts/gmail_auth.py</code>\n"
                    "A browser opens: sign in and allow.\n"
                    "To stop this weekly expiry, publish the OAuth app (see the README, Gmail step).")
    sheet.kv_set("Health", "gmail_reminded", today)


def due_reminders(sheet: Sheet) -> None:
    """One-time reminders: Health keys 'remind:<name>' = '<ISO time>|<text>'.
    Sent once when the time has come, then cleared."""
    now = datetime.now(TZ)
    for r in sheet.t("Health").rows:
        if not r["Key"].startswith("remind:") or "|" not in r["Value"]:
            continue
        when, _, text = r["Value"].partition("|")
        try:
            if datetime.fromisoformat(when) > now:
                continue
        except ValueError:
            continue
        sheet.kv_set("Health", r["Key"], "")              # cleared first: never sent twice
        tg.send_message(f"⏰ Reminder: {text}")


def daily_link_check(sheet: Sheet) -> None:
    """Once a day: every GitHub link in PROFILE.yaml must be public for an anonymous visitor."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if sheet.kv_get("Health", "link_check_date", "") == today:
        return
    sys.path.insert(0, str(config.TEMPLATE_DIR))
    from check_links import allowed_links, is_public_on_github
    problems, unverified = [], []
    for url in sorted(allowed_links(config.profile())):
        if "github.com" not in url:
            continue
        ok, why = is_public_on_github(url, {})
        if not ok:
            (unverified if "not verified" in why else problems).append(f"{url}: {why}")
    if problems:
        tg.send_message("⚠️ <b>A problem with a GitHub link on your CV</b>\n" + "\n".join(tg.esc(p) for p in problems) +
                        "\nCVs with this link won't be sent until it's fixed.")
    if not unverified:
        sheet.kv_set("Health", "link_check_date", today, flush=False)
        sheet.kv_set("Health", "link_check_result", "ok" if not problems else "; ".join(problems)[:500])
    say(f"link check: {len(problems)} problem(s), {len(unverified)} not verified")


def find_due(sheet: Sheet, now: datetime = None) -> bool:
    now = now or datetime.now(TZ)
    gap = FIND_GAP_DAY if 8 <= now.hour < 22 else FIND_GAP_NIGHT
    try:
        return now - datetime.fromisoformat(sheet.kv_get("Health", "last_find_at", "")) >= gap
    except ValueError:
        return True


def _lock_holder(sheet: Sheet, key: str = "find_lock") -> tuple:
    token, _, ts = sheet.kv_get("Health", key, "").partition("|")
    try:
        if token and datetime.now(TZ) - datetime.fromisoformat(ts) < LOCK_TTL:
            return token
    except ValueError:
        pass
    return ""


def claim_find(sheet: Sheet, owner: str, key: str = "find_lock") -> str:
    """Take the search lock in the Sheet; '' if someone else holds it. Write, wait, read back:
    of two runs that write at the same moment, only the one whose token stayed proceeds."""
    sheet.load("Health")
    if _lock_holder(sheet, key):
        return ""
    token = f"{owner}-{uuid.uuid4().hex[:6]}"
    sheet.kv_set("Health", key, f"{token}|{now_iso()}")
    time.sleep(4)
    sheet.load("Health")
    return token if _lock_holder(sheet, key) == token else ""


def release_find(sheet: Sheet, token: str, key: str = "find_lock") -> None:
    sheet.load("Health")
    if _lock_holder(sheet, key) == token:
        sheet.kv_set("Health", key, "")


def tracking_step(sheet: Sheet, owner: str, log=None) -> None:
    """Mail scan, follow-ups, 09:00 open jobs, 18:00 digest, 22:00 email: only when one is due, and under the
    'daily_lock', so the computer and the cloud never send the same thing twice."""
    from agent import gmail, openness, tracking
    log = log or say
    sheet.load("Health")
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    try:
        scan_due = now - datetime.fromisoformat(sheet.kv_get("Health", "mail_scan_at", "")) >= tracking.SCAN_EVERY
    except ValueError:
        scan_due = True
    due = (scan_due or (now.hour >= 9 and sheet.kv_get("Health", "followups_date", "") != today)
           or (now.hour >= tracking.MORNING_HOUR and sheet.kv_get("Health", "morning_date", "") != today)
           or (now.hour >= 18 and sheet.kv_get("Health", "digest_date", "") != today)
           or (now.hour >= 22 and sheet.kv_get("Health", "email_date", "") != today)
           or (now.hour >= 10 and sheet.kv_get("Health", "open_check_date", "") != today))
    if not due:
        return
    token = claim_find(sheet, owner, key="daily_lock")
    if not token:
        log("tracking: another run holds the lock")
        return
    try:
        for name, fn in (("mail", tracking.mail_scan), ("followups", tracking.followups),
                         ("morning", tracking.morning_jobs),
                         ("digest", tracking.digest), ("email", tracking.daily_email),
                         ("open check", openness.daily_check)):
            try:
                sheet.load("Health")
                fn(sheet, log=log)
            except Exception as e:                    # one part failing never blocks the others
                log(f"tracking {name} failed: {e.__class__.__name__}: {secrets.redact(str(e))[:200]}")
                if isinstance(e, gmail.GmailAuthError):
                    raise
    finally:
        release_find(sheet, token, key="daily_lock")


def find_step(sheet: Sheet, owner: str, send: bool, force: bool = False, log=None):
    log = log or say
    sheet.load("Health")
    if not force and not find_due(sheet):
        log("find: not due (searched recently)")
        return None
    token = claim_find(sheet, owner)
    if not token:
        log("find: another run is searching now")
        return None
    try:
        if not force and not find_due(sheet):          # it finished just before we took the lock
            return None
        out = pipeline.find_and_alert(sheet, send=send, log=log)
        sheet.load("Health")
        sheet.kv_set("Health", "last_find_at", now_iso())
        return out
    finally:
        release_find(sheet, token)


def count_minutes(sheet: Sheet) -> None:
    """Billable minutes of this run (rounded up), summed per month in Health; warn at 80%."""
    started = os.environ.get("RUN_STARTED")
    if not os.environ.get("GITHUB_ACTIONS") or not started:
        return
    minutes = max(1, math.ceil((time.time() - int(started) + 10) / 60))   # +10 s for the last steps
    month = datetime.now(TZ).strftime("%Y-%m")
    key = f"actions_minutes_{month}"
    total = int(sheet.kv_get("Health", key, "0") or 0) + minutes
    sheet.kv_set("Health", key, total, flush=False)
    sheet.kv_set("Health", "last_run_minutes", minutes, flush=False)
    if total >= 0.8 * FREE_MINUTES and sheet.kv_get("Health", f"minutes_warned_{month}", "") != "yes":
        tg.send_message(f"⚠️ GitHub Actions: about {total} of the {FREE_MINUTES} free minutes used this month (80%).\n"
                        "The search goes on. At the limit, the cloud runs stop until the month starts again (no charge).")
        sheet.kv_set("Health", f"minutes_warned_{month}", "yes", flush=False)
    sheet.flush()


def main() -> None:
    run_once(send="--no-send" not in sys.argv, force="--force" in sys.argv)


def run_once(send: bool = True, force: bool = False, owner: str = None, log=None) -> list:
    """All steps of one run; returns the names of failed steps. One failing step never blocks
    the others."""
    say = log or globals()["say"]
    owner = owner or ("cloud" if os.environ.get("GITHUB_ACTIONS") else "local")
    sheet = Sheet()
    sheet.load("Health", "Settings")
    steps = [("telegram", lambda: telegram_failover(sheet) if owner != "mac" else None),
             ("resend", lambda: (sheet.load("Jobs"), alerts.resend_unsent(sheet)) if send else None),
             ("find", lambda: find_step(sheet, owner, send, force=force, log=say)),
             ("links", lambda: daily_link_check(sheet)),
             ("gmail_expiry", lambda: gmail_expiry_reminder(sheet)),
             ("reminders", lambda: due_reminders(sheet)),
             ("tracking", lambda: tracking_step(sheet, owner, log=say))]
    failed = []
    for name, fn in steps:
        streak_key = f"fail_streak_{name}"
        try:
            out = fn()
            if sheet.kv_get("Health", streak_key, "0") not in ("", "0"):
                sheet.kv_set("Health", streak_key, 0, flush=False)
            if name == "find" and isinstance(out, dict):
                errs = out.get("errors") or {}
                sheet.kv_set("Health", "source_errors", "; ".join(f"{k}: {v}" for k, v in errs.items())[:1000])
        except Exception as e:                       # one failing step never blocks the others
            failed.append(name)
            say(f"step {name} failed: {e.__class__.__name__}: {e}")
            try:
                sheet.load("Health")
                streak = int(sheet.kv_get("Health", streak_key, "0") or 0) + 1
                sheet.kv_set("Health", streak_key, streak)
                if streak == 3:
                    tg.send_message(f"⚠️ The '{name}' step failed 3 times in a row: "
                                    f"{tg.esc(secrets.redact(str(e))[:200])}")
            except Exception:
                pass
            if "403" in str(e) and "Sheet" in str(e):
                try:
                    tg.send_message("⚠️ No access to the Sheet (403). It was probably unshared from the service "
                                    "account, or its key was deleted. The agent can't work without the Sheet.")
                except tg.TelegramError:
                    pass
    sheet.load("Health")
    sheet.kv_set("Health", f"last_{owner}_run", f"{now_iso()} failed={','.join(failed) or 'none'}", flush=False)
    count_minutes(sheet)
    sheet.flush()
    return failed


if __name__ == "__main__":
    main()
