"""Adding a company from its careers page link or its name (no network: boards are faked)."""
from agent import bot, company_add
from agent.jobs import Job
from tests.test_bot_idempotency import FakeSheet, sent  # noqa: F401
from tests.test_open_and_replies import msg


def test_board_links_are_read_from_urls_and_pages():
    assert company_add.boards_in("https://job-boards.greenhouse.io/acme/jobs/123") == [("greenhouse", "acme")]
    assert company_add.boards_in('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme">') == \
        [("greenhouse", "acme")]
    assert company_add.boards_in("https://jobs.lever.co/acme-labs/1a2b") == [("lever", "acme-labs")]
    assert company_add.boards_in("https://jobs.ashbyhq.com/acme") == [("ashby", "acme")]
    assert company_add.boards_in("https://www.comeet.com/jobs/acme/A1.B2C/dev/X") == [("comeet", "acme/A1.B2C")]
    assert company_add.boards_in("https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers/job/x") == \
        [("workday", "acme.wd5/acme/AcmeCareers")]
    assert company_add.boards_in("https://acme.com/careers") == []


def fake_boards(monkeypatch, boards):
    """boards: {(ats, board): [locations]} the fake job boards answer with."""
    def fetch(kind):
        def f(c):
            if (kind, c["board"]) not in boards:
                raise ValueError("no board")
            return [Job(kind, "", c["name"], "Engineer", loc, f"https://x/{i}") for i, loc in
                    enumerate(boards[(kind, c["board"])])]
        return f
    monkeypatch.setattr(company_add.sources, "FETCHERS", {k: fetch(k) for k in
                                                          ("greenhouse", "lever", "ashby", "comeet", "workday")})


def test_detect_from_a_careers_page(monkeypatch):
    fake_boards(monkeypatch, {("greenhouse", "acme"): ["Salford, United Kingdom", "Berlin, Germany"]})
    monkeypatch.setattr(company_add, "_page", lambda url: '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">')
    found, why = company_add.detect("https://www.acme.co.uk/careers")
    assert (found["name"], found["ats"], found["board"], found["jobs"], found["local"]) == \
        ("Acme", "greenhouse", "acme", 2, 1) and why == ""


def test_detect_from_a_name(monkeypatch):
    fake_boards(monkeypatch, {("ashby", "novadata"): ["Remote, UK"]})
    found, _ = company_add.detect("Nova Data")
    assert (found["name"], found["ats"], found["board"]) == ("Nova Data", "ashby", "novadata")
    found, why = company_add.detect("Nobody Inc")
    assert found is None and "careers page link" in why


def test_linkedin_and_job_boards_are_never_opened(monkeypatch):
    monkeypatch.setattr(company_add, "_page", lambda url: (_ for _ in ()).throw(AssertionError("opened")))
    for url in ("https://www.linkedin.com/company/acme/jobs", "https://www.indeed.com/cmp/Acme/jobs"):
        found, why = company_add.detect(url)
        assert found is None and "don't open" in why


def test_company_command_adds_a_board_once(monkeypatch, sent):  # noqa: F811
    fake_boards(monkeypatch, {("lever", "acme"): ["Salford, UK"]})
    sheet = FakeSheet()
    bot.handle(sheet, msg(1, "/company https://jobs.lever.co/acme"), by="mac", mac_online=True)
    (row,) = sheet.t("Boards").rows
    assert (row["Company"], row["ATS"], row["Board"], row["Status"]) == ("Acme", "lever", "acme", "found")
    assert "Now watching <b>Acme</b>" in sent[-1] and "1 in United Kingdom" in sent[-1]
    bot.handle(sheet, msg(2, "/company https://jobs.lever.co/acme"), by="mac", mac_online=True)
    assert len(sheet.t("Boards").rows) == 1 and "already watch" in sent[-1]
    bot.handle(sheet, msg(3, "/company"), by="mac", mac_online=True)
    assert "careers page link" in sent[-1]
