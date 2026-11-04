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
    monkeypatch.setattr(scout, "_page_boards", lambda careers: [])
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


def test_scout_reads_the_board_a_careers_page_links_to(sheet, monkeypatch):
    html = ('<a href="https://www.comeet.com/jobs/smart-shooter/0B.00A">Jobs</a>'
            '<iframe src="https://paloaltonetworks.wd5.myworkdayjobs.com/en-US/panwexternalcareers"></iframe>')

    from agent.sources import detect

    class R:
        text = html
    monkeypatch.setattr(detect.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(detect, "allowed", lambda url: True)
    assert scout._page_boards("https://x.co/careers") == [("comeet", "smart-shooter/0B.00A"),
                                                          ("workday", "paloaltonetworks.wd5/paloaltonetworks/panwexternalcareers")]
    local = [Job("workday", "workday:b:1", "Initech", "Software Engineer", "London, United Kingdom", "u")]
    monkeypatch.setattr(scout, "_fetchers", lambda: {"workday": lambda c: local, "comeet": lambda c: [],
                                                     "greenhouse": lambda c: [], "ashby": lambda c: [], "lever": lambda c: []})
    sheet.t("Boards").append({"Company": "Initech", "Status": "suggested", "Careers": "https://x.co/careers",
                              "Checked": "2026-10-01 10:00:00+03:00"})
    sheet.t("Boards").append({"Company": "Fresh Co", "Status": "suggested", "Careers": "https://y.co/careers",
                              "Checked": scout.now_iso()})              # checked today: not again yet
    assert scout.recheck_pages(sheet) == 1
    rows = {r["Company"]: r for r in sheet.t("Boards").rows}
    assert (rows["Initech"]["Status"], rows["Initech"]["ATS"]) == ("found", "workday")
    assert rows["Fresh Co"]["Status"] == "suggested"


def test_a_comeet_widget_on_the_company_site_gives_its_board(monkeypatch):
    from agent.sources import detect
    page = 'var comeetvar = {"comeet_token":"45715B315B38AE22B8D051A0A457D051E61","comeet_uid":"54.007"};'

    class R:
        ok = True
        text = page

        @staticmethod
        def json():
            return [{"url_comeet_hosted_page": "https://www.comeet.com/jobs/silverfort/54.007/full-stack/84.F60"}]
    monkeypatch.setattr(detect.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(detect, "allowed", lambda url: True)
    monkeypatch.setattr(detect, "render", lambda url: "")
    assert detect.boards_on_page("https://www.silverfort.com/careers/") == [("comeet", "silverfort/54.007")]


def test_the_page_is_opened_in_a_browser_only_when_the_plain_page_shows_no_board(monkeypatch):
    from agent.sources import detect

    class R:
        text = "<div id=app></div>"
    monkeypatch.setattr(detect.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(detect, "allowed", lambda url: True)
    monkeypatch.setattr(detect, "render", lambda url: '<a href="https://apply.workable.com/globex/">Jobs</a>')
    assert detect.boards_on_page("https://globex.example/careers") == [("workable", "globex")]
    assert detect.board_links('<a href="https://jobs.smartrecruiters.com/Acme2/744">x</a>') == [("smartrecruiters", "Acme2")]
    assert detect.board_links('<a href="https://jobs.eu.lever.co/hooli/e3">x</a>') == [("lever", "eu/hooli")]
    assert detect.board_links('src="https://www.comeet.co/jobs/A5.007/DA.810" href="https://www.comeet.com/jobs/umbrella/A5.007"') \
        == [("comeet", "umbrella/A5.007")]


def test_new_readers_parse_local_jobs(monkeypatch):
    from agent.sources import ats

    class R:
        status_code, ok = 200, True

        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data
    wk = {"jobs": [{"title": "Backend Engineer", "shortcode": "AB12", "url": "https://apply.workable.com/j/AB12",
                    "published_on": "2026-11-01", "description": "<p>Python</p>",
                    "locations": [{"country": "United Kingdom", "countryCode": "GB", "city": "London"}]}]}
    monkeypatch.setattr(ats.requests, "get", lambda *a, **k: R(wk))
    (j,) = ats.workable({"name": "Globex", "board": "globex"})
    assert (j.country, j.source_ref, j.description) == ("GB", "workable:globex:AB12", "Python")
    rc = {"offers": [{"id": 7, "title": "Data Engineer", "country_code": "GB", "location": "London, United Kingdom",
                      "careers_url": "https://vandelay.recruitee.com/o/data", "description": "<p>SQL</p>", "requirements": ""}]}
    monkeypatch.setattr(ats.requests, "get", lambda *a, **k: R(rc))
    (j,) = ats.recruitee({"name": "Vandelay", "board": "vandelay"})
    assert (j.country, j.source_ref, j.url) == ("GB", "recruitee:vandelay:7", "https://vandelay.recruitee.com/o/data")
    sr_list = {"totalFound": 1, "content": [{"id": "99", "name": "ML Engineer", "releasedDate": "2026-11-03T12:00:00Z",
                                             "location": {"city": "London", "country": "gb", "fullLocation": "London, United Kingdom"}}]}
    sr_ad = {"postingUrl": "https://jobs.smartrecruiters.com/Acme2/99",
             "jobAd": {"sections": {"jobDescription": {"title": "The job", "text": "<p>LLMs</p>"}}}}
    monkeypatch.setattr(ats.requests, "get", lambda url, *a, **k: R(sr_ad if url.endswith("/99") else sr_list))
    (j,) = ats.smartrecruiters({"name": "Acme", "board": "Acme2"})
    assert (j.country, j.url) == ("GB", "https://jobs.smartrecruiters.com/Acme2/99") and "LLMs" in j.description


def test_a_careers_page_whose_robots_txt_says_no_is_never_read(monkeypatch):
    from agent.sources import detect

    class R:
        status_code, text = 200, "User-agent: *\nDisallow: /careers"
    asked = []
    monkeypatch.setattr(detect.requests, "get", lambda url, **k: asked.append(url) or R())
    monkeypatch.setattr(detect, "render", lambda url: (_ for _ in ()).throw(AssertionError("opened")))
    assert detect.boards_on_page("https://acme.example/careers") == []
    assert asked == ["https://acme.example/robots.txt"]
