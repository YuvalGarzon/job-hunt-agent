"""Public ATS job boards: Greenhouse, Lever, Comeet and Ashby.

Only public endpoints meant for job listings, all allowed by the hosts' robots.txt:
  Greenhouse  boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true
  Lever       api.lever.co/v0/postings/<board>?mode=json   (robots.txt: crawl-delay 1 s)
  Comeet      www.comeet.com/jobs/<slug>/<UID>  (the public careers page embeds the positions)
  Ashby       api.ashbyhq.com/posting-api/job-board/<board>
"""
import json
import re
import threading
import time
from datetime import datetime, timezone

import requests

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
            url=j["absolute_url"], posted=_iso(j.get("first_published") or j.get("updated_at")),
            description=html_to_text(j.get("content", "")), tier=company.get("tier", 3)))
    return out


def greenhouse_one(board: str, job_id: str) -> dict:
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}", headers=UA, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    j = r.json()
    return {"title": j["title"], "description": html_to_text(j.get("content", "")), "url": j["absolute_url"]}


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


def lever(company: dict) -> list:
    r = _lever_get(f"https://api.lever.co/v0/postings/{company['board']}", {"mode": "json"})
    r.raise_for_status()
    return [_lever_job(company, j) for j in r.json()]


def lever_one(board: str, job_id: str) -> dict:
    r = _lever_get(f"https://api.lever.co/v0/postings/{board}/{job_id}", {"mode": "json"})
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
               title=p["name"].strip(), location=loc_text, url=p.get("url_active_page") or p["url_comeet_hosted_page"],
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
            return {"title": j.title, "description": j.description, "url": j.url}
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


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "comeet": comeet, "ashby": ashby}
ONE = {"greenhouse": greenhouse_one, "lever": lever_one, "comeet": comeet_one, "ashby": ashby_one}
