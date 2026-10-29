"""Company scout: every day, look for more companies to watch (startups, scale-ups, offices of
large companies) near the user.

Once a day from 11:00 (Mac, Claude with web search) Claude suggests tech companies near the
user's home city that are not watched yet, a different sector mix each day. Each suggestion is
checked against the company's public job board API (Greenhouse, Lever, Ashby, Comeet); job-board
sites and LinkedIn are never opened (rule 4).
- A company with a public board that has jobs in the user's country is added to the Boards tab
  ("found"), so every search from now on reads it and fitting jobs arrive as normal cards.
- A company without a public board is kept as "suggested": the agent cannot watch it.
The user gets one message per company: what it does, its careers page, and buttons to have a
general CV made for it (the user sends it) or to drop it ("blocked", never suggested again).
setup.py runs the same search once, so a new user starts with companies near them.
"""
import json
import re
from datetime import datetime

import requests

from agent import alerts, claude_cli, config, country, filters, learn, sources
from agent import telegram as tg
from agent.sheet import TZ, Sheet, now_iso
from agent.sources import ats
from agent.sources.linkedin import _slugs, company_key

SCOUT_HOUR = 11
MAX_SUGGEST = 12
MAX_MESSAGES = 6
FOCUS = ["AI and machine learning startups", "data infrastructure and analytics", "cybersecurity",
         "fintech and insurtech", "health-tech and medical imaging",
         "computer vision, robotics, drones and autonomous systems", "developer tools and cloud infrastructure",
         "e-commerce, marketing tech and gaming", "engineering offices of global tech companies",
         "deep-tech and space startups", "climate, energy, agri-tech and mobility"]

PROMPT = """Find tech companies that may hire {name}, who is looking for a full-time role ({level}
level) as: {roles}. They live in {city}, {country} and can work up to about {radius} km from
there (remote roles in {country} are fine too). Today's focus: {focus}.
Prefer startups, scale-ups and offices of larger companies with an engineering team in that
area that hire at that level. Do NOT include any of these (already watched or declined): {known}.

Use web search. For each company find its own careers page, and if the page uses a public job
board, the board's ID: greenhouse (boards.greenhouse.io/<id> or job-boards.greenhouse.io/<id>),
lever (jobs.lever.co/<id>), ashby (jobs.ashbyhq.com/<id>) or comeet (comeet.com/jobs/<slug>/<UID>,
give "<slug>/<UID>"). Never open linkedin.com, job-board sites (Indeed, Glassdoor and the like)
or any page that needs a login. Never guess an ID: leave it empty if you did not see it.

Return up to {n} companies as JSON only:
{{"companies": [{{"name": "...", "about": "one line: what they do",
"why": "one line: which of the target roles they hire for",
"careers": "https://...", "ats": "greenhouse|lever|ashby|comeet|other", "board": "..."}}]}}"""


def maybe_queue(sheet: Sheet) -> bool:
    """Called by the Mac worker's search loop: one scout task a day from 11:00."""
    from agent.bot import queue_task
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    sheet.load("Health", "Settings")
    if now.hour < SCOUT_HOUR or sheet.kv_get("Health", "scout_date", "") == today \
            or sheet.kv_get("Settings", "paused", "no") == "yes":
        return False
    sheet.kv_set("Health", "scout_date", today, flush=False)
    sheet.load("Queue")
    queue_task(sheet, "scout", "", "", {}, f"scout-{today}")
    sheet.flush()
    return True


def _known(sheet: Sheet) -> tuple:
    names = [c["name"] for c in sources.companies()] + [r["Company"] for r in sheet.t("Boards").rows]
    return names, {company_key(n) for n in names} | set(learn.blocked(sheet))


def _home_city() -> str:
    try:
        return str((config.profile().get("private_never_on_cv") or {}).get("home_city") or "")
    except config.SetupNeeded:                      # setup.py may add companies before the profile exists
        return ""


def in_country(job) -> bool:
    """A posting in the user's country, as far as its board tells without a lookup."""
    loc = job.location or ""
    city = _home_city()
    return job.country == country.code() or country.mentions_home(loc) or \
        bool(city and re.search(r"(?<!\w)" + re.escape(city) + r"(?!\w)", loc, re.I))


def _board(name: str, kind: str, board: str) -> tuple:
    """(ats, board, local jobs) for a board that answers and lists jobs in the country, else ("", "", [])."""
    tries = [(kind, board.strip())] if kind in ats.FETCHERS and board.strip() else []
    tries += [(k, s) for s in _slugs(name) for k in ("greenhouse", "ashby", "lever")]
    for k, b in dict.fromkeys(tries):
        try:
            jobs = ats.FETCHERS[k]({"name": name, "ats": k, "board": b, "tier": 3})
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
        local = [j for j in jobs if in_country(j)]
        if local:
            return k, b, local
    return "", "", []


def ask_claude(names: list, n: int = MAX_SUGGEST) -> list:
    from agent.geo import radius_km
    day = datetime.now(TZ).timetuple().tm_yday
    focus = " and ".join(FOCUS[(day * 2 + i) % len(FOCUS)] for i in range(2))
    roles = ", ".join(config.settings().get("role_folders", [])[:10])
    out = claude_cli.ask(PROMPT.format(name=config.first_name(), level=filters.level(), roles=roles,
                                       city=_home_city(), country=country.name(), radius=round(radius_km()),
                                       focus=focus, known=", ".join(sorted(set(names)))[:6000], n=n),
                         allow_web=True, timeout=1500)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    return [c for c in data.get("companies") or [] if isinstance(c, dict) and str(c.get("name") or "").strip()]


def scout(sheet: Sheet, notify: bool = True, log=print) -> tuple:
    """One scouting round: (companies suggested, companies added, companies now watched)."""
    sheet.load("Boards", "Settings", "Jobs")
    names, known = _known(sheet)
    found = ask_claude(names)
    tab, sent, added, watched = sheet.t("Boards"), 0, 0, 0
    for c in found:
        name = re.sub(r"\s+", " ", str(c["name"])).strip()[:60]
        key = company_key(name)
        if not key or key in known:
            continue
        known.add(key)
        kind, board, jobs = _board(name, str(c.get("ats") or "").lower(), str(c.get("board") or ""))
        careers = str(c.get("careers") or "")
        if not careers.startswith("http"):
            careers = ""
        fit = [j for j in jobs if not filters.cheap_drop(j)]
        tab.append({"Company": name, "ATS": kind, "Board": board, "Status": "found" if kind else "suggested",
                    "Checked": now_iso(), "About": " · ".join(str(c.get(k) or "") for k in ("about", "why"))[:300],
                    "Careers": careers, "Found by": "scout"})
        added += 1
        watched += bool(kind)
        log(f"scout: {name}: " + (f"{kind} board, {alerts.count(len(jobs), 'local job')}" if kind else "no public board"))
        if notify and sent < MAX_MESSAGES:
            tell(name, c, kind, jobs, fit, careers)
            sent += 1
    sheet.flush()
    return len(found), added, watched


def run_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    found, added, watched = scout(sheet, log=log)
    log(f"scout: {found} suggested, {added} new, {watched} watched")
    return f"{added} new compan{'y' if added == 1 else 'ies'}"


def _cb(key: str) -> str:
    """A company key that fits in Telegram's 64-byte callback data."""
    return key.encode("utf-8")[:48].decode("utf-8", "ignore")


def tell(name: str, c: dict, kind: str, jobs: list, fit: list, careers: str) -> None:
    e = tg.esc
    lines = [f"🏢 <b>A new company for you: {e(name)}</b>"]
    for k in ("about", "why"):
        if c.get(k):
            lines.append(e(str(c[k])[:200]))
    if kind:
        lines.append(f"👀 Added to the search: {alerts.count(len(jobs), 'open job')} in {e(country.name())}"
                     + (f", {len(fit)} pass{'es' if len(fit) == 1 else ''} the first filters. The ones that fit will come to you as cards." if fit
                        else ". None fits right now, but I'll watch it and send one when it does."))
    else:
        lines.append("ℹ️ It has no public job board, so I can't watch it. Its careers page is worth a look.")
    key = _cb(company_key(name))
    buttons = [[("📄 CV for this company", f"cocv:{key}"), ("🚫 Not interested", f"cono:{key}")]]
    tg.send_message("\n".join(lines), buttons=buttons + (tg.link_row(careers, "🔗 Careers page") if careers else []))


def find_row(sheet: Sheet, key: str):
    for r in sheet.t("Boards").rows:
        if _cb(company_key(r["Company"])) == key:
            return r
    return None


GENERAL = """Job title: {roles} (general application)
Company: {name}
Location: {country}
A general application to {name}, not for a specific opening.
About the company and the roles there that fit: {about}"""


def general_cv(sheet: Sheet, key: str, ctx: dict) -> None:
    """📄 CV for this company: a CV aimed at the company itself, for the user to send through its
    careers page or a general application address (the user sends it; the agent never does)."""
    from agent.bot import queue_task
    row = find_row(sheet, key)
    if not row:
        tg.send_message("I couldn't find that company in the list.")
        return
    roles = " / ".join(config.settings().get("role_folders", [])[:3]) or "Software Engineer"
    text = GENERAL.format(roles=roles, name=row["Company"], country=country.name(), about=row.get("About") or "-")
    queue_task(sheet, "intake", "", "", {"text": text, "urls": [], "approve": True},
               f"intake-co-{key[:30]}-{now_iso()[:10]}")
    sheet.flush()
    tg.send_message(f"📄 Building a CV for <b>{tg.esc(row['Company'])}</b>"
                    + ("…" if ctx.get("mac_online") else " when the Mac is back.")
                    + "\nWhen it's ready, send it through their careers page, form or general CV address.")


def drop(sheet: Sheet, key: str) -> None:
    row = find_row(sheet, key)
    if not row:
        return
    sheet.t("Boards").update(row, Status="blocked", Checked=now_iso())
    learn.block(sheet, row["Company"])
    sheet.flush()
    tg.send_message(f"🚫 Removed <b>{tg.esc(row['Company'])}</b> from the search. I won't suggest it again.")
