import json
from pathlib import Path

from agent import bot
from agent.truth_check import check_text
from tests.test_bot_idempotency import cb, sheet, sent  # noqa: F401  (fixtures)


def msg(uid, text, **extra):
    return {"update_id": uid, "message": {"message_id": 9, "chat": {"id": 42}, "from": {"id": 42}, "text": text, **extra}}


def _app(sheet):
    sheet.t("Applications").append({"App ID": "A1", "Company": "Acme", "Role": "Data Engineer", "Status": "CV ready",
                                    "Job ID": "abc123def456"})
    sheet.flush()


def test_cover_letter_asks_then_queues_once(sheet, sent):
    _app(sheet)
    bot.handle(sheet, cb("cl:A1", 1), by="mac", mac_online=True)
    assert "cover letter" in sent[-1]
    bot.handle(sheet, cb("clw:en:A1", 2), by="mac", mac_online=True)
    bot.handle(sheet, cb("clw:en:A1", 3), by="mac", mac_online=True)     # double tap
    q = [t for t in sheet.t("Queue").rows if t["Type"] == "cover_letter"]
    assert len(q) == 1 and json.loads(q[0]["Payload"]) == {"lang": "en", "mode": "new"}


def test_cover_letter_fix_uses_his_words(sheet, sent):
    _app(sheet)
    bot.handle(sheet, cb("clfx:he:A1", 1), by="mac", mac_online=True)
    bot.handle(sheet, msg(2, "shorter, and mention the CV project first"), by="mac", mac_online=True)
    q = [t for t in sheet.t("Queue").rows if t["Type"] == "cover_letter"]
    assert len(q) == 1
    assert json.loads(q[0]["Payload"]) == {"lang": "he", "mode": "fix", "feedback": "shorter, and mention the CV project first"}


def test_cover_letter_good_marks_y(sheet, sent):
    _app(sheet)
    bot.handle(sheet, cb("clok:A1", 1), by="mac", mac_online=True)
    assert sheet.t("Applications").find("App ID", "A1")["Cover letter (Y/N)"] == "Y"


def test_chat_reply_to_alert_carries_the_job(sheet, sent):
    sheet.t("Jobs").rows[0]["Alert msg ID"] = "77"
    bot.handle(sheet, msg(5, "is this a good fit for me?", reply_to_message={"message_id": 77}), by="mac", mac_online=True)
    q = sheet.t("Queue").rows[-1]
    assert q["Type"] == "chat" and json.loads(q["Payload"])["job_id"] == "abc123def456"


def test_prep_and_cv_commands(sheet, sent):
    bot.handle(sheet, msg(6, "/prep Brightwave"), by="mac", mac_online=True)
    bot.handle(sheet, msg(7, "/cv https://jobs.lever.co/acme/1"), by="mac", mac_online=True)
    types = [(t["Type"], json.loads(t["Payload"]).get("approve")) for t in sheet.t("Queue").rows]
    assert types == [("prep", None), ("intake", True)]


def test_check_text_catches_invented_claims():
    assert check_text("I completed my B.Sc. in Computer Science at Northfield University.") == []
    bad = check_text("As a passionate student with 7 years of Kubernetes experience in production")
    assert any("passionate" in b for b in bad) and any("Kubernetes" in b for b in bad)
    assert check_text("Your stack uses Kubernetes.", job_text="We run on Kubernetes") == []


def test_threshold_buttons_and_pick(sheet, sent, monkeypatch):
    from agent import pipeline
    monkeypatch.setattr(pipeline, "threshold", lambda s: 50)
    bot.handle(sheet, msg(20, "/threshold"), by="mac", mac_online=True)
    assert "threshold is now" in sent[-1]
    bot.handle(sheet, cb("thr:60", 21), by="mac", mac_online=True)
    assert str(sheet.kv_get("Settings", "fit_threshold")) == "60"


def test_bare_cv_then_link_builds_cv(sheet, sent):
    bot.handle(sheet, msg(22, "/cv"), by="mac", mac_online=True)
    bot.handle(sheet, msg(23, "https://jobs.lever.co/acme/1"), by="mac", mac_online=True)
    q = sheet.t("Queue").rows[-1]
    assert q["Type"] == "intake" and json.loads(q["Payload"]).get("approve") is True
    bot.handle(sheet, msg(24, "https://jobs.lever.co/acme/2"), by="mac", mac_online=True)
    assert json.loads(sheet.t("Queue").rows[-1]["Payload"]).get("approve") is None


def test_prep_buttons(sheet, sent):
    _app(sheet)
    bot.handle(sheet, msg(25, "/prep"), by="mac", mac_online=True)
    assert "which company" in sent[-1]
    bot.handle(sheet, cb("prep:Acme", 26), by="mac", mac_online=True)
    assert json.loads(sheet.t("Queue").rows[-1]["Payload"]) == {"company": "Acme"}


def test_cancel_stops_waiting_for_a_fix(sheet, sent):
    _app(sheet)
    bot.handle(sheet, cb("clfx:en:A1", 30), by="mac", mac_online=True)
    bot.handle(sheet, cb("cancel:", 31), by="mac", mac_online=True)
    bot.handle(sheet, msg(32, "what do you think about Acme?"), by="mac", mac_online=True)
    types = [t["Type"] for t in sheet.t("Queue").rows]
    assert types == ["chat"]                                   # not taken as a fix


def test_cancel_command_after_cv(sheet, sent):
    bot.handle(sheet, msg(33, "/cv"), by="mac", mac_online=True)
    bot.handle(sheet, msg(34, "/cancel"), by="mac", mac_online=True)
    assert "Cancelled" in sent[-1]
    bot.handle(sheet, msg(35, "https://jobs.lever.co/acme/1"), by="mac", mac_online=True)
    assert json.loads(sheet.t("Queue").rows[-1]["Payload"]).get("approve") is None


def test_open_sends_cards_with_buttons(sheet, sent, monkeypatch):
    from agent import telegram as tg
    calls = []
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: calls.append((text, buttons)) or {"message_id": 9})
    sheet.t("Jobs").rows[0].update({"First seen": "2099-01-01 10:00:00+03:00", "Score": "70", "User action": ""})
    _app(sheet)
    bot.handle(sheet, msg(40, "/open"), by="mac", mac_online=True)
    cards = [b for t, b in calls if b and b[0][0][1].startswith("ap:")]
    assert len(cards) == 1                                  # the open job, with Approve / Skip
    assert any(b and any(x[1].startswith("applied:") for row in b for x in row) for t, b in calls)   # CV with I applied


def test_chat_action_tokens_become_cards(monkeypatch, sheet, sent):
    from agent import alerts, claude_cli, writing
    monkeypatch.setattr(claude_cli, "ask", lambda *a, **k: "Here are your open jobs: [[PENDING_JOBS]]")
    shown = []
    monkeypatch.setattr(alerts, "send_pending", lambda s, offset=0: shown.append("jobs") or 1)
    monkeypatch.setattr(writing, "_log_turn", lambda *a: None)
    out = writing.chat(sheet, {}, {"text": "send me the jobs I did nothing with"}, print)
    assert shown == ["jobs"] and "PENDING_JOBS" in out
    assert all("[[" not in m for m in sent)                 # the token never reaches Telegram


def test_same_job_twice_warns_before_second_cv(sheet, sent):
    sheet.t("Applications").append({"App ID": "Aold", "Company": "Acme Ltd", "Role": "Data Engineer", "Status": "Applied",
                                    "Date applied": "2026-10-20", "Job ID": "other000001"})
    sheet.flush()
    bot.handle(sheet, cb("ap:abc123def456", 50), by="mac", mac_online=True)    # same company + title, other link
    assert "same job" in sent[-1]
    assert not [t for t in sheet.t("Queue").rows if t["Type"] == "build_cv"]
    bot.handle(sheet, cb("apy:abc123def456", 51), by="mac", mac_online=True)   # "yes, it is another job"
    assert len([t for t in sheet.t("Queue").rows if t["Type"] == "build_cv"]) == 1


def test_open_list_warns_instead_of_hiding(sheet, monkeypatch):
    from agent import alerts, telegram as tg
    calls = []
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: calls.append((text, buttons)) or {"message_id": 9})
    sheet.t("Jobs").rows[0].update({"First seen": "2099-01-01 10:00:00+03:00", "Score": "70", "User action": ""})
    sheet.t("Applications").append({"App ID": "Aold", "Company": "Acme", "Role": "Data Engineer", "Status": "Applied",
                                    "Date applied": "2026-10-20", "Date approved": "2026-10-19", "Job ID": "other000001"})
    sheet.flush()
    assert len(alerts.pending_rows(sheet)) == 1                       # still shown
    alerts.send_pending(sheet)
    card = [(t, b) for t, b in calls if b and b[0][0][1].startswith("apy:")]
    assert card and "Maybe the same job" in card[0][0]                  # with a warning, Approve = confirmed


def test_twin_note_round_trip():
    from agent import alerts
    note = alerts.twin_note({"App ID": "A1", "Role": "Backend Engineer", "Company": "Brightwave",
                             "Date applied": "2026-10-22", "Status": "Applied"})
    text = alerts.job_text({"Title": "Backend Engineer", "Company": "Brightwave", "City": "Salford", "Location": "",
                            "Location flag": "", "Score": "70", "Fit": "", "Gaps": "", "Stretch": "", "Source": "greenhouse",
                            "URL": "https://x", "Reason": note + "; title not in page HTML"}, 1)
    assert "Maybe the same job" in text and "Backend Engineer · Brightwave" in text and "applied 2026-10-22" in text


def test_skip_then_undo(sheet, sent):
    from agent import alerts
    sheet.t("Jobs").rows[0].update({"First seen": "2099-01-01 10:00:00+03:00", "Score": "70", "User action": ""})
    bot.handle(sheet, cb("sk:abc123def456", 60), by="mac", mac_online=True)
    assert sheet.t("Jobs").rows[0]["User action"] == "skipped" and alerts.pending_rows(sheet) == []
    bot.handle(sheet, cb("unsk:abc123def456", 61), by="mac", mac_online=True)
    assert sheet.t("Jobs").rows[0]["User action"] == "" and len(alerts.pending_rows(sheet)) == 1
    assert "Skip undone" in sent[-1]


def test_undo_never_touches_an_approved_job(sheet, sent):
    sheet.t("Jobs").rows[0]["User action"] = "approved"
    bot.handle(sheet, cb("unsk:abc123def456", 62), by="mac", mac_online=True)
    assert sheet.t("Jobs").rows[0]["User action"] == "approved"


def _replied_card(sheet, sent_calls, action):
    sheet.t("Jobs").rows[0].update({"Alert msg ID": "500", "User action": action, "Rank": "1"})
    bot.handle(sheet, msg(70, "send me this job again", reply_to_message={"message_id": 500}), by="mac", mac_online=True)
    return sent_calls[-1]


def test_resend_keeps_the_right_buttons(sheet, monkeypatch):
    from agent import telegram as tg
    calls = []
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: calls.append((text, buttons)) or {"message_id": 501})
    monkeypatch.setattr(tg, "edit_buttons", lambda *a, **k: None)
    monkeypatch.setattr(tg, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(tg, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(tg, "chat_id", lambda: "42")
    text, buttons = _replied_card(sheet, calls, "")
    assert [x[1] for x in buttons[0]][:2] == ["ap:abc123def456", "sk:abc123def456"]
    text, buttons = _replied_card(sheet, calls, "skipped")
    assert "You skipped" in text and [x[1] for x in buttons[0]] == ["apy:abc123def456", "unsk:abc123def456"]
    assert str(sheet.t("Jobs").rows[0]["Alert msg ID"]) == "501"          # the new card is the current one


def test_cv_cancel_keeps_everything_and_resume_restores(sheet, sent):
    sheet.t("Applications").append({"App ID": "A9", "Company": "Motorola Solutions", "Role": "ML Audio Engineer",
                                    "Status": "CV ready", "CV path": "CVs/ML Engineer CV/x/Alex Morgan CV.pdf",
                                    "Job ID": "j9", "Date applied": ""})
    sheet.flush()
    bot.handle(sheet, cb("cvx:A9", 80), by="mac", mac_online=True)
    app = sheet.t("Applications").find("App ID", "A9")
    assert app["Status"] == "Withdrawn" and app["CV path"]                    # nothing deleted
    assert "Nothing was deleted" in sent[-1]
    bot.handle(sheet, cb("cvres:A9", 81), by="mac", mac_online=True)
    assert sheet.t("Applications").find("App ID", "A9")["Status"] == "CV ready"


def test_resume_after_applied_goes_back_to_applied(sheet, sent):
    sheet.t("Applications").append({"App ID": "A8", "Company": "Acme", "Role": "Dev", "Status": "Withdrawn",
                                    "CV path": "x/Alex Morgan CV.pdf", "Date applied": "2026-10-20", "Job ID": "j8"})
    sheet.flush()
    bot.handle(sheet, cb("cvres:A8", 82), by="mac", mac_online=True)
    assert sheet.t("Applications").find("App ID", "A8")["Status"] == "Applied"


def test_skipped_lists_cancelled_cvs_with_resume(sheet, monkeypatch):
    from agent import telegram as tg
    calls = []
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: calls.append((text, buttons)) or {"message_id": 9})
    monkeypatch.setattr(tg, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(tg, "chat_id", lambda: "42")
    sheet.t("Applications").append({"App ID": "A7", "Company": "Acme", "Role": "Dev", "Status": "Withdrawn",
                                    "CV path": "x/Alex Morgan CV.pdf", "Last update": "2099-01-01 10:00:00+03:00", "Job ID": "j7"})
    sheet.flush()
    bot.handle(sheet, msg(90, "/skipped"), by="mac", mac_online=True)
    assert any(b and b[0][0][1] == "cvres:A7" for t, b in calls)


def test_chat_without_a_reply_is_about_the_last_job_sent(monkeypatch, sheet, sent):
    from agent import claude_cli, tasks, writing
    monkeypatch.setattr(writing, "CHAT_LOG", Path("/nonexistent/chat.jsonl"))
    asked = []
    monkeypatch.setattr(claude_cli, "ask", lambda prompt, **k: asked.append(prompt) or "Yes, TypeScript is in the requirements.")
    monkeypatch.setattr(tasks, "_job_from_sheet", lambda s, job_id: {"description": f"Requirements for {job_id}: TypeScript"})
    monkeypatch.setattr(writing, "_log_turn", lambda *a: None)
    job = sheet.t("Jobs").rows[0]
    job["Alert msg ID"] = 1900
    sheet.t("Queue").append({"Task ID": "cv-A1", "Type": "build_cv", "Job ID": job["Job ID"], "Status": "done",
                             "Result": '{"sent_msg": 1951, "pdf": "x.pdf"}'})
    assert writing.last_sent_job(sheet) == job["Job ID"]
    writing.chat(sheet, {}, {"text": "Does the job ask for TypeScript?"}, print)
    assert f"job id {job['Job ID']}" in asked[0] and f"Requirements for {job['Job ID']}: TypeScript" in asked[0]


def _chat_prompt(monkeypatch, sheet, text, payload_extra=None, log=None):
    from agent import claude_cli, tasks, writing
    asked = []
    monkeypatch.setattr(claude_cli, "ask", lambda prompt, **k: asked.append(prompt) or "OK.")
    monkeypatch.setattr(tasks, "_job_from_sheet", lambda s, job_id: {"description": f"posting of {job_id}"})
    if log:
        monkeypatch.setattr(writing, "CHAT_LOG", log)
    else:
        monkeypatch.setattr(writing, "_log_turn", lambda *a: None)
        monkeypatch.setattr(writing, "CHAT_LOG", Path("/nonexistent/chat.jsonl"))
    writing.chat(sheet, {}, {"text": text, **(payload_extra or {})}, print)
    return asked[0]


def test_chat_reply_says_whether_he_applied_and_knows_the_sheet(monkeypatch, sheet, sent):
    job = sheet.t("Jobs").rows[0]
    prompt = _chat_prompt(monkeypatch, sheet, "Did I apply to this job?", {"job_id": job["Job ID"]})
    assert f"job id {job['Job ID']}" in prompt and "NONE, not applied" in prompt
    assert "means THIS one" in prompt and "never say you cannot see it" in prompt


def test_chat_finds_the_job_of_a_pasted_posting(monkeypatch, sheet, sent):
    job = sheet.t("Jobs").rows[0]
    job["Title"], job["Company"] = "Software Solution Architect, NVIS", "NVIDIA"
    prompt = _chat_prompt(monkeypatch, sheet, "Software Solution Architect, NVIS\nApply\nAs a Software Solution "
                                              "Architect, NVIS at NVIDIA, you will lead...")
    assert f"job id {job['Job ID']}" in prompt and "contains this job's posting" in prompt


def test_chat_follow_up_stays_on_the_job_just_discussed(monkeypatch, sheet, sent, tmp_path):
    from agent import writing
    first = sheet.t("Jobs").rows[0]
    sheet.t("Jobs").append({**first, "Job ID": "0ther0job000", "Title": "Another Engineer",
                            "Alert msg ID": 1999})     # a newer card was sent meanwhile
    log = tmp_path / "chat.jsonl"
    _chat_prompt(monkeypatch, sheet, "Did I apply to this job?", {"job_id": first["Job ID"]}, log=log)
    assert writing._recent_chat_job() == first["Job ID"]
    from agent import claude_cli
    asked = []
    monkeypatch.setattr(claude_cli, "ask", lambda prompt, **k: asked.append(prompt) or "OK.")
    writing.chat(sheet, {}, {"text": "Check the sheet"}, print)
    assert f"job id {first['Job ID']}" in asked[0] and "a follow-up is still about it" in asked[0]


def test_chat_new_tokens_give_buttons_and_add_a_pasted_job(monkeypatch, sheet, sent):
    from agent import claude_cli, telegram as tg, writing
    job = sheet.t("Jobs").rows[0]
    _app(sheet)
    sheet.t("Applications").rows[-1]["Job ID"] = job["Job ID"]
    calls = []
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: calls.append((text, buttons)) or {"message_id": 9})
    monkeypatch.setattr(tg, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(writing, "_log_turn", lambda *a: None)
    monkeypatch.setattr(writing, "_posting", lambda s, j: "")
    monkeypatch.setattr(claude_cli, "ask", lambda prompt, **k: f"Tap the button. [[ACT:fix:{job['Job ID']}]] "
                        f"[[ACT:cover:{job['Job ID']}]] [[ACT:prep:{job['Job ID']}]] [[MAIL_NOW]] [[ADD_JOB_CV]]")
    writing.chat(sheet, {"Task ID": "chat-7"}, {"text": "Backend Engineer at Foo, requirements: Python " * 10}, print)
    text, buttons = calls[-1]
    assert "[[" not in text
    data = [b[1] for row in buttons for b in row]
    assert data == ["fix:A1", "cl:A1", f"prep:{job['Company'][:40]}", "mailnow:"]
    intake = [t for t in sheet.t("Queue").rows if t["Type"] == "intake"]
    assert len(intake) == 1 and json.loads(intake[0]["Payload"])["approve"] is True


def test_chat_fix_without_a_cv_shows_no_button(monkeypatch, sheet, sent):
    from agent import writing
    job = sheet.t("Jobs").rows[0]
    rows, cards = writing._act_buttons(sheet, [("fix", job["Job ID"]), ("cover", job["Job ID"])])
    assert rows == [] and cards == []


def test_chat_knows_a_cv_still_being_built_is_not_ready(monkeypatch, sheet, sent):
    from agent import bot as b
    job = sheet.t("Jobs").rows[0]
    b.handle(sheet, cb(f"ap:{job['Job ID']}"), by="mac", mac_online=True)      # approve: CV build queued
    prompt = _chat_prompt(monkeypatch, sheet, "Where is the CV?", {"job_id": job["Job ID"]})
    assert "BEING BUILT RIGHT NOW" in prompt and "CV being built now" in prompt
    task = next(t for t in sheet.t("Queue").rows if t["Type"] == "build_cv")
    sheet.t("Queue").update(task, Status="done")
    sheet.t("Applications").update(sheet.t("Applications").rows[0], **{"CV path": "CVs/x/Alex Morgan CV.pdf"})
    prompt = _chat_prompt(monkeypatch, sheet, "Where is the CV?", {"job_id": job["Job ID"]})
    assert "already sent in this chat" in prompt and "BEING BUILT" not in prompt
