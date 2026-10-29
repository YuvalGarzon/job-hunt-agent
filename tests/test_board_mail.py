"""Job-board alert emails (the example settings list Indeed), read by Claude, never fetched."""
from agent import alerts, pipeline, tasks
from agent.jobs import Job
from agent.sources import board_mail

# A made-up alert email: two jobs, links with tracking tokens
EMAIL = """<html><body><style>a{color:red}</style>
<p>Junior Data Engineer</p><p>Acme Data · Salford</p><p>Python and SQL data pipelines. 0-2 years.</p>
<a href="https://www.indeed.com/rc/clk?jk=111&tk=SECRET">View job</a>
<p>QA Tester</p><p>Confidential · Stockport</p><p>Manual testing.</p>
<a href="https://www.indeed.com/rc/clk?jk=222&tk=SECRET">View job</a>
</body></html>"""
CLAUDE = ('{"jobs": [{"title": "Junior Data Engineer", "company": "Acme Data", "location": "Salford", '
          '"employment": "Full-time", "link": "L1", "ref": "111", "description": "Python and SQL data pipelines."}, '
          '{"title": "QA Tester - 23510", "company": "Confidential", "location": "Stockport", "employment": "full time", '
          '"link": "[L2]", "ref": "222", "description": "Manual testing."}]}')


def test_boards_come_from_the_settings():
    assert board_mail.boards() == {"indeed": ("alert@indeed.com", "Indeed")}
    assert board_mail.senders() == ["alert@indeed.com"]


def test_clean_link_and_title():
    assert board_mail.clean_link("https://www.indeed.com/viewjob?jk=9&t=TOKEN") == "https://www.indeed.com/viewjob"
    assert board_mail.clean_link("mailto:x@y") == ""
    assert board_mail.clean_title("22761 - SAP QA Tester") == "SAP QA Tester"
    assert board_mail.clean_title("QA Tester - 23510") == "QA Tester"


def test_emails_wait_for_the_mac_in_the_cloud(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert board_mail.parse("indeed", {"text": "jobs", "html": "<p>jobs</p>"}) is None


def test_claude_reads_the_email_and_tokens_never_reach_it(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    seen = {}

    def ask(prompt, **k):
        seen["prompt"] = prompt
        return CLAUDE
    monkeypatch.setattr("agent.claude_cli.ask", ask)
    a, b = board_mail.parse("indeed", {"text": "", "html": EMAIL})
    assert (a.title, a.company, a.location, a.employment, a.ref) == \
        ("Junior Data Engineer", "Acme Data", "Salford", "Full-time", "111")
    assert a.link == "https://www.indeed.com/rc/clk" and (b.title, b.company) == ("QA Tester", "")
    assert "SECRET" not in seen["prompt"] and "color" not in seen["prompt"]
    text, links = board_mail.marked(EMAIL)
    assert text.index("[L1]") < text.index("QA Tester")


class FakeSheet:
    def __init__(self):
        self.kv = {}

    def kv_get(self, tab, key, default=""):
        return self.kv.get(key, default)

    def kv_set(self, tab, key, value, flush=True):
        self.kv[key] = value

    def t(self, name):
        class T:
            rows = []

            def append(self, r):
                pass

            def update(self, *a, **k):
                pass
        return T()


def test_collect(monkeypatch):
    import time
    from agent import gmail
    now_ms = int(time.time()) * 1000
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(gmail, "search", lambda q, limit=100: ["m1"] if "indeed" in q else [])
    monkeypatch.setattr(gmail, "message", lambda mid: {"id": mid, "received_ms": now_ms, "text": "", "html": EMAIL,
                                                        "subject": "New jobs for you"})
    monkeypatch.setattr("agent.claude_cli.ask", lambda prompt, **k: CLAUDE)
    monkeypatch.setattr(board_mail, "save_post", lambda j: None)
    monkeypatch.setattr(board_mail.linkedin, "discover", lambda c, t: ({}, []))
    on_board = Job(source="greenhouse", source_ref="greenhouse:acme:1", company="Acme Data",
                   title="Junior Data Engineer", location="Salford", url="https://boards.greenhouse.io/acme/jobs/1")
    sheet = FakeSheet()
    out = board_mail.collect(sheet, [on_board], ["Acme Data"], log=lambda *a: None)
    assert on_board.extra["via_board"] == "indeed"                         # the company's own posting is used
    (j,) = out                                                              # the hidden-company job: unverified
    assert (j.source, j.company, j.title, j.employment) == ("indeed", "?", "QA Tester", "Full-time")
    assert j.extra["unverified"] and j.source_ref == "mail:indeed:m1:222"
    assert sheet.kv["indeed_after"] == str(now_ms // 1000)
    assert board_mail.collect(sheet, [], [], log=lambda *a: None) == []    # the cursor's own email is not read again


def test_account_emails_never_reach_claude(monkeypatch):
    from agent import gmail
    monkeypatch.setattr(gmail, "search", lambda q, limit=100: ["m9"] if "indeed" in q else [])
    monkeypatch.setattr(gmail, "message", lambda mid: {"id": mid, "received_ms": 2_000_000_000_000, "html": "",
                                                        "text": "x", "subject": "Your password reset code"})
    monkeypatch.setattr(board_mail, "parse_with_claude", lambda *a: (_ for _ in ()).throw(AssertionError("sent")))
    sheet = FakeSheet()
    assert board_mail.collect(sheet, [], [], log=lambda *a: None) == []
    assert sheet.kv["indeed_after"] == "2000000000"


def test_board_job_is_tagged_and_never_fetched(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("job-board pages must never be fetched")
    monkeypatch.setattr(pipeline.requests, "get", boom)
    job = Job(source="indeed", source_ref="mail:indeed:m1:222", company="?", title="QA Tester", location="Stockport",
              url="https://www.indeed.com/rc/clk", extra={"unverified": True})
    assert pipeline.verify_live(job) == "" and job.extra["verify_note"].startswith("indeed alert email only")
    row = {"Title": "QA Tester", "Company": "?", "City": "", "Location": "", "Location flag": "", "Score": "60",
           "Fit": "", "Gaps": "", "Stretch": "", "Source": "indeed", "URL": job.url}
    assert "from an Indeed alert" in alerts.job_text(row, 1)


def test_cv_uses_the_email_text(monkeypatch, tmp_path):
    from agent import gmail
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(board_mail, "POSTS_DIR", tmp_path)
    monkeypatch.setattr(gmail, "message", lambda mid: {"id": mid, "received_ms": 0, "text": "", "html": EMAIL})
    monkeypatch.setattr("agent.claude_cli.ask", lambda prompt, **k: CLAUDE)

    class S:
        def t(self, name):
            class T:
                def find(self, k, v):
                    return {"Job ID": "J1", "Source": "indeed", "Source ref": "mail:indeed:m1:111",
                            "Company": "Acme Data", "Title": "Junior Data Engineer", "Location": "Salford",
                            "URL": "https://www.indeed.com/rc/clk"}
            return T()
    job = tasks._job_from_sheet(S(), "J1")                                  # read again from Gmail
    assert "data pipelines" in job["description"]
    (tmp_path / "J1.txt").write_text("saved text", encoding="utf-8")
    assert tasks._job_from_sheet(S(), "J1")["description"] == "saved text"   # saved copy first
