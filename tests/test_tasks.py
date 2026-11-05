"""The Mac task path: approve -> build_cv task -> PDF sent once -> Applications updated."""
from pathlib import Path

import pytest

from agent import bot, config, cv_builder, sources, tasks
from agent import telegram as tg
from tests.test_bot_idempotency import FakeSheet


@pytest.fixture
def env(monkeypatch, tmp_path):
    sent = {"docs": [], "msgs": []}
    monkeypatch.setattr(tg, "send_message", lambda text, buttons=None, **k: sent["msgs"].append(text) or {"message_id": 1})
    monkeypatch.setattr(tg, "send_document", lambda path, caption="", buttons=None: sent["docs"].append((path, buttons)) or {"message_id": 77})
    monkeypatch.setattr(tg, "edit_buttons", lambda *a, **k: None)
    monkeypatch.setattr(tg, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(tg, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(tg, "chat_id", lambda: "42")
    monkeypatch.setattr(sources, "fetch_one", lambda ref: {"title": "Data Engineer", "description": "Python, SQL", "url": "https://x"})
    calls = []

    def fake_build(job, out_dir=None, feedback=None, previous_content=None, failed=None, log=print):
        assert isinstance(job, dict) and job["company"] == "Acme" and job["description"]
        calls.append({"out_dir": out_dir, "feedback": feedback, "previous": previous_content, "failed": failed})
        pdf = config.ROOT / "CVs" / "_test" / config.cv_file_name()
        return {"pdf": pdf, "content": pdf.with_name("content.yaml"), "dir": pdf.parent, "fit": "fits",
                "gaps": ["Kafka"], "preview": None, "flags": {}}
    monkeypatch.setattr(cv_builder, "build", fake_build)
    s = FakeSheet()
    s.t("Jobs").append({"Job ID": "abc123def456", "Company": "Acme", "Title": "Data Engineer", "City": "Manchester",
                        "Location": "Manchester", "Source": "greenhouse", "Source ref": "greenhouse:acme:1",
                        "URL": "https://x", "Score": "70", "First seen": "2026-10-22T10:00:00+00:00"})
    s.flush()
    return s, sent, calls


def cb(data, uid=1):
    return {"update_id": uid, "callback_query": {"id": "c", "data": data, "from": {"id": 42},
                                                 "message": {"message_id": 5, "chat": {"id": 42}}}}


def test_approve_builds_and_sends_once(env):
    s, sent, calls = env
    bot.handle(s, cb("ap:abc123def456"), by="mac", mac_online=True)
    assert tasks.run_one(s) is True
    assert len(sent["docs"]) == 1
    app = s.t("Applications").rows[0]
    assert app["Status"] == "CV ready" and app["CV path"].endswith("Alex Morgan CV.pdf")
    q = s.t("Queue").rows[0]
    assert q["Status"] == "done" and '"sent_msg": 77' in q["Result"]
    # Running again (e.g. after a restart) sends nothing new
    q["Status"] = "pending"
    tasks.run_one(s)
    assert len(sent["docs"]) == 1


def test_fix_passes_feedback_and_reuses_folder(env):
    s, sent, calls = env
    bot.handle(s, cb("ap:abc123def456"), by="mac", mac_online=True)
    tasks.run_one(s)
    app_id = bot.app_id_for("abc123def456")
    bot.handle(s, cb(f"fix:{app_id}", 2), by="mac", mac_online=True)
    bot.handle(s, {"update_id": 3, "message": {"chat": {"id": 42}, "text": "shorter summary"}}, by="mac", mac_online=True)
    tasks.run_one(s)
    assert calls[1]["feedback"] == "shorter summary"
    assert calls[1]["out_dir"] == calls[0]["out_dir"] or str(calls[1]["out_dir"]).endswith("_test")
    assert len(sent["docs"]) == 2


def test_failure_backs_off(env, monkeypatch):
    s, sent, calls = env
    monkeypatch.setattr(cv_builder, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    bot.handle(s, cb("ap:abc123def456"), by="mac", mac_online=True)
    tasks.run_one(s)
    q = s.t("Queue").rows[0]
    assert q["Status"] == "pending" and "retry_at" in q["Payload"]
    assert tasks.run_one(s) is False            # not retried before its time


def test_cv_out_of_rounds_goes_on_at_once_from_its_last_version(env, monkeypatch, tmp_path):
    s, sent, calls = env
    real = cv_builder.build
    last = tmp_path / "last_try.yaml"
    last.write_text("summary: x\n")

    def once(*a, **k):
        monkeypatch.setattr(cv_builder, "build", real)
        raise cv_builder.CVBuildError("out of rounds", last=last, problems=["1 line longer than one page"])
    monkeypatch.setattr(cv_builder, "build", once)
    bot.handle(s, cb("ap:abc123def456"), by="mac", mac_online=True)
    tasks.run_one(s)
    q = s.t("Queue").rows[0]
    assert q["Status"] == "pending" and "retry_at" not in q["Payload"] and "resume" in q["Payload"]
    assert not any("failed" in m for m in sent["msgs"])
    assert tasks.run_one(s) is True              # no wait
    assert calls[-1]["previous"] == last and calls[-1]["failed"] == ["1 line longer than one page"]
    assert len(sent["docs"]) == 1 and s.t("Queue").rows[0]["Status"] == "done"


def test_resume_sends_already_built_pdf_without_rebuilding(env, monkeypatch, tmp_path):
    import json as _json
    from datetime import datetime, timedelta, timezone
    s, sent, calls = env
    monkeypatch.setattr(config, "cv_root", lambda: tmp_path / "CVs")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    bot.handle(s, cb("ap:abc123def456"), by="mac", mac_online=True)
    task = s.t("Queue").rows[0]
    app = s.t("Applications").rows[0]
    out = cv_builder.job_dir({"company": "Acme", "title": "Data Engineer"}, date=app["Date approved"])
    out.mkdir(parents=True)
    (out / config.cv_file_name()).write_bytes(b"%PDF")
    work = tmp_path / "state" / "work" / out.name
    work.mkdir(parents=True)
    built = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds")
    (work / "notes.json").write_text(_json.dumps({"fit": "f", "gaps": [], "built": built}))
    task.update({"Status": "in_progress", "Attempts": "1"})        # the worker died mid-task
    tasks.reset_stale(s, startup=True)
    tasks.run_one(s)
    assert calls == []                                              # no rebuild
    assert len(sent["docs"]) == 1 and s.t("Queue").rows[0]["Status"] == "done"


def test_find_due_day_and_night():
    from datetime import datetime, timedelta
    from agent import run
    from agent.sheet import TZ

    class S:
        def __init__(self, last): self.last = last
        def kv_get(self, tab, key, default=""): return self.last if key == "last_find_at" else default

    day = datetime(2026, 10, 23, 10, 0, tzinfo=TZ)
    night = datetime(2026, 10, 23, 3, 0, tzinfo=TZ)
    assert run.find_due(S(""), day)
    assert not run.find_due(S((day - timedelta(minutes=5)).isoformat(sep=" ")), day)
    assert run.find_due(S((day - timedelta(minutes=15)).isoformat(sep=" ")), day)
    assert not run.find_due(S((night - timedelta(minutes=60)).isoformat(sep=" ")), night)
    assert run.find_due(S((night - timedelta(minutes=120)).isoformat(sep=" ")), night)


def test_cv_caption_keeps_the_save_location():
    from agent import config
    from agent import telegram as tg
    where = config.where_saved("CVs/ML Engineer CV/Acme - Engineer - 2026-10-23/Alex Morgan CV.pdf")
    assert "<b>Alex Morgan CV.pdf</b>" in where
    assert f"<code>{config.ROOT}/CVs/ML Engineer CV/Acme - Engineer - 2026-10-23/Alex Morgan CV.pdf</code>" in where
    import platform
    try:
        top = config.ROOT.relative_to(Path.home()).parts[0]
    except ValueError:
        top = ""
    in_icloud = platform.system() == "Darwin" and top in ("Desktop", "Documents")
    assert ("iCloud Drive" in where) == in_icloud
    if in_icloud:
        assert f"{config.ROOT.name} › CVs › ML Engineer CV › Acme - Engineer - 2026-10-23" in where
    # same arithmetic as tasks.build_cv: a very long fit text never pushes the location out
    head, tail = "📄 head\n", f'\n🔗 link\n{where}'
    room = max(120, 1024 - len(head) - len(tail) - 30)
    fit, gap = ("x" * 2000)[:room * 55 // 100], ("y" * 2000)[:room * 45 // 100]
    caption = f"{head}✅ {fit}\n⚠️ Gaps: {gap}{tail}"
    assert len(caption) <= 1024 and caption.endswith(where)
