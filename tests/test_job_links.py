"""A job's link opens that job itself, not the company's whole job list; its ID never changes."""
from agent.jobs import job_id
from agent.sources import ats


class R:
    def __init__(self, data=None, text=""):
        self._data, self.text, self.status_code, self.ok = data, text, 200, True

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def test_greenhouse_on_a_company_site_links_to_greenhouses_job_page(monkeypatch):
    site = "https://www.acme.example/careers/job/?gh_jid=8591838002"
    jobs = {"jobs": [{"id": 8591838002, "title": "Product Manager", "absolute_url": site,
                      "location": {"name": "Tel Aviv"}},
                     {"id": 7, "title": "Data Engineer", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7",
                      "location": {"name": "Tel Aviv"}}]}
    monkeypatch.setattr(ats.requests, "get", lambda *a, **k: R(jobs))
    a, b = ats.greenhouse({"name": "Acme", "board": "acme"})
    assert a.url == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=8591838002"
    assert a.job_id == job_id("Acme", "Product Manager", site)      # same ID as before
    assert b.url == "https://job-boards.greenhouse.io/acme/jobs/7"


def test_comeet_links_to_comeets_own_job_page(monkeypatch):
    hosted = "https://www.comeet.com/jobs/globex/A4.00A/llm-engineer/86.473"
    page = ('COMPANY_POSITIONS_DATA = [{"uid": "86.473", "name": "LLM Engineer", "url_active_page": '
            '"https://globex.example/open-positions", "url_comeet_hosted_page": "' + hosted + '", "location": {}}];')
    monkeypatch.setattr(ats.requests, "get", lambda *a, **k: R(text=page))
    j, = ats.comeet({"name": "Globex", "board": "globex/A4.00A"})
    assert j.url == hosted
    assert j.job_id == job_id("Globex", "LLM Engineer", "https://globex.example/open-positions")
    one = ats.comeet_one("globex/A4.00A", "86.473")
    assert one["url"] == hosted and one["id_url"] == "https://globex.example/open-positions"


def test_a_pasted_greenhouse_job_page_link_is_read(monkeypatch):
    from agent import intake
    monkeypatch.setattr(ats, "greenhouse_one", lambda board, jid: {"title": "Software Engineer", "description": "x",
                                                                   "url": "u", "id_url": "https://initech.example/careers/job/?gh_jid=7"})
    job = intake.resolve_ats("https://job-boards.greenhouse.io/embed/job_app?for=initech&token=7")
    assert job.source_ref == "greenhouse:initech:7"
    assert job.job_id == job_id(job.company, "Software Engineer", "https://initech.example/careers/job/?gh_jid=7")
