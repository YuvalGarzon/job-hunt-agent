"""Filters, score and location, for the made-up Alex Morgan (junior, home in Manchester, GB, 40 km)."""
from datetime import datetime, timedelta, timezone

import pytest

from agent import config, filters, geo
from agent.jobs import Job, canonical_url, job_id

# place -> (lat, lon, country): the Geo tab's cache, so no test calls OpenStreetMap
CACHE = {"salford": (53.4875, -2.2901, "GB"), "stockport": (53.4106, -2.1575, "GB"),
         "leeds": (53.8008, -1.5491, "GB"), "london": (51.5072, -0.1276, "GB"), "gurugram": None,
         "springfield": (39.7817, -89.6501, "US")}


def mk(title="Software Engineer", desc="", **kw):
    return Job(source="greenhouse", source_ref="greenhouse:x:1", company="Acme", title=title,
               location=kw.pop("location", "Salford"), url=kw.pop("url", "https://boards.greenhouse.io/x/jobs/1"),
               description=desc, **kw)


def search(monkeypatch, **changes):
    """Run a test with other search settings."""
    s = dict(config.settings())
    s["search"] = {**s["search"], **changes}
    monkeypatch.setattr(config, "settings", lambda: s)


@pytest.mark.parametrize("title,keep", [
    ("Software Engineer", True), ("Data Scientist", True), ("Backend Developer", True),
    ("QA Automation Engineer", True), ("Algorithm Developer", True), ("Data Analyst", True),
    ("Account Executive", False), ("Talent Acquisition Partner", False), ("IT Support Engineer", False),
    ("Manual QA Tester", False), ("Hardware Engineer", False), ("Product Manager", False),
    ("Game Economist", False), ("GRC Security Specialist", False),
])
def test_title_filter(title, keep):
    assert (filters.title_check(title) == "") is keep


def test_exclude_words_from_settings(monkeypatch):
    assert filters.title_check("Unpaid Software Engineer").startswith("title has 'unpaid'")
    search(monkeypatch, exclude_title_words=["crypto"])
    assert filters.title_check("Crypto Backend Engineer")
    assert filters.title_check("Unpaid Software Engineer") == ""


@pytest.mark.parametrize("title", ["Senior Software Engineer", "Sr. Data Scientist", "Team Lead, Backend",
                                   "Staff ML Engineer", "Principal Engineer", "Engineering Manager"])
def test_senior_titles_dropped(title):
    assert filters.seniority(mk(title))["drop"]


def test_levels_move_the_senior_line(monkeypatch):
    search(monkeypatch, level="mid", max_years=6)
    assert not filters.seniority(mk("Senior Software Engineer", "- 5+ years of experience"))["drop"]
    assert filters.seniority(mk("Staff Engineer"))["drop"]
    assert filters.seniority(mk(desc="- 7+ years of experience"))["drop"] == "requires 7+ years"
    search(monkeypatch, level="senior", max_years=15)
    assert not filters.seniority(mk("Staff Engineer"))["drop"]
    assert filters.seniority(mk("Director of Engineering"))["drop"]


@pytest.mark.parametrize("desc,years", [
    ("Requirements:\n- 3+ years of experience in Python", 3),
    ("- At least 5 years of hands-on experience", 5),
    ("- 2-3 years of experience as a backend developer", 2),
    ("- B.Sc. in Computer Science\n- Experience with SQL", -1),
    ("- 4+ years of experience with Kubernetes - an advantage\n- 1+ years experience with Python", 1),
])
def test_required_years(desc, years):
    assert filters.required_years(desc) == years


def test_years_drop_and_stretch():
    assert filters.seniority(mk(desc="- 4+ years of experience"))["drop"] == "requires 4+ years"
    s = filters.seniority(mk(desc="- 2+ years of experience in Java"))
    assert not s["drop"] and s["stretch"]
    assert filters.seniority(mk("Junior Developer"))["graduate"]


def test_degree_requirement_never_drops():
    j = mk(desc="- B.Sc. in Computer Science, Computer Engineering or a related field")
    assert not filters.seniority(j)["drop"] and not filters.title_check(j.title)


@pytest.mark.parametrize("title", ["Software Engineering Intern", "Student Data Analyst",
                                   "Part-time Python Developer", "Contract Backend Developer"])
def test_not_fulltime(title):
    assert filters.employment_check(mk(title))


def test_job_types_from_settings(monkeypatch):
    search(monkeypatch, job_types=["full-time", "internship", "contract"])
    assert not filters.employment_check(mk("Software Engineering Intern"))
    assert not filters.employment_check(mk("Contract Backend Developer"))
    assert filters.employment_check(mk("Part-time Python Developer"))
    assert not filters.cheap_drop(mk("Software Engineering Intern"))


def test_remote_jobs_follow_the_setting(monkeypatch):
    loc = geo.locate("Remote", "", CACHE, allow_network=False)
    assert filters.remote_check(mk(location="Remote"), loc) == ""
    search(monkeypatch, include_remote=False)
    assert filters.remote_check(mk(location="Remote"), loc)
    assert filters.remote_check(mk(location="Salford (Hybrid)"), geo.locate("Salford (Hybrid)", "", CACHE,
                                                                            allow_network=False)) == ""


def test_recency():
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert filters.recency_check(mk(posted=old))
    assert not filters.recency_check(mk(posted=new))


def test_scam_signs():
    assert filters.scam_check(mk(desc="Please pay a registration fee of 200 GBP"))
    assert filters.scam_check(mk(desc="Send CV to jobs.recruiter@gmail.com"))
    assert not filters.scam_check(mk(desc="Apply through our careers site"))


@pytest.mark.parametrize("loc,ok", [("Salford", True), ("Stockport, United Kingdom", True),
                                    ("Salford, Greater Manchester, UK", True), ("Leeds", False), ("London, UK", False),
                                    ("Berlin, Germany", False), ("Austin, TX", False), ("Gurugram", False),
                                    ("Springfield", False), ("London | Salford", True)])
def test_location(loc, ok):
    r = geo.locate(loc, "", dict(CACHE), allow_network=False)
    assert (r["drop_reason"] == "") is ok, r


def test_far_and_abroad_have_their_reasons():
    assert geo.locate("Leeds", "", CACHE, allow_network=False)["drop_reason"].endswith("km away")
    assert geo.locate("Berlin, Germany", "", CACHE, allow_network=False)["drop_reason"] == \
        "outside United Kingdom (germany)"
    assert geo.locate("Somewhere", "DE", CACHE, allow_network=False)["drop_reason"].startswith("outside United Kingdom")
    assert geo.locate("Chicago, IL | United States", "", CACHE, allow_network=False)["drop_reason"].startswith("outside")


def test_unknown_location_kept_and_flagged():
    assert geo.locate("United Kingdom", "", {}, allow_network=False)["flag"] == "location?"
    assert geo.locate("UK - Home Based", "", {}, allow_network=False)["flag"] == "location?"
    assert geo.locate("Newtown", "", {}, allow_network=False)["flag"] == "defer"        # needs a lookup
    assert geo.locate("Newtown", "GB", {}, allow_network=False)["flag"] == "location?"


def test_states_and_countries_that_share_a_name(monkeypatch):
    from agent import country
    s = dict(config.settings())
    s["location"] = {**s["location"], "country": "US"}
    monkeypatch.setattr(config, "settings", lambda: s)
    country._pattern.cache_clear()
    assert country.abroad("Atlanta, Georgia") == "" and country.abroad("Jersey City, NJ") == ""
    assert country.abroad("Albuquerque, New Mexico") == "" and country.abroad("Toronto, Canada") == "canada"
    country._pattern.cache_clear()


def test_job_id_stable_across_tracking_params():
    a = job_id("Acme", "Data Engineer", "https://boards.greenhouse.io/acme/jobs/1?gh_src=abc&utm_source=li")
    b = job_id("ACME ", "Data  Engineer", "https://boards.greenhouse.io/acme/jobs/1")
    assert a == b
    assert canonical_url("https://www.x.com/jobs/1/?utm_campaign=z") == "https://x.com/jobs/1"


@pytest.mark.parametrize("title", ["Supplier Quality Engineer", "Physicist - Electro Optics Engineer",
                                   "Electrical, Electronics & Control Systems Practical Engineer",
                                   "NPI Optics Engineer", "IC Package Design Engineer", "Customer Engineer - Leeds",
                                   "Junior AI SoC Design Verification Engineer, Google Cloud",
                                   "CPU Design for Test Engineer, Google Cloud"])
def test_hardware_titles_dropped(title):
    assert filters.title_check(title) == "hardware role"


@pytest.mark.parametrize("title", ["Software Validation Engineer (QA Engineer)", "WiFi FW Engineer",
                                   "Firmware Engineer, SoC", "Embedded Software Engineer, Annapurna Labs",
                                   "Software Quality Engineer", "Electrical & Software Engineer"])
def test_software_word_outweighs_hardware_word(title):
    assert filters.title_check(title) == ""


def test_title_tiers_come_from_settings():
    from agent.score import score
    sen = {"drop": "", "stretch": False, "years": -1, "graduate": False}
    loc = {"km": 9}
    part = lambda t: score(mk(t), sen, loc, {})["parts"]["title"]   # noqa: E731
    assert part("Machine Learning Engineer") == 30 and part("Backend Engineer") == 24
    assert part("Offensive Security Researcher") == 14 and part("R&D System Engineer") == 4


def test_specialty_and_favorites_raise_the_score():
    from agent import sources
    from agent.score import score
    sen = {"drop": "", "stretch": False, "years": -1, "graduate": False}
    plain = score(mk("Backend Engineer", "Build APIs."), sen, {"km": 30}, {})
    ml = score(mk("Backend Engineer", "Serve machine learning models with PyTorch; deep learning; statistics."),
               sen, {"km": 30}, {})
    assert ml["parts"]["specialty"] > plain["parts"]["specialty"]
    assert sources.company_tiers()["spotify"] == 1                     # a favorite company in the settings
    assert score(mk("Backend Engineer"), sen, {"km": 5}, {})["parts"]["location"] == 10


@pytest.mark.parametrize("title", ["Experienced Software Engineer", "Software Engineer III, Networking",
                                   "Mid- Level Backend Engineer"])
def test_experience_titles_are_a_stretch(title):
    s = filters.seniority(mk(title))
    assert not s["drop"] and s["stretch"]


def test_timestamp_links_are_one_job():
    a = Job("comeet", "", "Playforge", "Business Data Analyst", "", "https://www.playforge.co/careers-position/C2.37D/?t=1791348049854")
    b = Job("comeet", "", "Playforge", "Business Data Analyst", "", "https://www.playforge.co/careers-position/C2.37D/")
    assert a.job_id == b.job_id and "?" not in a.url


def test_years_of_doing_something_count_without_the_word_experience():
    from agent.filters import required_years
    assert required_years("What we're looking for\n3+ years building and shipping machine learning models") == 3
    assert required_years("at least 2 years in backend development") == 2
    assert required_years("Founded 10 years ago, we grew fast") == -1
    assert required_years("Nice to have: 5+ years in ad tech") == -1


def test_card_shows_one_location_pin_and_who_sent_it():
    from agent import alerts
    row = {"Title": "Data Scientist", "Company": "Acme", "City": "", "Location": "", "Location flag": "location?",
           "Score": 66, "Source": "telegram", "URL": "https://acme.ai/careers/data-scientist", "Stretch": "yes"}
    text = alerts.job_text(row, 1)
    assert text.count("📍") == 1 and "location?" in text and "you sent it from the company site" in text
    row["URL"] = "https://www.linkedin.com/jobs/view/1/"
    assert "not verified on the company site" in alerts.job_text(row, 1)


def test_remote_jobs_show_remote_on_the_card():
    from agent import alerts
    row = {"Title": "Software Engineer", "Company": "Acme", "City": "", "Location": "Remote - UK",
           "Location flag": "location?", "Score": 66, "Source": "greenhouse", "URL": "https://x"}
    assert "📍 Remote" in alerts.job_text(row, 1)
