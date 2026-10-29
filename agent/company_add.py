"""Add a company to watch, from its careers page link or its name.

    detect("https://boards.greenhouse.io/acme")    # a job board link
    detect("https://acme.com/careers")             # a careers page that embeds a job board
    detect("Acme")                                 # a name: its public board is searched for

The company's job system and board ID are read from the link (Greenhouse, Lever, Ashby,
Comeet, Workday), or from the careers page itself when its robots.txt allows reading it, or
guessed from the name and accepted only when that board answers. The board is then read once
to count its open jobs and the ones in the user's country. LinkedIn and job-board sites are
never opened (rule 4).
"""
import re
import urllib.robotparser
from urllib.parse import urlsplit

import requests

from agent import sources
from agent.sources import ats

BOARD_LINKS = [
    (re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", re.I), "greenhouse"),
    (re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)", re.I), "greenhouse"),
    (re.compile(r"(?<![\w.-])(?:job-)?boards\.greenhouse\.io/(?!embed)([\w-]+)", re.I), "greenhouse"),
    (re.compile(r"(?<![\w.-])jobs\.lever\.co/([\w.-]+)", re.I), "lever"),
    (re.compile(r"(?<![\w.-])jobs\.ashbyhq\.com/([\w.%-]+)", re.I), "ashby"),
    (re.compile(r"comeet\.com/jobs/([\w-]+/[\w.]+)", re.I), "comeet"),
    (re.compile(r"(?<![\w.-])([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"), "workday"),
]
NEVER_OPEN = re.compile(r"linkedin\.com|indeed\.|glassdoor\.|monster\.|ziprecruiter\.|t\.me", re.I)


def boards_in(text: str) -> list:
    """Every (ats, board) a link or a page names, in order, without repeats."""
    found = []
    for pattern, kind in BOARD_LINKS:
        for m in pattern.finditer(text or ""):
            board = f"{m.group(1)}.{m.group(2)}/{m.group(1)}/{m.group(3)}" if kind == "workday" else m.group(1)
            if (kind, board) not in found and board.lower() not in ("embed", "v1", "jobs"):
                found.append((kind, board))
    return found


def _page(url: str) -> str:
    """A careers page's HTML, only if its robots.txt allows reading it ('' otherwise)."""
    p = urlsplit(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers=ats.UA, timeout=10)
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        if not rp.can_fetch(ats.UA["User-Agent"], url):
            return ""
        page = requests.get(url, headers=ats.UA, timeout=15)
    except requests.RequestException:
        return ""
    return page.text if page.status_code == 200 else ""


def _name_from(url: str, kind: str, board: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if host and not re.search(r"greenhouse|lever|ashbyhq|comeet|myworkdayjobs", host):
        parts = host.split(".")
        second_level = len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net")
        return parts[-3 if second_level else -2].replace("-", " ").title()
    slug = board.split("/")[0].split(".")[0]
    return re.sub(r"[-_]+", " ", slug).title()


def check(name: str, kind: str, board: str) -> dict:
    """Read the board once: {name, ats, board, tier, jobs, local}, or None if it does not answer."""
    from agent.scout import in_country
    company = {"name": name, "ats": kind, "board": board, "tier": 3}
    try:
        jobs = sources.FETCHERS[kind](company)
    except Exception:
        return None
    return {**company, "jobs": len(jobs), "local": sum(1 for j in jobs if in_country(j))}


def detect(query: str) -> tuple:
    """(company found, '') or (None, why not)."""
    q = (query or "").strip()
    if not q:
        return None, "nothing given"
    from agent.sources.linkedin import _slugs
    if re.match(r"https?://", q):
        if NEVER_OPEN.search(q):
            return None, "I don't open LinkedIn or job-board sites. Send the company's own careers page."
        tries = boards_in(q) or boards_in(_page(q))
        if not tries:
            return None, ("I couldn't find a job board on that page (it may use a system the agent can't read, "
                          "or not allow automatic reading). Try the company's name instead.")
        for kind, board in tries:
            hit = check(_name_from(q, kind, board), kind, board)
            if hit:
                return hit, ""
        return None, "The job board on that page didn't answer. Try again later."
    for slug in _slugs(q):
        for kind in ("greenhouse", "ashby", "lever"):
            hit = check(q, kind, slug)
            if hit and hit["jobs"]:
                return hit, ""
    return None, (f"I couldn't find a public job board for {q}. Send its careers page link: the agent reads "
                  "Greenhouse, Lever, Ashby, Comeet and Workday boards.")


def known(company: dict, names: list) -> bool:
    from agent.sources.linkedin import company_key
    return company_key(company["name"]) in {company_key(n) for n in names}
