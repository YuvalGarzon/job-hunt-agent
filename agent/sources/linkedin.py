"""LinkedIn job-alert emails, read through the Gmail API. LinkedIn itself is never opened.

An alert gives a title, a company, a place and a LinkedIn link. A job is alerted only when it
links to the company's own careers page, so each alert job is looked up there:
  1. in the postings fetched this run from the boards we know (companies.yaml + Boards tab);
  2. otherwise the company's public Greenhouse / Lever / Ashby board is searched for by name
     (public APIs only). A board found is saved in the Boards tab and fetched every run from
     then on; a miss is saved too and checked again after 30 days.
A job found neither way is recorded as dropped ("LinkedIn only"), never alerted.

The links in the emails carry sign-in tokens: only the job number is kept, and the stored link
is the bare https://www.linkedin.com/jobs/view/<number>/.
"""
import concurrent.futures as cf
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import requests

from agent import filters, gmail
from agent.jobs import Job, norm_text
from agent.sheet import TZ, Sheet, now_iso
from agent.sources import ats

SENDER = "jobalerts-noreply@linkedin.com"
CURSOR = "linkedin_after"                 # Health key: Gmail internalDate (s) of the newest email handled
FIRST_LOOKBACK_DAYS = 3
DISCOVER_PER_RUN = 3
RECHECK_DAYS = 30
NOISE = re.compile(r"^(your job alert for|new jobs? match|this company is actively hiring|actively recruiting|"
                   r"apply with resume|easy apply|promoted|see all jobs|\d+ (company )?(alumni|alum|connections?|"
                   r"school alumni?)\b|\d+ applicants?|be an early applicant|-+$)", re.I)
VIEW = re.compile(r"View job:\s*\S*?/jobs/view/(\d+)")


@dataclass
class AlertJob:
    title: str
    company: str
    location: str
    li_id: str

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/jobs/view/{self.li_id}/"


def parse(text: str) -> list:
    """The job cards in one alert email."""
    out = []
    for block in re.split(r"\r?\n[ \t]*-{10,}[ \t]*\r?\n", text):
        m = VIEW.search(block)
        if not m:
            continue
        lines = [l.strip() for l in block[:m.start()].splitlines()]
        lines = [l for l in lines if l and "http" not in l and not NOISE.match(l)]
        if len(lines) >= 2:
            out.append(AlertJob(title=lines[0], company=lines[1], location=lines[2] if len(lines) > 2 else "",
                                li_id=m.group(1)))
    return out


# --- matching ----------------------------------------------------------------------------
def short_company(name: str) -> str:
    """'Acme | Counter-Drone ...' -> 'Acme'; 'Applied Materials - Germany' -> 'Applied Materials';
    'Solid (AI Powered by Data)' -> 'Solid'."""
    s = re.split(r"\s[|·]\s|\s-\s", name or "")[0]
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


def company_key(name: str) -> str:
    s = norm_text(short_company(name))
    s = re.sub(r"\b(ltd|inc|llc|corp|corporation|co|limited|gmbh|plc|group|technologies|technology|the)\b\.?", " ", s)
    return re.sub(r"[^\w]+", "", s)


def same_title(a: str, b: str) -> bool:
    a, b = norm_text(a), norm_text(b)
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.9


def find_in(alert: AlertJob, jobs: list):
    key = company_key(alert.company)
    for j in jobs:
        ck = company_key(j.company)
        if ck and key and (ck == key or ck.startswith(key) or key.startswith(ck)) and same_title(alert.title, j.title):
            return j
    return None


# --- board discovery ---------------------------------------------------------------------
def _slugs(company: str) -> list:
    base = short_company(company)
    words = [w for w in re.findall(r"[a-z0-9]+", base.lower())
             if w not in ("ltd", "inc", "llc", "corp", "limited", "gmbh", "plc", "the", "co")]
    if not words:
        return []
    out = ["".join(words), "-".join(words), words[0]]
    return list(dict.fromkeys(s for s in out if len(s) >= 3))


def discover(company: str, titles: list) -> tuple:
    """The company's public board, accepted only if it lists one of these titles:
    ({"ats", "board"}, its jobs), or ({}, [])."""
    slugs = _slugs(company)
    tries = [(k, s) for s in slugs for k in ("greenhouse", "ashby")] + [("lever", s) for s in slugs[:1]]
    for kind, slug in tries:
        comp = {"name": short_company(company), "ats": kind, "board": slug, "tier": 3}
        try:
            jobs = ats.FETCHERS[kind](comp)               # Lever calls keep its 1 s crawl-delay
        except (requests.RequestException, ValueError, KeyError):
            continue
        if any(same_title(t, j.title) for t in titles for j in jobs):
            return {"ats": kind, "board": slug}, jobs
    return {}, []


def boards(sheet: Sheet) -> list:
    """Boards found through LinkedIn alerts, in companies.yaml form."""
    return [{"name": r["Company"], "ats": r["ATS"], "board": r["Board"], "tier": 3}
            for r in sheet.t("Boards").rows if r["Status"] == "found" and r["ATS"] in ats.FETCHERS]


# --- one run -----------------------------------------------------------------------------
def collect(sheet: Sheet, fetched: list, known_companies: list, log=print) -> list:
    """Read new alert emails and return the extra Job records this run should evaluate: postings
    found on newly discovered boards, and LinkedIn-only jobs (extra["unverified"])."""
    sheet.load("Boards")
    after = sheet.kv_get("Health", CURSOR, "")
    if not after:
        after = str(int((datetime.now(TZ) - timedelta(days=FIRST_LOOKBACK_DAYS)).timestamp()))
    ids = gmail.search(f"from:{SENDER} after:{after}", limit=200)
    newest = int(after)
    alerts, seen_ids = [], set()
    for mid in reversed(ids):                             # oldest first
        m = gmail.message(mid)
        newest = max(newest, m["received_ms"] // 1000)
        for a in parse(m["text"]):
            if a.li_id not in seen_ids:
                seen_ids.add(a.li_id)
                alerts.append(a)

    out = resolve(sheet, fetched, known_companies, alerts, "via_linkedin", lambda a: a.li_id,
                  lambda a: Job(source="linkedin", source_ref="", company=short_company(a.company), title=a.title,
                                location=a.location, url=a.url, extra={"unverified": True, "via_linkedin": a.li_id}),
                  log=log)
    sheet.kv_set("Health", CURSOR, str(newest), flush=False)
    on_known = len(alerts) - len(out)
    log(f"linkedin: {len(ids)} emails, {len(alerts)} jobs, {on_known} on known boards, "
        f"{sum(1 for j in out if not j.extra.get('unverified'))} on new boards, "
        f"{sum(1 for j in out if j.extra.get('unverified'))} LinkedIn-only")
    return out


def resolve(sheet: Sheet, fetched: list, known_companies: list, alerts: list, tag: str, tag_value,
            unverified, log=print) -> list:
    """Alert jobs (each with .title, .company, .location) -> the extra Job records to evaluate:
    the company's own posting when it is on a board we know (tagged in place, not returned) or on
    a public board discovered now (returned), else unverified(alert) (returned)."""
    missing = []
    for a in alerts:
        hit = find_in(a, fetched)
        if hit:
            hit.extra[tag] = tag_value(a)
        else:
            missing.append(a)
    out = []

    # Unknown companies: look for their public board (a few per run, cached in the Boards tab).
    # Only for jobs whose title and date already pass, so no lookup is wasted on a senior role.
    known = {company_key(c) for c in known_companies}
    tab = sheet.t("Boards")
    cached = {company_key(r["Company"]): r for r in tab.rows}
    wanted = {}
    for a in missing:
        k = company_key(a.company)
        row = cached.get(k)
        stale = row is not None and row["Status"] not in ("found", "blocked") and _age_days(row["Checked"]) > RECHECK_DAYS
        probe = Job(source="alert", source_ref="", company=a.company, title=a.title, location=a.location, url="")
        if k and k not in known and (row is None or stale) and not filters.cheap_drop(probe):
            wanted.setdefault(k, (a.company, []))[1].append(a.title)
    picked = list(wanted.items())[:DISCOVER_PER_RUN]
    board_jobs = {}
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        results = ex.map(lambda item: discover(item[1][0], item[1][1]), picked)
        for (k, (company, _)), (res, jobs) in zip(picked, results):
            rec = {"Company": short_company(company), "ATS": res.get("ats", ""), "Board": res.get("board", ""),
                   "Status": "found" if res else "none", "Checked": now_iso()}
            if k in cached:
                tab.update(cached[k], **rec)
            else:
                tab.append(rec)
            if res:
                board_jobs[k] = jobs
                log(f"found {res['ats']} board '{res['board']}' for {rec['Company']}")

    for a in missing:
        hit = find_in(a, board_jobs.get(company_key(a.company), []))
        if hit:
            hit.extra[tag] = tag_value(a)
            out.append(hit)
        else:
            out.append(unverified(a))
    return out


def _age_days(ts: str) -> float:
    try:
        return (datetime.now(TZ) - datetime.fromisoformat(ts)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9
