"""The Mac worker: primary Telegram consumer, heartbeat, and the task queue.

    .venv/bin/python -m agent.worker

Four threads: one long-polls Telegram, writes every update to the Inbox and handles it;
one writes the heartbeat every minute; one runs queue tasks (CV builds) one at a time; one runs
the search (agent.run) whenever it is due: every 15 min by day, every 2 h at night, at once
after the Mac wakes. The optional cloud run skips its search when this one searched recently.
Only one worker runs at a time (file lock).
"""
import fcntl
import logging
import logging.handlers
import sys
import threading
import time

from agent import bot, config, run, scout, secrets, tasks
from agent import telegram as tg
from agent.sheet import Sheet, now_iso

HEARTBEAT_EVERY = 60
INBOX_RECHECK = 120
_wake = threading.Event()
_stop = threading.Event()


def _logger() -> logging.Logger:
    d = config.STATE_DIR / "logs"
    d.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("worker")
    if not log.handlers:
        h = logging.handlers.RotatingFileHandler(d / "worker.log", maxBytes=2_000_000, backupCount=3)
        h.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(message)s"))
        log.addHandler(h)
        log.addHandler(logging.StreamHandler(sys.stdout))
        log.setLevel(logging.INFO)
    return log


def heartbeat_loop(log) -> None:
    """Its own thread, so a slow Sheets write never holds up Telegram."""
    say = lambda m: log.info(secrets.redact(str(m)))
    sheet = None
    while not _stop.is_set():
        try:
            sheet = sheet or Sheet()
            sheet.load("Health")
            sheet.kv_set("Health", "mac_heartbeat", now_iso())
        except Exception as e:
            say(f"heartbeat error: {e.__class__.__name__}: {e}")
            sheet = None
        _stop.wait(HEARTBEAT_EVERY)


def poll_loop(log) -> None:
    sheet = Sheet()
    say = lambda m: log.info(secrets.redact(str(m)))
    offset, last_inbox_check = None, 0.0
    while not _stop.is_set():
        try:
            updates = tg.get_updates(offset=offset, timeout=25)
            if updates:
                for u in updates:                       # stop the button spinner at once
                    if "callback_query" in u:
                        tg.answer_callback(u["callback_query"]["id"])
                tg.send_chat_action("typing")           # instant sign that the message arrived
                sheet.load("Inbox")
                bot.record(sheet, updates, by="mac")
                offset = max(u["update_id"] for u in updates) + 1
            # unhandled updates: new ones, or ones the cloud recorded while the Mac was away
            if updates or time.time() - last_inbox_check > INBOX_RECHECK:
                if not updates:
                    sheet.load("Inbox")
                last_inbox_check = time.time()
                if any(not r["Handled"] for r in sheet.t("Inbox").rows):
                    bot.handle_pending(sheet, by="mac", mac_online=True, log=say)
                    _wake.set()
        except Exception as e:
            say(f"poll error: {e.__class__.__name__}: {e}")
            time.sleep(5)


def task_loop(log) -> None:
    sheet = Sheet()
    say = lambda m: log.info(secrets.redact(str(m)))
    sheet.load("Queue")
    tasks.reset_stale(sheet, log=say, startup=True)
    while not _stop.is_set():
        try:
            ran = tasks.run_one(sheet, log=say)
        except Exception as e:
            say(f"task loop error: {e.__class__.__name__}: {e}")
            ran = False
            time.sleep(20)
        if not ran:
            _wake.wait(timeout=30)
            _wake.clear()


def find_loop(log) -> None:
    say = lambda m: log.info(secrets.redact(str(m)))
    time.sleep(20)                                   # let the heartbeat go out first
    while not _stop.is_set():
        try:
            sheet = Sheet()
            sheet.load("Health")
            if run.find_due(sheet):
                failed = run.run_once(owner="mac", log=say)
                say(f"run done, failed steps: {', '.join(failed) or 'none'}")
            if scout.maybe_queue(sheet):                 # once a day: look for more companies
                _wake.set()
        except Exception as e:
            say(f"find loop error: {e.__class__.__name__}: {e}")
        _stop.wait(60)


def main() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(config.STATE_DIR / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another worker is already running")
        return
    log = _logger()
    log.info("worker started")
    try:
        bot.setup_menu()                                # the ☰ Menu button in the chat
    except Exception as e:
        log.info(f"menu setup failed: {e.__class__.__name__}")
    t1 = threading.Thread(target=poll_loop, args=(log,), name="poll", daemon=True)
    t2 = threading.Thread(target=task_loop, args=(log,), name="tasks", daemon=True)
    t3 = threading.Thread(target=find_loop, args=(log,), name="find", daemon=True)
    t4 = threading.Thread(target=heartbeat_loop, args=(log,), name="heartbeat", daemon=True)
    for t in (t1, t2, t3, t4):
        t.start()
    try:
        while all(t.is_alive() for t in (t1, t2, t3, t4)):
            time.sleep(1)
    except KeyboardInterrupt:
        _stop.set()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
