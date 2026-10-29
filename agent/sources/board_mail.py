"""Job-board alert emails (any job site that emails you alerts), used ONLY through those
emails in Gmail (rule 4): the boards' web pages are never opened, not even the job links in
the emails.

Which boards: settings.yaml email_alerts.boards, each a name and the sender address (or just
its domain) the alerts come from, e.g. {name: Indeed, sender: alert@indeed.com}.

Each email is read by Claude (on the computer; in the cloud those emails wait for the computer), which
lists every job's title, company, place and text. Like the LinkedIn alerts:
  1. a job that is also on the company's own careers board (one we know, or a public
     Greenhouse / Lever / Ashby board found now) is taken from there;
  2. otherwise it is sent tagged "not verified on the company site", with the email's text as
     its description (for the score, the filters and the CV).

Links keep only the job page address: the query strings carry tracking and session tokens.
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from agent import config, gmail
from agent.jobs import Job, html_to_text
from agent.sheet import Sheet
from agent.sources import linkedin

FIRST_LOOKBACK_DAYS = 3
# Account emails from a board (passwords, verification codes) hold no jobs and are never sent to Claude
ACCOUNT_MAIL = re.compile(r"password|verification|verify|confirm your|sign in code|security code", re.I)
POSTS_DIR = config.STATE_DIR / "posts"              # each job's text, for its CV (same as intake)
HIDDEN_COMPANY = re.compile(r"^(confidential|undisclosed|hidden|anonymous|a leading company|our client)?$", re.I)


def boards() -> dict:
    """key -> (Gmail sender, name shown on the card), from settings.yaml email_alerts.boards."""
    out = {}
    for b in ((config.settings().get("email_alerts") or {}).get("boards") or []):
        name, sender = str(b.get("name") or "").strip(), str(b.get("sender") or "").strip()
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if key and sender and key not in ("linkedin", "telegram", "web"):
            out[key] = (sender, name)
    return out


def senders() -> list:
    return [s for s, _ in boards().values()]


@dataclass
class BoardJob:
    board: str
    title: str
    company: str
    location: str
    link: str
    description: str
    ref: str = ""                 # the board's own job number, when known
    employment: str = ""
    extra: dict = field(default_factory=dict)


def clean_link(url: str) -> str:
    """The job page address only: no query string (tracking and session tokens)."""
    p = urlsplit((url or "").strip().replace("&amp;", "&"))
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return f"https://{p.netloc}{p.path}"


def clean_title(t: str) -> str:
    """'24128 - Software Engineer' -> 'Software Engineer'; 'QA Tester - 23510' -> 'QA Tester'."""
    t = re.sub(r"\s+", " ", t or "").strip()
    t = re.sub(r"^\d{3,}\s*[-–]\s*", "", t)
    return re.sub(r"\s*[-–]\s*\d{3,}$", "", t).strip()


def employment(text: str) -> str:
    """Full-time whenever full time is offered ('full-time, part-time' is full-time too)."""
    for pattern, kind in ((r"full", "Full-time"), (r"part", "Part-time"), (r"student|intern", "Student"),
                          (r"freelance|contract", "Freelance"), (r"temporary", "Temporary")):
        if re.search(pattern, text or "", re.I):
            return kind
    return ""


def hidden(company: str) -> bool:
    return bool(HIDDEN_COMPANY.match((company or "").strip(" -–—")))


# --- Claude reads the email ----------------------------------------------------------------
EXTRACT = """This is a job-alert email from a job board. List every job offered in it.
Answer with JSON only, no other text:
{"jobs": [{"title": "...", "company": "...", "location": "...", "employment": "...", "link": "...",
           "ref": "...", "description": "..."}]}
- Copy titles, company names and places exactly as written. Use "" for anything not given (a
  hidden or confidential company is "").
- "link": the mark of the job's own link, e.g. "L3": the [Ln] mark inside that job's part of
  the EMAIL (see LINKS), else "".
- "ref": the board's job number if the email shows one, else "".
- "description": everything the email says about that job (requirements, years of experience,
  duties), copied as written, at most 2500 characters.
- Ads, tips, other services and unsubscribe text are not jobs. If there is no job, answer {"jobs": []}.

LINKS (mark -> address):
{links}

EMAIL:
{text}"""


def marked(html: str) -> tuple:
    """The email's text with a mark [Ln] after every link, and the marks' addresses. Many links
    share one label ("View job"), so only the place of the mark tells which job it opens."""
    urls = []

    def mark(m):
        url = clean_link(m.group(1))
        if not url:
            return m.group(2)
        if url not in urls:
            urls.append(url)
        return f"{m.group(2)} [L{urls.index(url) + 1}]"
    html = re.sub(r"<(style|script)\b.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<a\s[^>]*href\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", mark, html, flags=re.S | re.I)
    return html_to_text(text), [(f"L{i + 1}", u) for i, u in enumerate(urls)]


def parse_with_claude(board: str, msg: dict) -> list:
    from agent import claude_cli
    if msg.get("html"):
        text, links = marked(msg["html"])
    else:
        text, links = msg["text"], []
    prompt = (EXTRACT.replace("{links}", "\n".join(f"{l} -> {u}" for l, u in links[:120]) or "(none)")
              .replace("{text}", text[:15000]))
    out = claude_cli.ask(prompt, timeout=300)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    by_mark = dict(links)
    jobs = []
    for j in (data.get("jobs") or [])[:30]:
        j = {k: str(j.get(k) or "").strip() for k in ("title", "company", "location", "employment", "link",
                                                       "ref", "description")}
        link = by_mark.get(j["link"].strip("[] ").upper()) or clean_link(j["link"])
        if j["title"]:
            jobs.append(BoardJob(board=board, title=clean_title(j["title"]),
                                 company="" if hidden(j["company"]) else j["company"],
                                 location=j["location"], link=link,
                                 description=j["description"][:4000], employment=employment(j["employment"]),
                                 ref=re.sub(r"\W", "", j["ref"])[:20]))
    return jobs


def on_mac() -> bool:
    return not os.environ.get("GITHUB_ACTIONS")


def parse(board: str, msg: dict):
    """The jobs in one email, or None when it must wait for the computer (Claude is needed)."""
    if not on_mac():
        return None
    return parse_with_claude(board, msg)


# --- one run -----------------------------------------------------------------------------
def to_job(b: BoardJob, msg_id: str, received_ms: int) -> Job:
    posted = datetime.fromtimestamp(received_ms / 1000, timezone.utc).isoformat(timespec="seconds")
    return Job(source=b.board, source_ref=f"mail:{b.board}:{msg_id}:{b.ref}", company=b.company or "?",
               title=b.title, location=b.location, url=b.link, posted=posted, description=b.description,
               employment=b.employment, extra={"unverified": True, "via_board": b.board})


def save_post(job: Job) -> None:
    """The job's text, for its CV (read by tasks._job_from_sheet; the computer only, where CVs are built).
    In the cloud the email is read again from Gmail instead."""
    if on_mac():
        POSTS_DIR.mkdir(parents=True, exist_ok=True)
        (POSTS_DIR / f"{job.job_id}.txt").write_text(job.description, encoding="utf-8")


def collect(sheet: Sheet, fetched: list, known_companies: list, log=print) -> list:
    """Read new alert emails from every board; return the extra Job records this run evaluates."""
    out = []
    for board, (sender, _) in boards().items():
        key = f"{board}_after"                         # Health: newest email handled (Gmail seconds)
        after = sheet.kv_get("Health", key, "") or \
            str(int((datetime.now(config.tz()) - timedelta(days=FIRST_LOOKBACK_DAYS)).timestamp()))
        ids = gmail.search(f"from:{sender} after:{after}", limit=100)
        newest, items, waiting, read = int(after), [], 0, 0
        ids = ids[::-1]                                  # oldest first
        for i, mid in enumerate(ids):
            m = gmail.message(mid)
            if m["received_ms"] // 1000 <= int(after):   # Gmail's after: includes the cursor's own second
                continue
            read += 1
            jobs = [] if ACCOUNT_MAIL.search(m.get("subject", "")) else parse(board, m)
            if jobs is None:                             # needs Claude: the computer reads it later
                waiting = len(ids) - i
                break
            newest = max(newest, m["received_ms"] // 1000)
            items += [(j, to_job(j, mid, m["received_ms"])) for j in jobs]
        items = list({job.job_id: (b, job) for b, job in items}.values())   # one email can list a job twice
        if items:
            unverified = {id(b): j for b, j in items}
            found = linkedin.resolve(sheet, fetched, known_companies, [b for b, _ in items], "via_board",
                                     lambda b: b.board, lambda b: unverified[id(b)], log=log)
            for j in found:
                if j.extra.get("unverified"):
                    save_post(j)
            out += found
        sheet.kv_set("Health", key, str(newest), flush=False)
        if read:
            log(f"{board}: {read} emails, {len(items)} jobs, "
                f"{sum(1 for j in out if j.source == board)} not on a company board"
                + (f", {waiting} waiting for the computer" if waiting else ""))
    return out


def description(row: dict) -> str:
    """The text of a board job for its CV: saved when it was found, else read again from Gmail."""
    f = POSTS_DIR / f"{row['Job ID']}.txt"
    if f.exists():
        return f.read_text(encoding="utf-8")
    parts = (row.get("Source ref") or "").split(":")
    if len(parts) == 4 and parts[0] == "mail":
        board, mid, ref = parts[1], parts[2], parts[3]
        try:
            jobs = parse(board, gmail.message(mid)) or []
        except Exception:
            jobs = []
        for j in jobs:
            if (ref and j.ref == ref) or (not ref and j.title == row["Title"]):
                return j.description
    return ""
