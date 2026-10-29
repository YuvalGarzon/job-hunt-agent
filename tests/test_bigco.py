import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import filters, geo, sources
from agent.jobs import Job
from agent.sources import bigco


def mk(title="Software Engineer", posted="", **kw):
    return Job(source="workday", source_ref="workday:x.wd1/x/Ext:/job/United-Kingdom-Salford/A_1", company="Acme",
               title=title, location="United Kingdom Salford", url="https://x.wd1.myworkdayjobs.com/Ext/job/A_1",
               posted=posted, country="GB", extra={"detail": True}, **kw)


def test_workday_country_facet_wins():
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationHierarchy1", "values": [{"descriptor": "United Kingdom", "id": "GB1", "count": 5},
                                                            {"descriptor": "India", "id": "IN1", "count": 9}]},
        {"facetParameter": "locations", "values": [{"descriptor": "UK, Salford", "id": "SA", "count": 3}]}]}]
    assert bigco._wd_country(facets) == {"locationHierarchy1": ["GB1"]}


def test_workday_places_when_no_country():
    facets = [{"facetParameter": "locations", "values": [
        {"descriptor": "Salford, United Kingdom", "id": "a", "count": 2}, {"descriptor": "Leeds, UK", "id": "b", "count": 1},
        {"descriptor": "Bangalore, India", "id": "c", "count": 7}]},
        {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "UK Labs", "id": "x", "count": 1}]}]
    assert bigco._wd_country(facets) == {"locations": ["a", "b"]}


def test_workday_no_local_jobs():
    assert bigco._wd_country([{"facetParameter": "locations", "values": [{"descriptor": "Austin, TX", "id": "a"}]}]) == {}


@pytest.mark.parametrize("text,days", [("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3),
                                       ("Posted 30+ Days Ago", 31)])
def test_workday_posted(text, days):
    got = datetime.fromisoformat(bigco._wd_posted(text))
    assert abs((datetime.now(timezone.utc) - got) - timedelta(days=days)) < timedelta(minutes=1)


def test_workday_old_posting_dropped_before_detail():
    assert filters.cheap_drop(mk(posted=bigco._wd_posted("Posted 30+ Days Ago"))).startswith("posted")


def test_workday_location_from_path():
    p = {"locationsText": "5 Locations", "externalPath": "/job/United-Kingdom-Salford/DevOps-Engineer_JR1"}
    assert bigco._wd_location(p) == "United Kingdom Salford"
    cache = {"salford": (53.4875, -2.2901, "GB")}
    assert geo.locate(bigco._wd_location(p), "", cache, allow_network=False)["city"] == "Salford"
    assert bigco._wd_location({"locationsText": "UK, Leeds", "externalPath": "/job/x/y"}) == "UK, Leeds"


@pytest.mark.parametrize("title,drop", [("Software Engineer", False), ("Senior Software Engineer", True),
                                        ("Student Software Engineer", True), ("Account Executive", True)])
def test_cheap_drop(title, drop):
    assert bool(filters.cheap_drop(mk(title))) is drop


def test_cheap_drop_never_stricter_than_full_filters():
    # A job cheap_drop keeps must still be judged by the full filters after its description arrives
    job = mk("Software Engineer", posted=datetime.now(timezone.utc).isoformat())
    assert filters.cheap_drop(job) == ""
    job.description = "Requirements: 6+ years of experience in backend development"
    assert filters.seniority(job)["drop"]


def test_amazon_posted():
    assert bigco._amz_posted("October 22, 2026").startswith("2026-10-22")
    assert bigco._amz_posted("soon") == ""


def test_google_parse_keeps_only_local_locations():
    row = ["123", "Software Engineer III, Networking", "signin", [None, "<li>Build things</li>"],
           [None, "<li>BS in CS</li>"], "tenant", None, "Google", "en-US",
           [["New York, NY, USA", [], "New York", None, "NY", "US"], ["Manchester, UK", [], "Manchester", None, None, "GB"]],
           [None, "<p>About</p>"], [2], [1792607999, 0]]
    html = ("<script>AF_initDataCallback({key: 'ds:1', hash: '2', data:" + json.dumps([[row], None, 1, 20]) +
            ", sideChannel: {}});</script>")
    j = bigco._g_job({"name": "Google", "tier": 1}, bigco._g_data(html, "ds:1")[0][0], "software-engineer-iii")
    assert j.location == "Manchester, UK" and j.country == "GB"
    assert j.url.endswith("/123-software-engineer-iii")
    assert "BS in CS" in j.description and j.posted.startswith("2026-")


def test_google_error_block_is_parsed():
    html = "<script>AF_initDataCallback({key: 'ds:0',  data:[5,null,[[\"x\",[\"cp.v1.e\",30]]]],errorHasStatus: true,});</script>"
    assert bigco._g_data(html, "ds:0")[0] == 5


def test_fill_details_caps_and_marks(monkeypatch):
    calls = []

    def fake(job):
        calls.append(job.title)
        job.description = "filled"
        return job.title != "gone"

    monkeypatch.setitem(bigco.DETAIL, "workday", fake)
    monkeypatch.setattr(bigco, "DETAIL_CAP_DEFAULT", 2)
    jobs = [mk("Software Engineer A"), mk("gone"), mk("Software Engineer C")]
    done = sources.fill_details(jobs)
    assert len(calls) == 2                                  # cap per source
    assert done == {jobs[0].job_id}                         # gone posting and capped one wait


def test_every_company_has_a_fetcher():
    for c in sources.companies():
        assert c["ats"] in sources.FETCHERS, c["name"]


def test_sources_ask_for_the_users_country():
    assert [q["location"] for q in bigco.g_queries()] == ["United Kingdom", "United Kingdom"]
    assert bigco._a_slug() == "united-kingdom-GBR" and bigco.a_base("en-gb").endswith("/en-gb")
    assert bigco._ms_country(["Manchester, ENG, GB"]) == "GB" and bigco._ms_country(["Berlin, BE, DE"]) == "DE"
