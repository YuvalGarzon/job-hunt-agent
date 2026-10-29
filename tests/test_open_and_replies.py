import json
from datetime import datetime, timedelta

from agent import alerts, bot, openness, writing
from agent.sheet import TZ, now_iso
from tests.test_bot_idempotency import cb, sheet, sent  # noqa: F401  (fixtures)


def msg(uid, text, **extra):
    return {"update_id": uid, "message": {"message_id": 9, "chat": {"id": 42}, "from": {"id": 42}, "text": text, **extra}}


def add_job(sheet, jid, first_seen, score, posted="", source="greenhouse", ref=""):
    sheet.t("Jobs").append({"Job ID": jid, "Company": f"Co{jid}", "Title": "Data Engineer", "City": "", "Location": "",
                            "Source": source, "Source ref": ref, "URL": f"https://x/{jid}", "Score": str(score),
                            "First seen": first_seen, "Posted": posted, "Decision": "alerted"})
    sheet.flush()


def test_open_lists_newest_first_then_best(sheet):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    old = (datetime.now(TZ) - timedelta(days=3)).strftime("%Y-%m-%d")
    sheet.t("Jobs").rows.clear()
    add_job(sheet, "j_old_best", f"{today}T08:00", 95, posted=old)        # found today, posted 3 days ago
    add_job(sheet, "j_today_low", f"{today}T09:00", 60)
    add_job(sheet, "j_today_high", f"{today}T07:00", 80)
    add_job(sheet, "j_closed", f"{today}T07:00", 99)
    sheet.t("Jobs").rows[-1]["Closed"] = f"{today} you"
    assert [r["Job ID"] for r in alerts.pending_rows(sheet)] == ["j_today_high", "j_today_low", "j_old_best"]


def test_closed_button_only_where_the_agent_cannot_check(sheet):
    li = {"Job ID": "a", "Source": "linkedin", "Source ref": "", "URL": "https://www.linkedin.com/jobs/view/1/"}
    gh = {"Job ID": "b", "Source": "greenhouse", "Source ref": "greenhouse:acme:1", "URL": "https://x/1"}
    assert ("🔒 Closed", "closed:a") in alerts.card_buttons(li)[0]
    assert all(d != "closed:b" for _, d in alerts.card_buttons(gh)[0])


def test_closed_and_reopen(sheet, sent):
    bot.handle(sheet, cb("ap:abc123def456"), by="mac", mac_online=True)
    app = sheet.t("Applications").rows[0]
    app["Status"] = "CV ready"
    app["CV path"] = "CVs/x/Alex Morgan CV.pdf"
    bot.handle(sheet, cb(f"aclosed:{app['App ID']}", 2), by="mac", mac_online=True)
    job = sheet.t("Jobs").rows[0]
    assert job["Closed"].endswith("you") and app["Status"] == "Closed"
    assert "Marked as closed" in sent[-1]
    bot.handle(sheet, cb("reopen:abc123def456", 3), by="mac", mac_online=True)
    assert job["Closed"] == "" and app["Status"] == "CV ready"


def test_daily_check_needs_two_misses(sheet, sent, monkeypatch):
    sheet.t("Jobs").rows.clear()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    for jid in ("gone", "open", "err"):
        add_job(sheet, jid, f"{today}T08:00", 70, ref=f"greenhouse:acme:{jid}")

    def one(ref):
        if ref.endswith("err"):
            raise ConnectionError("down")
        return None if ref.endswith("gone") else {"title": "x"}
    monkeypatch.setattr(openness.sources, "fetch_one", one)
    monkeypatch.setattr(openness, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime.now(TZ).replace(hour=11)),
                                                               "fromisoformat": staticmethod(datetime.fromisoformat)}))
    assert openness.daily_check(sheet, log=lambda *a: None) == 0           # first miss: not closed yet
    gone = sheet.t("Jobs").find("Job ID", "gone")
    assert gone["Open check"].startswith("missing") and not gone["Closed"]
    gone["Open check"] = "missing " + (datetime.now(TZ) - timedelta(hours=30)).isoformat(timespec="seconds")
    sheet.kv_set("Health", "open_check_date", "")
    assert openness.daily_check(sheet, log=lambda *a: None) == 1
    assert gone["Closed"].endswith("auto")
    assert not sheet.t("Jobs").find("Job ID", "err")["Closed"]              # could not check: never closed
    assert [r["Job ID"] for r in alerts.pending_rows(sheet)] == ["open", "err"] or \
        [r["Job ID"] for r in alerts.pending_rows(sheet)] == ["err", "open"]


def test_reply_to_any_message_reaches_the_chat_with_context(sheet, sent):
    sheet.t("Queue").append({"Task ID": "cv-Aabc123de-1", "Type": "build_cv", "Job ID": "abc123def456",
                             "App ID": "Aabc123de", "Payload": "{}", "Status": "done", "Created": now_iso(),
                             "Result": json.dumps({"sent_msg": 800, "pdf": "x.pdf"})})
    sheet.flush()
    bot.handle(sheet, msg(90, "I already applied", reply_to_message={"message_id": 800, "caption": "📄 Tailored CV"}),
               by="mac", mac_online=True)
    bot.handle(sheet, msg(91, "what does this mean?", reply_to_message={"message_id": 801, "text": "📊 Status: 3 applications"}),
               by="mac", mac_online=True)
    chats = [json.loads(t["Payload"]) for t in sheet.t("Queue").rows if t["Type"] == "chat"]
    assert chats[0]["job_id"] == "abc123def456" and "Tailored CV" in chats[0]["replied_text"]
    assert chats[1]["job_id"] == "" and "3 applications" in chats[1]["replied_text"]


def test_chat_job_actions_become_confirm_buttons(sheet, sent):
    rows, cards = writing._act_buttons(sheet, [("applied", "abc123def456"), ("closed", "nope"), ("card", "abc123def456")])
    assert rows == [[("📨 I applied: Acme", "japplied:abc123def456")]] and len(cards) == 1
    bot.handle(sheet, cb("japplied:abc123def456", 5), by="mac", mac_online=True)
    app = sheet.t("Applications").rows[0]
    assert app["Status"] == "Applied" and app["Date applied"]
    rows, _ = writing._act_buttons(sheet, [("applied", "abc123def456")])
    assert rows == [[("📨 I applied: Acme", f"applied:{app['App ID']}")]]


def test_mail_now_is_queued_once(sheet, sent):
    bot.handle(sheet, msg(95, "/mail"), by="mac", mac_online=True)
    bot.handle(sheet, cb("mailnow:", 96), by="mac", mac_online=True)
    assert [t["Type"] for t in sheet.t("Queue").rows] == ["mail_now"]
    assert "Already checking" in sent[-1]


POSTING = ("Software Engineer at At-Bay. Responsibilities: design, build, and maintain high-scale Python "
           "microservices and their infrastructure. Deploy robust code to production. Qualifications: 5+ years "
           "of experience as a software engineer. Strong fundamentals: OOP, design patterns. Experience with AWS. "
           "Nice to have: ML concepts, MLOps tooling, data cleaning. Please send your CV. Location: Manchester.")


def test_reply_with_a_posting_is_attached_to_that_job(sheet, sent):
    sheet.t("Jobs").rows[0]["Alert msg ID"] = "700"
    bot.handle(sheet, msg(100, POSTING, reply_to_message={"message_id": 700, "text": "5. Software Engineer"}),
               by="mac", mac_online=True)
    (t,) = sheet.t("Queue").rows
    p = json.loads(t["Payload"])
    assert t["Type"] == "intake" and p["for_job"] == "abc123def456" and "5+ years" in p["text"]
    assert "Got the job description" in sent[-1]
    bot.handle(sheet, msg(101, "send again", reply_to_message={"message_id": 700}), by="mac", mac_online=True)
    assert len(sheet.t("Queue").rows) == 1                                   # a short "send again" still resends


def test_attach_posting_scores_saves_and_builds(sheet, sent, monkeypatch, tmp_path):
    from agent import intake, tasks
    monkeypatch.setattr(intake, "POSTS_DIR", tmp_path)
    job = sheet.t("Jobs").rows[0]
    job["Source"] = "linkedin"
    assert intake.attach_posting(sheet, "abc123def456", POSTING, print) == "approved"
    assert (tmp_path / "abc123def456.txt").read_text(encoding="utf-8") == POSTING
    assert job["Score"] and job["Gaps"] is not None
    assert [t["Type"] for t in sheet.t("Queue").rows] == ["build_cv"]
    assert tasks._job_from_sheet(sheet, "abc123def456")["description"] == POSTING   # the CV uses it
    app = sheet.t("Applications").rows[0]
    app["Status"] = "CV ready"
    sheet.t("Queue").rows[0]["Status"] = "done"
    assert intake.attach_posting(sheet, "abc123def456", POSTING, print) == "attached"
    assert json.loads(sheet.t("Queue").rows[-1]["Payload"]) == {"mode": "posting"}
    app["Status"] = "Applied"
    n = len(sheet.t("Queue").rows)
    intake.attach_posting(sheet, "abc123def456", POSTING, print)
    assert len(sheet.t("Queue").rows) == n and "already applied" in sent[-1]       # applied: no new CV
