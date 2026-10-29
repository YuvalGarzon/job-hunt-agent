import json
from datetime import datetime, timedelta

from agent import bot, learn, scout, tracking
from agent.jobs import Job
from agent.sheet import TZ
from tests.test_bot_idempotency import cb, sheet, sent  # noqa: F401  (fixtures)


def test_click_trackers_are_removed_from_links():
    j = Job("telegram", "", "Playforge", "Dev", "", "https://playforge.co/careers/?fbclid=IwZX&gh_jid=123&utm_source=fb")
    assert j.url == "https://playforge.co/careers/?gh_jid=123"
    assert Job("x", "", "A", "B", "", "https://a.com/jobs/1").url == "https://a.com/jobs/1"


def test_skip_reason_company_blocks_and_undo_unblocks(sheet, sent):
    bot.handle(sheet, cb("sk:abc123def456"), by="mac", mac_online=True)
    bot.handle(sheet, cb("skr:co:abc123def456", 2), by="mac", mac_online=True)
    job = sheet.t("Jobs").rows[0]
    assert job["Skip reason"] == "co" and "Acme" in sent[-1]
    assert learn.Memory(sheet).drop("Acme Ltd")                       # no more jobs from Acme
    bot.handle(sheet, cb("unsk:abc123def456", 3), by="mac", mac_online=True)
    assert not learn.Memory(sheet).drop("Acme") and job["Skip reason"] == ""


def test_skip_reasons_lower_similar_jobs(sheet, sent):
    bot.handle(sheet, cb("sk:abc123def456"), by="mac", mac_online=True)
    bot.handle(sheet, cb("skr:loc:abc123def456", 2), by="mac", mac_online=True)
    sheet.t("Jobs").append({"Job ID": "j2", "Company": "Beta", "Title": "QA Automation Engineer", "City": "Leeds",
                            "User action": "skipped", "Skip reason": "field",
                            "Updated": datetime.now(TZ).isoformat()})
    mem = learn.Memory(sheet)
    row = {"Title": "Data Engineer", "City": "Manchester", "Score": 70, "Score parts": "title=30"}
    mem.apply(row)
    assert row["Score"] == 70 - learn.CITY_PENALTY and "skipped_city" in row["Score parts"]
    row = {"Title": "QA Automation Engineer II", "City": "Ramat Gan", "Score": 60, "Score parts": ""}
    mem.apply(row)
    assert row["Score"] == 60 - learn.TITLE_PENALTY
    row = {"Title": "Machine Learning Engineer", "City": "Ramat Gan", "Score": 60, "Score parts": ""}
    mem.apply(row)
    assert row["Score"] == 60


def test_morning_jobs_once_a_day(sheet, sent, monkeypatch):
    monkeypatch.setattr(tracking, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime.now(TZ).replace(hour=9))}))
    sheet.t("Jobs").rows[0]["First seen"] = datetime.now(TZ).isoformat()
    assert tracking.morning_jobs(sheet) is False                 # only today's cards: just seen, not sent again
    assert not any("Good morning" in m for m in sent)
    sheet.kv_set("Health", "morning_date", "")
    sheet.t("Jobs").rows[0]["First seen"] = (datetime.now(TZ) - timedelta(days=1)).isoformat()
    assert tracking.morning_jobs(sheet) is True
    assert any("Good morning" in m for m in sent)
    assert tracking.morning_jobs(sheet) is False


def test_scout_watches_boards_and_suggests(sheet, sent, monkeypatch):
    monkeypatch.setattr(scout.sources, "companies", lambda: [{"name": "Northwind"}])
    monkeypatch.setattr(scout, "ask_claude", lambda names, n=12: [
        {"name": "Northwind", "about": "x"},                                           # already watched
        {"name": "Nova Data", "about": "data tools", "why": "Data Engineer", "careers": "https://nova.io/careers",
         "ats": "greenhouse", "board": "novadata"},
        {"name": "Quiet Co", "about": "security", "careers": "https://quiet.co/jobs", "ats": "other"}])

    def gh(c):
        if c["board"] != "novadata":
            raise ValueError("no board")
        return [Job("greenhouse", "greenhouse:novadata:1", "Nova Data", "Junior Data Engineer", "Manchester, United Kingdom",
                    "https://x/1"), Job("greenhouse", "greenhouse:novadata:2", "Nova Data", "Engineer", "Berlin", "u")]
    monkeypatch.setattr(scout.ats, "FETCHERS", {"greenhouse": gh, "ashby": gh, "lever": gh})
    assert scout.run_task(sheet, {}, {}, print) == "2 new companies"
    rows = {r["Company"]: r for r in sheet.t("Boards").rows}
    assert (rows["Nova Data"]["Status"], rows["Nova Data"]["Board"]) == ("found", "novadata")
    assert rows["Quiet Co"]["Status"] == "suggested" and "Northwind" not in rows
    assert "1 open job in United Kingdom, 1 passes" in sent[-2] and "no public job board" in sent[-1]

    key = scout._cb("novadata")
    bot.handle(sheet, cb(f"cocv:{key}", 7), by="mac", mac_online=True)
    (t,) = [t for t in sheet.t("Queue").rows if t["Type"] == "intake"]
    p = json.loads(t["Payload"])
    assert p["approve"] and "general application" in p["text"] and "Nova Data" in p["text"]
    bot.handle(sheet, cb(f"cono:{key}", 8), by="mac", mac_online=True)
    assert rows["Nova Data"]["Status"] == "blocked" and learn.Memory(sheet).drop("Nova Data")


def test_the_same_job_is_not_sent_twice(sheet):
    from agent import alerts
    sent = alerts.Sent(sheet)                                   # the fixture's Acme "Data Engineer" was sent
    assert sent.before("Acme Ltd", "Data Engineer").startswith("already sent")
    assert sent.before("Acme", "ML Engineer") == "" and sent.before("Other", "Data Engineer") == ""
    sent.add("Nova", "Backend Engineer", "2026-10-25")
    assert sent.before("Nova", "Backend Engineer")
    assert alerts.Sent(sheet).before("?", "Data Engineer") == ""        # hidden company: never matched


def test_two_postings_with_their_own_ids_are_both_sent(sheet):
    from agent import alerts
    sent = alerts.Sent(sheet)
    sent.add("NVIDIA", "Firmware Engineer", "2026-10-24", "workday:nvidia:JR1")
    assert sent.before("NVIDIA", "Firmware Engineer", "workday:nvidia:JR2") == ""
    assert sent.before("NVIDIA", "Firmware Engineer", "workday:nvidia:JR1")
    assert sent.before("NVIDIA", "Firmware Engineer", "")                 # LinkedIn copy of it
