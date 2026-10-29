from agent import pipeline
from agent.jobs import Job
from agent.sources import linkedin

EMAIL = """Your job alert for software engineer in United Kingdom
New jobs match your preferences.

Image Processing SW Enginner
Applied Materials - UK
Stockport

36 company alumni
View job: https://www.linkedin.com/comm/jobs/view/4472614620/?trackingId=abc&otpToken=SECRET123

---------------------------------------------------------

Full Stack Developer- Insignito
Sentrycs | Counter-Drone Solutions Adapting at the Speed of Threats
Manchester

This company is actively hiring
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/4482102101/?trackingId=def&otpToken=SECRET456
  \r
 --------------------------------------------------------- \r
See all jobs: https://www.linkedin.com/comm/jobs/search/?otpToken=SECRET789
"""


def test_parse_cards():
    cards = linkedin.parse(EMAIL)
    assert [(c.title, c.company, c.location, c.li_id) for c in cards] == [
        ("Image Processing SW Enginner", "Applied Materials - UK", "Stockport", "4472614620"),
        ("Full Stack Developer- Insignito", "Sentrycs | Counter-Drone Solutions Adapting at the Speed of Threats",
         "Manchester", "4482102101")]


def test_stored_link_has_no_token():
    for c in linkedin.parse(EMAIL):
        assert c.url == f"https://www.linkedin.com/jobs/view/{c.li_id}/"
        assert "otp" not in c.url.lower() and "?" not in c.url


def test_company_names():
    assert linkedin.short_company("Applied Materials - UK") == "Applied Materials"
    assert linkedin.short_company("Sentrycs | Counter-Drone Solutions") == "Sentrycs"
    assert linkedin.short_company("Solid (AI Powered by Data)") == "Solid"
    assert linkedin.company_key("AppCard, Inc.") == linkedin.company_key("AppCard")
    assert linkedin.company_key("Acme Systems Ltd") == linkedin.company_key("Acme Systems")


def test_slugs():
    assert linkedin._slugs("At-Bay")[:2] == ["atbay", "at-bay"]
    assert linkedin._slugs("Orca AI")[:2] == ["orcaai", "orca-ai"]
    assert linkedin._slugs("Ltd") == []


def mk(company, title):
    return Job(source="greenhouse", source_ref="greenhouse:x:1", company=company, title=title, location="Manchester",
               url="https://boards.greenhouse.io/x/jobs/1")


def test_find_on_known_board():
    alert = linkedin.parse(EMAIL)[0]
    jobs = [mk("Applied Materials", "Algorithm Developer"), mk("Applied Materials", "Image Processing SW Enginner")]
    assert linkedin.find_in(alert, jobs) is jobs[1]
    assert linkedin.find_in(alert, [mk("KLA", "Image Processing SW Enginner")]) is None


def test_linkedin_only_never_fetched(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LinkedIn must never be fetched")
    monkeypatch.setattr(pipeline.requests, "get", boom)
    job = Job(source="linkedin", source_ref="", company="QTREX", title="Algorithm Developer", location="Ness Ziona",
              url="https://www.linkedin.com/jobs/view/4480784386/", extra={"unverified": True})
    assert pipeline.verify_live(job) == ""                       # alerted, tagged
    assert job.extra["verify_note"].startswith("LinkedIn only")


def test_linkedin_only_alert_is_tagged():
    from agent import alerts
    row = {"Title": "Algorithm Developer", "Company": "QTREX", "City": "Ness Ziona", "Location": "", "Location flag": "",
           "Score": "67", "Fit": "", "Gaps": "", "Stretch": "", "Source": "linkedin",
           "URL": "https://www.linkedin.com/jobs/view/4480784386/"}
    assert "LinkedIn only" in alerts.job_text(row, 1)
