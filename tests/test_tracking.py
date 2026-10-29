from agent import tracking
from tests.test_bot_idempotency import sheet, sent  # noqa: F401  (fixtures)

APPS = [{"App ID": "A1", "Company": "Brightwave", "Role": "Backend Engineer", "Status": "Applied"},
        {"App ID": "A2", "Company": "Cato Networks", "Role": "Network Automation Engineer", "Status": "CV ready"}]


def m(sender, subject, text=""):
    return {"from": sender, "subject": subject, "text": text}


def test_match_by_sender_or_subject():
    assert tracking.match_app(m("no-reply@brightwave.com", "Thank you from Brightwave"), APPS)["App ID"] == "A1"
    assert tracking.match_app(m("Recruiting <jobs@x.com>", "Your application to Cato Networks"), APPS)["App ID"] == "A2"


def test_match_by_body_only_with_ats_sender():
    body = "Thank you for applying to Brightwave for the Backend Engineer role."
    assert tracking.match_app(m("no-reply@us.greenhouse-mail.io", "Thank You For Applying", body), APPS)["App ID"] == "A1"
    assert tracking.match_app(m("newsletter@somesite.com", "Weekly news", body), APPS) is None


def test_email_older_than_the_application_is_ignored():
    apps = [{**APPS[0], "Date approved": "2026-10-22"}]
    old = {**m("no-reply@brightwave.com", "Thank you from Brightwave"), "received_ms": 1792494000000}   # 2026-10-20
    new = {**old, "received_ms": 1792700000000}                                                         # 2026-10-23
    assert tracking.match_app(old, apps) is None
    assert tracking.match_app(new, apps)["App ID"] == "A1"


def test_acknowledgment_never_reopens_a_rejection(sheet):
    app = _app(sheet, "Rejected", **{"Date applied": "2026-10-22"})
    c = {"type": "acknowledgment", "summary": "received", "deadline": ""}
    assert tracking.apply_event(sheet, app, MAIL, c) == "Rejected"


def test_unrelated_company_ignored():
    assert tracking.match_app(m("rotem.mor@northwind.io", "Information about your application to Northwind"), APPS) is None


def _app(sheet, status, **kw):
    row = {"App ID": "A1", "Company": "Brightwave", "Role": "Backend", "Status": status, "Date applied": "",
           "Follow-up due": "", "Next step": "", "Job ID": "j1", **kw}
    sheet.t("Applications").append(row)
    sheet.flush()
    return sheet.t("Applications").find("App ID", "A1")


MAIL = {"id": "m1", "from": "x@brightwave.com", "received_ms": 1792700000000, "link": "https://mail.google.com/x"}


def test_rejection_sets_rejected_and_event_once(sheet):
    app = _app(sheet, "Applied", **{"Date applied": "2026-10-22"})
    c = {"type": "rejection", "summary": "rejected", "deadline": ""}
    assert tracking.apply_event(sheet, app, MAIL, c) == "Rejected"
    tracking.apply_event(sheet, app, MAIL, c)
    assert len(sheet.t("Events").rows) == 1


def test_acknowledgment_marks_applied_but_never_moves_back(sheet):
    app = _app(sheet, "CV ready")
    c = {"type": "acknowledgment", "summary": "received", "deadline": ""}
    assert tracking.apply_event(sheet, app, MAIL, c) == "Applied"
    assert app["Date applied"] and app["Follow-up due"]
    app["Status"] = "Interview"
    assert tracking.apply_event(sheet, app, {**MAIL, "id": "m2"}, c) == "Interview"


def test_followup_reminder_once(sheet, sent, monkeypatch):
    from datetime import datetime
    from agent.sheet import TZ
    _app(sheet, "Applied", **{"Date applied": "2026-10-10", "Follow-up due": "2026-10-17"})
    sheet.t("Health").rows.clear()
    monkeypatch.setattr(tracking, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime(2026, 10, 23, 10, tzinfo=TZ)),
                                                            "fromisoformat": datetime.fromisoformat,
                                                            "fromtimestamp": datetime.fromtimestamp}))
    assert tracking.followups(sheet) == 1
    assert tracking.followups(sheet) == 0
    assert "7 days" in sent[0]


def test_followup_button_always_answers(sheet, sent):
    from agent import bot
    _app(sheet, "Applied", **{"Date applied": "2026-10-10"})
    cb = lambda uid: {"update_id": uid, "callback_query": {"id": "c", "data": "fu:A1", "from": {"id": 42},
                                                           "message": {"message_id": 5, "chat": {"id": 42}}}}
    bot.handle(sheet, cb(1), by="mac", mac_online=True)
    bot.handle(sheet, cb(2), by="mac", mac_online=True)          # second tap while the first is pending
    assert len([t for t in sheet.t("Queue").rows if t["Type"] == "followup_draft"]) == 1
    assert "Already writing" in sent[-1]
    sheet.t("Queue").rows[0]["Status"] = "done"
    bot.handle(sheet, cb(3), by="mac", mac_online=True)          # a later tap: a new draft
    assert len([t for t in sheet.t("Queue").rows if t["Type"] == "followup_draft"]) == 2


def test_mail_notice_shows_sender_and_copyable_subject(sheet, monkeypatch):
    from agent import telegram as tg
    calls = []
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: calls.append((text, buttons)) or {"message_id": 1})
    _app(sheet, "Applied", **{"Date applied": "2026-10-22"})
    mail = {**MAIL, "from": '"Arm @ icims" <arm+autoreply@talent.icims.com>', "subject": "Thank you for applying <Arm>",
            "text": "", "thread": "t1"}
    monkeypatch.setattr(tracking.gmail, "message", lambda mid: mail)
    monkeypatch.setattr(tracking, "classify", lambda m, app: {"type": "acknowledgment", "summary": "received", "deadline": ""})
    tracking.mail_event(sheet, {"App ID": "A1"}, {"message_id": "m1"}, print)
    text, buttons = calls[-1]
    assert "From: Arm @ icims" in text and "<code>Thank you for applying &lt;Arm&gt;</code>" in text
    assert buttons == [[("📧 Open email", "https://mail.google.com/x")]]
