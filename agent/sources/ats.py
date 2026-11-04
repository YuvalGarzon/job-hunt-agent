"""Public ATS job boards: Greenhouse, Lever, Comeet, Ashby, Workable, Recruitee, SmartRecruiters.

To support another hiring system: add its reader here (FETCHERS: board -> jobs, ONE: one job
again) and a detector in sources/detect.py; the scout then finds every company that uses it.

Only public endpoints meant for job listings, all allowed by the hosts' robots.txt:
  Greenhouse  boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true
  Lever       api.lever.co/v0/postings/<board>?mode=json   (robots.txt: crawl-delay 1 s);
              board "eu/<board>" for Lever's EU site (api.eu.lever.co)
  Comeet      www.comeet.com/jobs/<slug>/<UID>  (the public careers page embeds the positions)
  Ashby       api.ashbyhq.com/posting-api/job-board/<board>
  Workable    apply.workable.com/api/v1/widget/accounts/<board>?details=true
  Recruitee   <board>.recruitee.com/api/offers/
  SmartRecruiters  api.smartrecruiters.com/v1/companies/<board>/postings (the home country only, then each one's ad)
"""
import concurrent.futures as cf
import json
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from agent import country
from agent.jobs import Job, html_to_text

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; job-hunt-agent personal job search)"}
TIMEOUT = 15
_lever_lock = threading.Lock()
_lever_last = [0.0]


def _iso(ts) -> str:
    if ts in (None, ""):
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, tz=timezone.utc).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return ""


# --- Greenhouse --------------------------------------------------------------------------
def _gh_link(board: str, j: dict) -> str:
    """A link that opens this job itself. A company site that embeds Greenhouse often ignores
    ?gh_jid= and shows its whole job list; Greenhouse's own job page always shows the job and
    its apply form (and redirects to an error page once the job closes)."""
    url = j.get("absolute_url") or ""
    if "greenhouse.io" in urlsplit(url).netloc:
        return url
    return f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={j['id']}"


def greenhouse(company: dict) -> list:
    board = company["board"]
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        offices = ", ".join(o.get("name", "") for o in j.get("offices") or [] if o.get("name"))
        loc = j.get("location", {}).get("name", "")
        out.append(Job(
            source="greenhouse", source_ref=f"greenhouse:{board}:{j['id']}",
            company=company["name"], title=j["title"].strip(),
            location=loc if not offices or offices in loc else f"{loc} | {offices}",
            url=_gh_link(board, j), id_url=j["absolute_url"], posted=_iso(j.get("first_published") or j.get("updated_at")),
            description=html_to_text(j.get("content", "")), tier=company.get("tier", 3)))
    return out


def greenhouse_one(board: str, job_id: str) -> dict:
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}", headers=UA, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    j = r.json()
    return {"title": j["title"], "description": html_to_text(j.get("content", "")), "url": _gh_link(board, j),
            "id_url": j["absolute_url"]}


# --- Lever -------------------------------------------------------------------------------
def _lever_get(url: str, params: dict = None):
    with _lever_lock:                                  # robots.txt asks for 1 s between requests
        wait = 1.0 - (time.time() - _lever_last[0])
        if wait > 0:
            time.sleep(wait)
        _lever_last[0] = time.time()
        return requests.get(url, params=params, headers=UA, timeout=TIMEOUT)


def _lever_job(company: dict, j: dict) -> Job:
    cats = j.get("categories") or {}
    locs = cats.get("allLocations") or [cats.get("location", "")]
    desc = "\n".join(filter(None, [j.get("descriptionPlain") or html_to_text(j.get("description", ""))] +
                            [f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in j.get("lists") or []] +
                            [j.get("additionalPlain", "")]))
    return Job(source="lever", source_ref=f"lever:{company['board']}:{j['id']}", company=company["name"],
               title=j["text"].strip(), location=", ".join(filter(None, locs)), url=j["hostedUrl"],
               posted=_iso(j.get("createdAt")), description=desc, country=j.get("country") or "",
               employment=cats.get("commitment") or "", workplace=j.get("workplaceType") or "",
               tier=company.get("tier", 3))


def _lever_api(board: str) -> str:
    region, _, slug = board.rpartition("/")
    return f"https://api.{region + '.' if region else ''}lever.co/v0/postings/{slug}"


def lever(company: dict) -> list:
    r = _lever_get(_lever_api(company["board"]), {"mode": "json"})
    r.raise_for_status()
    return [_lever_job(company, j) for j in r.json()]


def lever_one(board: str, job_id: str) -> dict:
    r = _lever_get(f"{_lever_api(board)}/{job_id}", {"mode": "json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    j = _lever_job({"name": "", "board": board}, r.json())
    return {"title": j.title, "description": j.description, "url": j.url}


# --- Comeet ------------------------------------------------------------------------------
def _comeet_positions(slug_uid: str) -> list:
    r = requests.get(f"https://www.comeet.com/jobs/{slug_uid}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);", r.text, re.S)
    if not m:
        raise ValueError("Comeet page format changed: no COMPANY_POSITIONS_DATA")
    return json.loads(m.group(1))


def _comeet_desc(p: dict) -> str:
    details = ((p.get("custom_fields") or {}).get("details")) or p.get("details") or []
    parts = []
    for d in sorted(details, key=lambda x: x.get("order", 0)):
        parts.append(f"{d.get('name', '')}\n{html_to_text(d.get('value') or '')}")
    return "\n".join(parts)


def _comeet_job(company: dict, p: dict) -> Job:
    loc = p.get("location") or {}
    loc_text = ", ".join(filter(None, [loc.get("city") or loc.get("name"), loc.get("state")]))
    if loc.get("is_remote"):
        loc_text += " (remote)"
    return Job(source="comeet", source_ref=f"comeet:{company['board']}:{p['uid']}", company=company["name"],
               # Comeet's own page always opens this job; the company's page often opens its whole list
               title=p["name"].strip(), location=loc_text, url=p.get("url_comeet_hosted_page") or p.get("url_active_page"),
               id_url=p.get("url_active_page") or p["url_comeet_hosted_page"],
               posted=_iso(p.get("time_updated")), description=_comeet_desc(p), country=loc.get("country") or "",
               employment=p.get("employment_type") or "", workplace=(p.get("workplace_type") or "").lower(),
               tier=company.get("tier", 3), extra={"experience_level": p.get("experience_level") or "",
                                                   "hosted": p.get("url_comeet_hosted_page", "")})


def comeet(company: dict) -> list:
    return [_comeet_job(company, p) for p in _comeet_positions(company["board"])]


def comeet_one(board: str, uid: str) -> dict:
    for p in _comeet_positions(board):
        if p.get("uid") == uid:
            j = _comeet_job({"name": "", "board": board}, p)
            return {"title": j.title, "description": j.description, "url": j.url, "id_url": j.id_url}
    return None


# --- Ashby -------------------------------------------------------------------------------
def _ashby_jobs(board: str) -> list:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("jobs", [])


def _ashby_job(company: dict, j: dict) -> Job:
    locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
    addr = ((j.get("address") or {}).get("postalAddress") or {})
    return Job(source="ashby", source_ref=f"ashby:{company['board']}:{j['id']}", company=company["name"],
               title=j["title"].strip(), location=", ".join(filter(None, locs)), url=j.get("jobUrl") or j.get("applyUrl"),
               posted=_iso(j.get("publishedAt")), description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
               country=_ashby_country(addr.get("addressCountry") or ""),
               employment=j.get("employmentType") or "", workplace=(j.get("workplaceType") or "").lower(),
               tier=company.get("tier", 3))


def _ashby_country(name: str) -> str:
    """Ashby gives the country's name: its code, or '' when unknown."""
    import pycountry
    try:
        return pycountry.countries.lookup(name.strip()).alpha_2 if name.strip() else ""
    except LookupError:
        return ""


def ashby(company: dict) -> list:
    return [_ashby_job(company, j) for j in _ashby_jobs(company["board"]) if j.get("isListed", True)]


def ashby_one(board: str, job_id: str) -> dict:
    for j in _ashby_jobs(board):
        if j.get("id") == job_id:
            job = _ashby_job({"name": "", "board": board}, j)
            return {"title": job.title, "description": job.description, "url": job.url}
    return None


def _is_home(code: str, name: str, home: str) -> bool:
    """A location in the user's own country, by its ISO code or its name."""
    return bool(home) and ((code or "").upper() == home or (name or "").strip().lower() in country.names(home))


# --- Workable ----------------------------------------------------------------------------
def _workable_jobs(board: str) -> list:
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{board}", params={"details": "true"},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("jobs", [])


def _workable_job(company: dict, j: dict) -> Job:
    locs = j.get("locations") or [{"country": j.get("country"), "city": j.get("city"), "countryCode": ""}]
    home = country.code()
    local = any(_is_home(l.get("countryCode"), l.get("country"), home) for l in locs)
    first = (locs[0].get("countryCode") or "").upper() if locs else ""
    return Job(source="workable", source_ref=f"workable:{company['board']}:{j['shortcode']}", company=company["name"],
               title=j["title"].strip(), location=" | ".join(", ".join(filter(None, [l.get("city"), l.get("country")])) for l in locs),
               url=j.get("url") or j.get("shortlink"), posted=_iso(j.get("published_on")),
               description=html_to_text(j.get("description") or ""), country=home if local else first,
               employment=j.get("employment_type") or "", workplace="remote" if str(j.get("telecommuting")) == "True" else "",
               tier=company.get("tier", 3))


def workable(company: dict) -> list:
    return [_workable_job(company, j) for j in _workable_jobs(company["board"])]


def workable_one(board: str, code: str) -> dict:
    for j in _workable_jobs(board):
        if j.get("shortcode") == code:
            job = _workable_job({"name": "", "board": board}, j)
            return {"title": job.title, "description": job.description, "url": job.url}
    return None


# --- Recruitee ---------------------------------------------------------------------------
def _recruitee_offers(board: str) -> list:
    r = requests.get(f"https://{board}.recruitee.com/api/offers/", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("offers", [])


def _recruitee_job(company: dict, o: dict) -> Job:
    code = (o.get("country_code") or "").upper()
    return Job(source="recruitee", source_ref=f"recruitee:{company['board']}:{o['id']}", company=company["name"],
               title=o["title"].strip(), location=o.get("location") or ", ".join(filter(None, [o.get("city"), o.get("country")])),
               url=o.get("careers_url") or o.get("careers_apply_url") or "", posted=_iso(o.get("published_at")),
               description="\n".join(filter(None, [html_to_text(o.get("description") or ""), html_to_text(o.get("requirements") or "")])),
               country=country.code() if _is_home(code, o.get("country"), country.code()) else code,
               employment=o.get("employment_type_code") or "", workplace="remote" if o.get("remote") else "",
               tier=company.get("tier", 3))


def recruitee(company: dict) -> list:
    return [_recruitee_job(company, o) for o in _recruitee_offers(company["board"])]


def recruitee_one(board: str, offer_id: str) -> dict:
    for o in _recruitee_offers(board):
        if str(o.get("id")) == str(offer_id):
            job = _recruitee_job({"name": "", "board": board}, o)
            return {"title": job.title, "description": job.description, "url": job.url}
    return None


# --- SmartRecruiters ---------------------------------------------------------------------
SR_API = "https://api.smartrecruiters.com/v1/companies"


def _sr_ad(board: str, posting_id: str) -> dict:
    r = requests.get(f"{SR_API}/{board}/postings/{posting_id}", headers=UA, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _sr_text(ad: dict) -> str:
    sections = ((ad or {}).get("jobAd") or {}).get("sections") or {}
    return "\n".join(f"{v.get('title', '')}\n{html_to_text(v.get('text') or '')}" for v in sections.values() if v.get("text"))


def _sr_job(company: dict, p: dict, ad: dict) -> Job:
    loc = p.get("location") or {}
    return Job(source="smartrecruiters", source_ref=f"smartrecruiters:{company['board']}:{p['id']}", company=company["name"],
               title=p["name"].strip(), location=loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("country")])),
               url=(ad or {}).get("postingUrl") or f"https://jobs.smartrecruiters.com/{company['board']}/{p['id']}",
               posted=_iso(p.get("releasedDate")), description=_sr_text(ad), country=(loc.get("country") or "").upper(),
               employment=(p.get("typeOfEmployment") or {}).get("label") or "",
               workplace="remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "",
               tier=company.get("tier", 3), extra={"experience_level": (p.get("experienceLevel") or {}).get("label") or ""})


def smartrecruiters(company: dict) -> list:
    board, postings, offset = company["board"], [], 0
    while offset < 500:
        r = requests.get(f"{SR_API}/{board}/postings", params={"country": country.code().lower(), "limit": 100, "offset": offset},
                         headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        postings += data.get("content") or []
        offset += 100
        if offset >= data.get("totalFound", 0):
            break
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        ads = list(ex.map(lambda p: _sr_ad(board, p["id"]), postings))     # every ad: a job is never scored blind
    return [_sr_job(company, p, ad) for p, ad in zip(postings, ads)]


def smartrecruiters_one(board: str, posting_id: str) -> dict:
    ad = _sr_ad(board, posting_id)
    if not ad or ad.get("active") is False:
        return None
    return {"title": ad.get("name", ""), "description": _sr_text(ad), "url": ad.get("postingUrl") or ""}


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "comeet": comeet, "ashby": ashby, "workable": workable,
            "recruitee": recruitee, "smartrecruiters": smartrecruiters}
ONE = {"greenhouse": greenhouse_one, "lever": lever_one, "comeet": comeet_one, "ashby": ashby_one,
       "workable": workable_one, "recruitee": recruitee_one, "smartrecruiters": smartrecruiters_one}
