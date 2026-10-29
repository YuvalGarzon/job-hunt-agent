"""Running any handler twice must never duplicate a row, a task or a status change."""
import json

import pytest

from agent import bot
from agent import telegram as tg
from agent.sheet import TABS, Sheet, Table


class FakeSheet(Sheet):
    def __init__(self):
        self.tables = {n: Table(self, n, [TABS[n]]) for n in TABS}
        self._appends, self._updates = {}, []

    def load(self, *names):
        pass

    def forget(self):
        pass

    def flush(self):
        for name, rows in self._appends.items():
            t = self.tables[name]
            for r in rows:
                r["_row"] = t._next_row
                t._next_row += 1
        self._appends, self._updates = {}, []


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: out.append(text) or {"message_id": 1})
    monkeypatch.setattr(tg, "edit_buttons", lambda *a, **k: None)
    monkeypatch.setattr(tg, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(tg, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(tg, "chat_id", lambda: "42")
    return out


@pytest.fixture
def sheet():
    s = FakeSheet()
    s.t("Jobs").append({"Job ID": "abc123def456", "Company": "Acme", "Title": "Data Engineer", "City": "Manchester",
                        "Location": "Manchester", "Source": "greenhouse", "URL": "https://x", "Score": "70",
                        "First seen": "2026-10-22T10:00:00+00:00", "Decision": "alerted"})
    s.flush()
    return s


def cb(data, uid=1, chat="42"):
    return {"update_id": uid, "callback_query": {"id": "c", "data": data, "from": {"id": int(chat)},
                                                 "message": {"message_id": 5, "chat": {"id": int(chat)}}}}


def test_approve_twice_creates_one_application_and_task(sheet, sent):
    bot.handle(sheet, cb("ap:abc123def456"), by="mac", mac_online=True)
    bot.handle(sheet, cb("ap:abc123def456", 2), by="cloud", mac_online=False)
    assert len(sheet.t("Applications").rows) == 1
    assert len(sheet.t("Queue").rows) == 1
    assert sheet.t("Applications").rows[0]["Status"] == "Approved"


def test_approve_offline_says_queued(sheet, sent):
    bot.handle(sheet, cb("ap:abc123def456"), by="cloud", mac_online=False)
    assert any("Queued" in m for m in sent)


def test_applied_twice(sheet, sent):
    bot.handle(sheet, cb("ap:abc123def456"), by="mac", mac_online=True)
    app_id = bot.app_id_for("abc123def456")
    bot.handle(sheet, cb(f"applied:{app_id}", 2), by="mac", mac_online=True)
    first = dict(sheet.t("Applications").rows[0])
    bot.handle(sheet, cb(f"applied:{app_id}", 3), by="mac", mac_online=True)
    second = sheet.t("Applications").rows[0]
    assert first["Status"] == "Applied" and second["Date applied"] == first["Date applied"]
    assert second["Follow-up due"]


def test_inbox_records_each_update_once(sheet, sent):
    ups = [cb("sk:abc123def456", 7), cb("sk:abc123def456", 7), cb("noop:", 8)]
    assert bot.record(sheet, ups, by="mac") == 2
    assert bot.record(sheet, ups, by="cloud") == 0


def test_other_chats_ignored(sheet, sent):
    bot.handle(sheet, cb("ap:abc123def456", chat="999"), by="mac", mac_online=True)
    assert sheet.t("Applications").rows == []


def test_threshold_command(sheet, sent):
    msg = {"update_id": 9, "message": {"chat": {"id": 42}, "text": "/threshold 60"}}
    bot.handle(sheet, msg, by="mac", mac_online=True)
    assert sheet.kv_get("Settings", "fit_threshold") == "60"
    bot.handle(sheet, {"update_id": 10, "message": {"chat": {"id": 42}, "text": "/threshold abc"}}, by="mac", mac_online=True)
    assert sheet.kv_get("Settings", "fit_threshold") == "60"


def test_fix_flow_queues_one_task(sheet, sent):
    bot.handle(sheet, cb("ap:abc123def456"), by="mac", mac_online=True)
    app_id = bot.app_id_for("abc123def456")
    bot.handle(sheet, cb(f"fix:{app_id}", 2), by="mac", mac_online=True)
    bot.handle(sheet, {"update_id": 3, "message": {"chat": {"id": 42}, "text": "Put the crowd anomaly project first"}},
               by="mac", mac_online=True)
    fixes = [r for r in sheet.t("Queue").rows if json.loads(r["Payload"]).get("mode") == "fix"]
    assert len(fixes) == 1 and "crowd" in json.loads(fixes[0]["Payload"])["feedback"]
    assert sheet.kv_get("Settings", "awaiting_fix") == ""


def test_reply_to_alert_resends_it(sheet, sent):
    sheet.t("Jobs").rows[0]["Alert msg ID"] = "12"
    sheet.t("Jobs").rows[0]["Rank"] = "4"
    msg = {"update_id": 20, "message": {"chat": {"id": 42}, "text": "send me this job again",
                                        "reply_to_message": {"message_id": 12}}}
    bot.handle(sheet, msg, by="mac", mac_online=True)
    assert any("Data Engineer" in m for m in sent)
    assert sheet.t("Queue").rows == []          # not sent to free chat
