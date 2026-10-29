"""Big-company career sites: Workday tenants, Microsoft, Amazon, Google and Apple.

Only public listing endpoints that the hosts' robots.txt allows (checked 2026-10-22):
  Workday    <host>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs  (not disallowed)
  Microsoft  apply.careers.microsoft.com/api/pcsx/...  (robots.txt: Allow /api/pcsx; rate-limited)
  Amazon     www.amazon.jobs/en/search.json  (only /internal is disallowed)
  Google     www.google.com/about/careers/applications/jobs/results  (first page only:
             robots.txt disallows ?page=, so each query reads its newest 20)
  Apple      jobs.apple.com/<locale>/search and /details  (no robots.txt)
Every source is filtered at the source to the user's country (settings.yaml location.country).
Not used: Meta (robots.txt forbids automated collection), SmartRecruiters (robots.txt allows
only LinkedInBot).

Workday, Microsoft and Apple list a posting without its description. Such jobs carry
extra["detail"] = True, and the pipeline calls DETAIL[source](job) only for new jobs whose
title and date already pass, so a run never fetches hundreds of pages.
"""
import concurrent.futures as cf
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from agent import country
from agent.jobs import Job, html_to_text
from agent.sources.ats import TIMEOUT, UA, _iso


class RateLimited(RuntimeError):
    pass


# --- Workday -----------------------------------------------------------------------------
def _wd_parts(board: str) -> tuple:
    host, tenant, site = board.split("/")
    return f"https://{host}.myworkdayjobs.com", f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}", site


def _wd_list(api: str, facets: dict, offset: int) -> dict:
    r = requests.post(f"{api}/jobs", json={"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""},
                      headers={**UA, "Accept": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _wd_country(facets: list) -> dict:
    """The applied-facets filter for the user's country: the country value if the tenant has
    one, otherwise every location value that names the country."""
    home, places = {}, {}
    names = set(country.names(country.code()))

    def walk(values, param):
        for v in values:
            if "values" in v:
                walk(v["values"], v.get("facetParameter", param))
            elif v.get("descriptor", "").strip().lower() in names:
                home.setdefault(param, []).append(v["id"])
            elif "location" in param.lower() and country.mentions_home(v.get("descriptor", "")):
                places.setdefault(param, []).append(v["id"])

    for f in facets:
        walk(f.get("values", []), f["facetParameter"])
    return home or places


def _wd_posted(text: str) -> str:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    t = (text or "").lower()
    now = datetime.now(timezone.utc)
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*days?", t)
        if not m:
            return ""
        days = int(m.group(1)) + (1 if "+" in t else 0)
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


def _wd_location(p: dict) -> str:
    text = p.get("locationsText") or ""
    if re.search(r"\d+\s+locations", text, re.I) or not text:
        # "5 Locations": the path names the primary one, e.g. /job/Germany-Berlin/...
        m = re.match(r"/job/([^/]+)/", p.get("externalPath", ""))
        text = m.group(1).replace("-", " ") if m else text
    return text


def workday(company: dict) -> list:
    base, api, site = _wd_parts(company["board"])
    first = _wd_list(api, {}, 0)
    facets = _wd_country(first.get("facets", []))
    if not facets:
        return []                                       # no opening in the country right now
    page = _wd_list(api, facets, 0)
    posts = list(page.get("jobPostings", []))
    total = min(page.get("total", 0), 1000)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for more in ex.map(lambda off: _wd_list(api, facets, off), range(20, total, 20)):
            posts.extend(more.get("jobPostings", []))
    out = []
    for p in posts:
        if not p.get("externalPath"):
            continue
        out.append(Job(source="workday", source_ref=f"workday:{company['board']}:{p['externalPath']}",
                       company=company["name"], title=p["title"].strip(), location=_wd_location(p),
                       url=f"{base}/{site}{p['externalPath']}", posted=_wd_posted(p.get("postedOn")),
                       country=country.code(), tier=company.get("tier", 3), extra={"detail": True}))
    return out


def _wd_info(board: str, path: str) -> dict:
    _, api, _ = _wd_parts(board)
    r = requests.get(f"{api}{path}", headers={**UA, "Accept": "application/json"}, timeout=TIMEOUT)
    if r.status_code in (404, 410):
        return None
    r.raise_for_status()
    info = r.json().get("jobPostingInfo") or {}
    return info if info.get("posted", True) else None


def workday_detail(job: Job) -> bool:
    _, rest = job.source_ref.split(":", 1)
    board, path = rest.rsplit(":", 1)
    info = _wd_info(board, path)
    if not info:
        return False
    job.description = html_to_text(info.get("jobDescription", ""))
    job.location = " | ".join(filter(None, [info.get("location", "")] + (info.get("additionalLocations") or []))) or job.location
    job.posted = _iso(info.get("startDate")) or job.posted
    job.employment = info.get("timeType") or ""
    return True


def workday_one(board: str, path: str) -> dict:
    info = _wd_info(board, path)
    if not info:
        return None
    base, _, site = _wd_parts(board)
    return {"title": info["title"], "description": html_to_text(info.get("jobDescription", "")),
            "url": info.get("externalUrl") or f"{base}/{site}{path}"}


# --- Microsoft ---------------------------------------------------------------------------
MS_API = "https://apply.careers.microsoft.com/api/pcsx"
_ms_lock = threading.Lock()
_ms_last = [0.0]


def _ms_get(path: str, params: dict) -> dict:
    with _ms_lock:                                     # the API answers 429 to quick bursts
        wait = 3.0 - (time.time() - _ms_last[0])
        if wait > 0:
            time.sleep(wait)
        _ms_last[0] = time.time()
        r = requests.get(f"{MS_API}/{path}", params={"domain": "microsoft.com", **params}, headers=UA, timeout=TIMEOUT)
    if r.status_code == 429:
        raise RateLimited("Microsoft careers API: too many requests")
    r.raise_for_status()
    return r.json().get("data") or {}


def microsoft(company: dict) -> list:
    out, start, count = [], 0, 1
    while start < min(count, 300):
        try:
            data = _ms_get("search", {"query": "", "location": country.name(), "start": start, "num": 10})
        except RateLimited:
            if out:
                break                                  # keep what we have; the rest next run
            raise
        count = data.get("count") or 0
        positions = data.get("positions") or []
        if not positions:
            break
        for p in positions:
            std = p.get("standardizedLocations") or []
            out.append(Job(source="microsoft", source_ref=f"microsoft:microsoft.com:{p['id']}", company=company["name"],
                           title=p["name"].strip(), location=" | ".join(p.get("locations") or []),
                           url=f"https://apply.careers.microsoft.com{p['positionUrl']}", posted=_iso(p.get("postedTs")),
                           country=_ms_country(std),
                           workplace=(p.get("workLocationOption") or "").lower(), tier=company.get("tier", 3),
                           extra={"detail": True}))
        start += len(positions)
    return out


def _ms_country(std: list) -> str:
    """'Berlin, BE, DE' -> 'DE': the code at the end of each standardized location."""
    codes = {s.rsplit(",", 1)[-1].strip() for s in std if s}
    home = country.code()
    return home if home in codes else (sorted(codes)[0] if len(codes) == 1 and len(next(iter(codes))) == 2 else "")


def _ms_details(pid: str) -> dict:
    try:
        return _ms_get("position_details", {"position_id": pid, "hl": "en"})
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def microsoft_detail(job: Job) -> bool:
    d = _ms_details(job.source_ref.rsplit(":", 1)[1])
    if not d:
        return False
    job.description = html_to_text(d.get("jobDescription", ""))
    job.employment = ", ".join(d.get("efcustomTextEmploymentType") or [])
    return True


def microsoft_one(board: str, pid: str) -> dict:
    d = _ms_details(pid)
    if not d:
        return None
    return {"title": d["name"], "description": html_to_text(d.get("jobDescription", "")),
            "url": d.get("publicUrl") or f"https://apply.careers.microsoft.com/careers/job/{pid}"}


# --- Amazon ------------------------------------------------------------------------------
def _amz_search(params: dict) -> dict:
    r = requests.get("https://www.amazon.jobs/en/search.json", params={"country": country.alpha3(), **params},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _amz_desc(j: dict) -> str:
    parts = [j.get("description", ""), "Basic qualifications", j.get("basic_qualifications", ""),
             "Preferred qualifications", j.get("preferred_qualifications", "")]
    return html_to_text("<br/>".join(p for p in parts if p))


def _amz_posted(s: str) -> str:
    try:
        return datetime.strptime(s.strip(), "%B %d, %Y").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, AttributeError):
        return ""


def amazon(company: dict) -> list:
    out, offset, hits = [], 0, 1
    while offset < min(hits, 500):
        data = _amz_search({"result_limit": 100, "offset": offset, "sort": "recent"})
        hits = data.get("hits") or 0
        jobs = data.get("jobs") or []
        if not jobs:
            break
        for j in jobs:
            out.append(Job(source="amazon", source_ref=f"amazon:{country.alpha3()}:{j['id_icims']}", company=company["name"],
                           title=j["title"].strip(), location=j.get("normalized_location") or j.get("location", ""),
                           url=f"https://www.amazon.jobs{j['job_path']}", posted=_amz_posted(j.get("posted_date", "")),
                           description=_amz_desc(j),
                           country=country.code() if j.get("country_code") == country.alpha3() else "",
                           employment=j.get("job_schedule_type") or "", tier=company.get("tier", 3)))
        offset += len(jobs)
    return out


def amazon_one(board: str, icims: str) -> dict:
    for j in _amz_search({"base_query": icims, "result_limit": 10}).get("jobs", []):
        if str(j.get("id_icims")) == icims:
            return {"title": j["title"], "description": _amz_desc(j), "url": f"https://www.amazon.jobs{j['job_path']}"}
    return None


# --- Google ------------------------------------------------------------------------------
G_BASE = "https://www.google.com/about/careers/applications/jobs/results"
# Each query reads only its first page (the newest 20); robots.txt disallows paging.
G_LEVELS = {"junior": [["EARLY", "MID"], "EARLY"], "mid": [["MID", "ADVANCED"], "MID"],
            "senior": [["ADVANCED"], ["MID", "ADVANCED"]]}


def g_queries() -> list:
    from agent import filters
    return [{"location": country.name(), "target_level": lv, "employment_type": "FULL_TIME", "sort_by": "date"}
            for lv in G_LEVELS[filters.level()]]


def _g_data(html: str, key: str) -> list:
    m = re.search(r"AF_initDataCallback\(\{key: '" + key + r"',\s*(?:hash: '\d+',\s*)?data:(.*?),\s*"
                  r"(?:sideChannel: \{\}|errorHasStatus: true,?)\}\);</script>", html, re.S)
    if not m:
        raise ValueError("Google careers page format changed: no " + key)
    return json.loads(m.group(1))


def _g_text(cell) -> str:
    return html_to_text(cell[1]) if isinstance(cell, list) and len(cell) > 1 and cell[1] else ""


def _g_job(company: dict, j: list, slug: str = "") -> Job:
    here = [loc for loc in (j[9] or []) if loc and loc[-1] == country.code()]
    desc = "\n".join(filter(None, [_g_text(j[10]), "Responsibilities", _g_text(j[3]), "Qualifications", _g_text(j[4])]))
    ts = (j[12] or [None])[0] if len(j) > 12 else None
    return Job(source="google", source_ref=f"google:{country.code().lower()}:{j[0]}", company=j[7] or company["name"],
               title=j[1].strip(), location=" | ".join(loc[0] for loc in here),
               url=f"{G_BASE}/{j[0]}" + (f"-{slug}" if slug else ""), posted=_iso(ts), description=desc,
               country=country.code() if here else "", tier=company.get("tier", 3))


def google(company: dict) -> list:
    out, seen = [], set()
    for q in g_queries():
        r = requests.get(G_BASE, params=q, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        slugs = dict(re.findall(r'jobs/results/(\d+)-([\w-]+)', r.text))
        for j in _g_data(r.text, "ds:1")[0] or []:
            if j[0] not in seen:
                seen.add(j[0])
                out.append(_g_job(company, j, slugs.get(j[0], "")))
    return out


def google_one(board: str, gid: str) -> dict:
    r = requests.get(f"{G_BASE}/{gid}", headers=UA, timeout=TIMEOUT)
    if r.status_code in (404, 410):
        return None
    r.raise_for_status()
    rows = _g_data(r.text, "ds:0")
    if not rows or not isinstance(rows[0], list) or rows[0][0] != gid:
        return None                                    # a closed posting answers with an error block
    job = _g_job({"name": "Google"}, rows[0])
    return {"title": job.title, "description": job.description, "url": job.url}


# --- Apple -------------------------------------------------------------------------------
def a_base(locale: str = "") -> str:
    """Apple's site for a locale (companies.yaml board, e.g. "en-us", "en-gb"); default en-<country>."""
    return f"https://jobs.apple.com/{locale or 'en-' + country.code().lower()}"


def _a_slug() -> str:
    """Apple's location filter: 'united-kingdom-GBR'."""
    return re.sub(r"[^a-z]+", "-", country.name().lower()).strip("-") + "-" + country.alpha3()


def _a_loader(html: str) -> dict:
    m = re.search(r'__staticRouterHydrationData = JSON.parse\((".*?")\);', html, re.S)
    if not m:
        raise ValueError("Apple jobs page format changed: no hydration data")
    return json.loads(json.loads(m.group(1)))["loaderData"]


def _a_page(page: int, locale: str = "") -> dict:
    params = {"location": _a_slug()}
    if page > 1:
        params["page"] = page
    r = requests.get(f"{a_base(locale)}/search", params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return _a_loader(r.text)["search"]


def _a_location(locs: list) -> str:
    return " | ".join(filter(None, (f"{l.get('name') or l.get('city')}, {l.get('countryName') or country.name()}"
                                    for l in locs or [])))


def apple(company: dict) -> list:
    locale = company.get("board") or ""
    first = _a_page(1, locale)
    results = list(first.get("searchResults") or [])
    pages = min(-(-int(first.get("totalRecords") or 0) // 20), 10)
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for more in ex.map(lambda n: _a_page(n, locale), range(2, pages + 1)):
            results.extend(more.get("searchResults") or [])
    out = []
    for p in results:
        locs = p.get("locations") or []
        out.append(Job(source="apple", source_ref=f"apple:{locale or 'en-' + country.code().lower()}:{p['id']}",
                       company=company["name"], title=p["postingTitle"].strip(), location=_a_location(locs),
                       url=f"{a_base(locale)}/details/{p['id']}/{p.get('transformedPostingTitle', '')}",
                       posted=_iso(p.get("postDateInGMT")), description=p.get("jobSummary") or "",
                       country=country.code() if any(l.get("countryID") == f"iso-country-{country.alpha3()}"
                                                     for l in locs) else "",
                       tier=company.get("tier", 3), extra={"detail": True}))
    return out


def _a_details(aid: str, locale: str = "") -> dict:
    r = requests.get(f"{a_base(locale)}/details/{aid}", headers=UA, timeout=TIMEOUT)
    if r.status_code in (404, 410):
        return None
    r.raise_for_status()
    return (_a_loader(r.text).get("jobDetails") or {}).get("jobsData")


def _a_desc(d: dict) -> str:
    parts = [("", d.get("jobSummary")), ("Description", d.get("description")),
             ("Responsibilities", d.get("responsibilities")),
             ("Minimum Qualifications", d.get("minimumQualifications")),
             ("Preferred Qualifications", d.get("preferredQualifications"))]
    return "\n".join(f"{h}\n{html_to_text(t)}".strip() for h, t in parts if t)


def apple_detail(job: Job) -> bool:
    _, locale, aid = job.source_ref.split(":", 2)
    d = _a_details(aid, locale)
    if not d:
        return False
    job.description = _a_desc(d)
    return True


def apple_one(board: str, aid: str) -> dict:
    d = _a_details(aid, board)
    if not d:
        return None
    return {"title": d["postingTitle"], "description": _a_desc(d),
            "url": f"{a_base(board)}/details/{aid}/{d.get('transformedPostingTitle', '')}"}


FETCHERS = {"workday": workday, "microsoft": microsoft, "amazon": amazon, "google": google, "apple": apple}
ONE = {"workday": workday_one, "microsoft": microsoft_one, "amazon": amazon_one, "google": google_one, "apple": apple_one}
DETAIL = {"workday": workday_detail, "microsoft": microsoft_detail, "apple": apple_detail}
# Most descriptions fetched per source in one run; the rest wait for the next run
DETAIL_CAP = {"microsoft": 3}
DETAIL_CAP_DEFAULT = 40
