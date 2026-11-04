"""Which job board a company's careers page uses, so the agent can read it from then on.

Each detector reads the page's HTML and returns (ats, board) pairs that sources.ats or
sources.bigco can fetch. To support another hiring system: add its reader (FETCHERS and ONE)
and one detector here. The scout runs every detector on each careers page it sees, and checks
the companies it could not watch again every two weeks, so new companies are picked up by
themselves. Many pages load their job list with JavaScript: when the plain page shows no
board, it is opened once in headless Chrome or Chromium (when one is installed).
"""
import json
import re
import shutil
import subprocess
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlsplit

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; job-hunt-agent personal job search)"}
CHROME_PATHS = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),          # macOS
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),            # Windows
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")]
CHROME_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]   # Linux, PATH


def chrome() -> str:
    """The Chrome or Chromium program on this computer; '' when there is none."""
    for path in CHROME_PATHS:
        if path.exists():
            return str(path)
    return next((found for name in CHROME_NAMES if (found := shutil.which(name))), "")
RENDER_SECONDS = 15
DETECTORS = []


def detector(fn):
    DETECTORS.append(fn)
    return fn


LINKS = [("comeet", re.compile(r"comeet\.(?:com|co)/jobs/([\w.-]+/[0-9A-F]{2}\.[0-9A-F]{3})", re.I)),
         ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)", re.I)),
         ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", re.I)),
         ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)", re.I)),
         ("lever", re.compile(r"jobs\.(eu)\.lever\.co/([\w-]+)", re.I)),
         ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I)),
         ("workable", re.compile(r"apply\.workable\.com/([\w-]+)", re.I)),
         ("recruitee", re.compile(r"https?://([\w-]+)\.recruitee\.com", re.I)),
         ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)),
         ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([\w-]+)", re.I))]
UID_FIRST = re.compile(r"[0-9A-F]{2}\.[0-9A-F]{3}/", re.I)
NOT_BOARDS = {"embed", "js", "jobs", "api", "wday", "j", "privacy-policy", "assets", "static", "careers", "v1"}


@detector
def board_links(html: str) -> list:
    """Links to a public board, or a board's widget, anywhere on the page."""
    out = []
    for kind, pat in LINKS:
        out += [(kind, "/".join(m.groups())) for m in pat.finditer(html) if m.group(m.lastindex).lower() not in NOT_BOARDS
                and not (kind == "comeet" and UID_FIRST.match(m.group(1)))]   # comeet.co/jobs/<UID>/<job>: a widget link
    out += [("workday", f"{m.group(1)}/{m.group(2)}/{m.group(3)}") for m in re.finditer(
        r"https://(([\w-]+)\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", html)
        if m.group(3).lower() not in NOT_BOARDS]
    return out


COMEET_UID = re.compile(r"comeet_uid\"?\s*[:=]\s*\"([0-9A-F]{2}\.[0-9A-F]{3})\"|company-uid=([0-9A-F]{2}\.[0-9A-F]{3})"
                        r"|comeet\.co/jobs/([0-9A-F]{2}\.[0-9A-F]{3})/", re.I)
COMEET_TOKEN = re.compile(r"comeet_token\"?\s*[:=]\s*\"([0-9A-F]{20,})\"|[?&;]token=([0-9A-F]{20,})", re.I)


@detector
def comeet_widget(html: str) -> list:
    """Comeet built into the company's own site: its company id and token are on the page, and
    Comeet's careers API gives the public board address (slug/UID) the comeet reader uses."""
    uid, token = COMEET_UID.search(html), COMEET_TOKEN.search(html)
    if not (uid and token):
        return []
    uid = next(g for g in uid.groups() if g)
    token = next(g for g in token.groups() if g)
    try:
        r = requests.get(f"https://www.comeet.co/careers-api/2.0/company/{uid}/positions",
                         params={"token": token}, headers=UA, timeout=15)
        positions = r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        return []
    for p in positions if isinstance(positions, list) else []:
        m = re.search(r"comeet\.com/jobs/([\w.-]+/[0-9A-F]{2}\.[0-9A-F]{3})/", p.get("url_comeet_hosted_page") or "", re.I)
        if m:
            return [("comeet", m.group(1))]
    return []


def render(url: str) -> str:
    """The page as a browser shows it (after its JavaScript ran); '' without Chrome."""
    browser = chrome()
    if not browser:
        return ""
    try:
        p = subprocess.run([browser, "--headless=new", "--disable-gpu", f"--virtual-time-budget={RENDER_SECONDS * 1000}",
                            "--dump-dom", url], capture_output=True, text=True, timeout=RENDER_SECONDS + 45)
        return p.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _run(html: str) -> list:
    out = []
    for fn in DETECTORS:
        try:
            out += fn(html)
        except Exception:                               # one broken detector never stops the others
            continue
    return list(dict.fromkeys(out))


def allowed(url: str) -> bool:
    """The site's robots.txt lets the agent read this page (no robots.txt: yes)."""
    p = urlsplit(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers=UA, timeout=10)
    except requests.RequestException:
        return True
    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    return rp.can_fetch(UA["User-Agent"], url)


def boards_on_page(url: str, use_browser: bool = True) -> list:
    """(ats, board) pairs a careers page points to, most likely first; [] if none, if it does not
    load, or if the site's robots.txt asks agents not to read it."""
    if not url.startswith("http") or not allowed(url):
        return []
    try:
        html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).text
    except requests.RequestException:
        html = ""
    found = _run(html)
    if not found and use_browser:
        found = _run(render(url))
    return found[:8]
