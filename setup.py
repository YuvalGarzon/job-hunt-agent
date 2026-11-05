"""Set up your job-hunt agent, step by step.

    python setup.py            # the full setup, or pick one step to redo
    python setup.py <step>     # one step: about, cv, search, telegram, sheet, gmail,
                               # alerts, companies, basecv, test, worker
(Run it with the project's Python: activate .venv first, see the README.)

It asks about you and the jobs you want, reads your CV to draft your profile, and walks you
through connecting Claude Code, Telegram, a Google Sheet and Gmail. Everything personal stays
on this computer: your answers in PROFILE.yaml and settings.yaml (never committed), keys in
your system's keychain (macOS Keychain, Windows Credential Manager or the Linux Secret Service).
"""
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from agent import config  # noqa: E402

EXAMPLE_PROFILE = config.EXAMPLES / "PROFILE.example.yaml"
EXAMPLE_SETTINGS = config.EXAMPLES / "settings.example.yaml"
EXAMPLE_COMPANIES = config.EXAMPLES / "companies.example.yaml"
BOLD, DIM, GREEN, RED, END = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


# --- asking ------------------------------------------------------------------------------
def say(text: str = "") -> None:
    print(text)


def title(text: str) -> None:
    print(f"\n{BOLD}== {text} =={END}")


def ok(text: str) -> None:
    print(f"{GREEN}✓{END} {text}")


def warn(text: str) -> None:
    print(f"{RED}!{END} {text}")


def ask(question: str, default: str = "", required: bool = False) -> str:
    hint = f" {DIM}[{default}]{END}" if default else ""
    while True:
        answer = input(f"{question}{hint}: ").strip() or default
        if answer or not required:
            return answer
        warn("This one is needed.")


def yes(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def words(question: str, default: list = ()) -> list:
    answer = ask(question + " (comma-separated)", ", ".join(default))
    return [w.strip() for w in answer.split(",") if w.strip()]


def secret(question: str) -> str:
    return getpass.getpass(f"{question} (hidden, paste and press Enter): ").strip()


def wait(text: str = "Press Enter when it's done") -> None:
    input(f"{DIM}{text}…{END}")


def open_file(path: Path, text: bool = False) -> None:
    """Open a file with the system's own app (a text editor for text=True)."""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", "-t", str(path)] if text else ["open", str(path)])
    elif system == "Windows":
        if text:
            subprocess.Popen(["notepad", str(path)])
        else:
            os.startfile(str(path))                              # noqa: the Windows-only call
    else:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if text and editor:
            subprocess.run([editor, str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])


def edit(path: Path) -> None:
    """Open a file in a text editor and wait until the user is done."""
    open_file(path, text=True)
    wait(f"Opened {path} in your text editor. Save your changes, then press Enter")


# --- files -------------------------------------------------------------------------------
def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}


# a new user's starting settings: nothing about the jobs yet (the "jobs you want" step asks)
DEFAULTS = {"worker": {"model": "sonnet", "effort": "medium"}, "fit_threshold_default": 50,
            "cover_letter_languages": ["English"], "cv_root": "CVs"}


def settings() -> dict:
    return load(config.SETTINGS_PATH) or json.loads(json.dumps(DEFAULTS))


def save_settings(s: dict) -> None:
    """settings.yaml, keeping the example's comments as a header."""
    head = "# settings.yaml: how the agent searches. Change it here or run `python setup.py search`.\n"
    config.SETTINGS_PATH.write_text(head + yaml.safe_dump(s, allow_unicode=True, sort_keys=False, width=110),
                                    encoding="utf-8")
    config.reload()


def settings_country() -> str:
    from agent import country
    try:
        return country.name() or "your country"
    except config.SetupNeeded:
        return "your country"


def system_timezone() -> str:
    """This computer's time zone name (macOS and Linux); UTC when it can't be read (Windows)."""
    try:
        target = Path("/etc/localtime").resolve()
        return str(target).split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def keychain(name: str) -> str:
    from agent import secrets
    return secrets.get(name, required=False)


def store(name: str, value: str) -> None:
    from agent import secrets
    secrets.set_local(name, value)


# --- steps -------------------------------------------------------------------------------
def step_check() -> bool:
    title("Checking your computer")
    good = True
    if platform.system() not in ("Darwin", "Linux", "Windows"):
        warn(f"{platform.system()} is not supported: the agent runs on macOS, Windows and Linux.")
        good = False
    else:
        ok({"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}[platform.system()])
    if sys.version_info < (3, 11):
        warn(f"Python 3.11 or newer is needed (this is {platform.python_version()}).")
        good = False
    else:
        ok(f"Python {platform.python_version()}")
    if not shutil.which("claude"):
        warn("Claude Code is not installed. Install it (https://claude.com/claude-code), run `claude` once "
             "to sign in, then run setup again.")
        good = False
    else:
        ok("Claude Code is installed")
    return good


def step_about() -> None:
    title("1. About you")
    say("These details go on your CV header. Leave a line empty to keep it off the CV.")
    p = load(config.PROFILE_PATH)
    ident = p.get("identity") or {}
    c = ident.get("contact") or {}
    name = ask("Your full name", ident.get("name", ""), required=True)
    email = ask("Email", c.get("email", ""), required=True)
    phone = ask("Phone", c.get("phone", ""))
    linkedin = ask("LinkedIn profile URL", c.get("linkedin", ""))
    github = ask("GitHub profile URL", c.get("github", ""))
    website = ask("Personal website or portfolio URL", c.get("website", ""))

    import pycountry
    s = settings()
    loc = s.get("location") or {}
    while True:
        raw = ask("The country you're searching in (name or 2-letter code)", loc.get("country", ""), required=True)
        try:
            land = pycountry.countries.lookup(raw)
            break
        except LookupError:
            warn("I don't know that country. Try its English name or its 2-letter code (e.g. GB, US, DE).")
    home = ask("Your home city or town (used only to measure distance, never written anywhere)",
               (p.get("private_never_on_cv") or {}).get("home_city", ""), required=True)
    radius = ask("How far from home can a job be, in km", str(loc.get("radius_km", 50)))
    shown = ask("Location shown on your CV (e.g. the country, or 'City, Country')",
                c.get("location") or getattr(land, "common_name", None) or land.name)

    from agent import geo
    try:
        point = geo.geocode_home(home, land.alpha_2)
    except Exception:
        point = None
    if point:
        ok(f"Found {home}: {point[0]:.4f}, {point[1]:.4f}")
    else:
        warn(f"I couldn't find {home} on the map. Check the spelling, or set location.home in settings.yaml.")
        point = loc.get("home") or [0, 0]

    s["location"] = {"country": land.alpha_2, "radius_km": int(float(radius or 50)), "home": [round(point[0], 4),
                                                                                             round(point[1], 4)]}
    s["timezone"] = ask("Your time zone (like Europe/London or America/New_York)",
                        s.get("timezone") if config.SETTINGS_PATH.exists() else system_timezone())
    save_settings(s)

    contact = {k: v for k, v in (("location", shown), ("phone", phone), ("email", email), ("linkedin", linkedin),
                                 ("github", github), ("website", website)) if v}
    if p:
        p.setdefault("identity", {})["name"] = name
        p["identity"]["contact"] = contact
        p.setdefault("private_never_on_cv", {})["home_city"] = home
        config.PROFILE_PATH.write_text(yaml.safe_dump(p, allow_unicode=True, sort_keys=False, width=110),
                                       encoding="utf-8")
    else:
        # kept until the CV step writes the full profile
        (config.STATE_DIR).mkdir(parents=True, exist_ok=True)
        (config.STATE_DIR / "setup_about.json").write_text(json.dumps(
            {"name": name, "contact": contact, "home_city": home}), encoding="utf-8")
    ok("Saved")


PROFILE_PROMPT = """Write this person's PROFILE.yaml for a job-search agent, from their CV.

Follow EXACTLY the format of the EXAMPLE below (another, made-up person): the same keys, the
same structure, and keep its helpful comments. Replace every value with facts from THE CV.
Rules:
- Only facts that are in the CV. Never invent a tool, number, title, date or project. If the
  CV has no projects or no experience, write an empty list for that section.
- identity.name, identity.contact and private_never_on_cv.home_city: exactly from ABOUT.
- skills.strong: tools the CV shows were used in depth (several places, or central to a
  project or job); skills.used: the other tools in the CV. skill_groups_on_cv: 3 to 5 groups
  that fit these skills.
- experience: newest first; bullets_approved: the CV's own bullets, lightly cleaned up, never
  with new facts. Add bullets_short / bullets_related only when an entry is clearly unrelated
  to the person's target field (then bullets_short is one short line).
- projects: facts copied from the CV, with numbers exactly as the CV states them; url only
  when the CV gives one.
- cv_rules.titles_held: only job titles the person really held, plus the title their degree
  directly qualifies them for (e.g. "Software Engineer" for a software engineering degree).
  specialty: only if the degree names one. lead_project: the strongest project's id, or remove
  the key. related_job_words: only if some experience is outside the target field.
- writing_rules: keep the example's banned_words and forbidden_words lists; adapt "never" to
  this person, and always keep its rules about claims and soft skills.
Answer with the YAML only, in one ```yaml block.

ABOUT:
{about}

EXAMPLE:
```yaml
{example}
```

THE CV:
{cv}"""


def _cv_text(path: Path) -> str:
    import pymupdf
    doc = pymupdf.open(path)
    return "\n".join(page.get_text() for page in doc)


def step_cv() -> None:
    title("2. Your profile, from your CV")
    say("Your PROFILE.yaml is the only source of facts about you: the agent never writes anything")
    say("that is not in it. I'll draft it from your CV with Claude, then you check every part.")
    if config.PROFILE_PATH.exists() and not yes("You already have a PROFILE.yaml. Draft it again from your CV?", False):
        return
    about_file = config.STATE_DIR / "setup_about.json"
    about = json.loads(about_file.read_text()) if about_file.exists() else None
    if not about:
        step_about()
        about = json.loads(about_file.read_text()) if about_file.exists() else {}
    raw = ask("Path to your current CV (PDF) — drag the file into this window", required=False)
    path = Path(raw.strip().strip("'\"").replace("\\ ", " ")).expanduser() if raw else None
    if not path or not path.exists():
        warn("No CV file. I'll start from the example profile: replace Alex Morgan's facts with yours.")
        text = EXAMPLE_PROFILE.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        data["identity"]["name"] = about.get("name", data["identity"]["name"])
        data["identity"]["contact"] = about.get("contact", data["identity"]["contact"])
        data["private_never_on_cv"]["home_city"] = about.get("home_city", "")
        config.PROFILE_PATH.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110),
                                       encoding="utf-8")
        edit(config.PROFILE_PATH)
        return
    from agent import claude_cli
    say("Reading your CV with Claude (about a minute)…")
    answer = claude_cli.ask(PROFILE_PROMPT.format(about=json.dumps(about, ensure_ascii=False, indent=1),
                                                  example=EXAMPLE_PROFILE.read_text(encoding="utf-8"),
                                                  cv=_cv_text(path)[:20000]), timeout=600)
    text = claude_cli.extract_block(answer, "yaml")
    try:
        data = yaml.safe_load(text)
        assert isinstance(data, dict) and data.get("identity")
    except Exception:
        warn("Claude's draft was not valid YAML. Starting from the example instead.")
        text = EXAMPLE_PROFILE.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    config.PROFILE_PATH.write_text(text.strip() + "\n", encoding="utf-8")
    config.reload()
    review_profile(data)


def review_profile(data: dict) -> None:
    """Show each part of the profile and let the user fix it."""
    parts = [("Education", data.get("education")), ("Skills", data.get("skills")),
             ("Experience", [{k: e.get(k) for k in ("dates", "role", "org", "bullets_approved")}
                             for e in data.get("experience") or []]),
             ("Projects", [{k: p.get(k) for k in ("name", "url", "stack", "facts")} for p in data.get("projects") or []]),
             ("CV rules", data.get("cv_rules"))]
    wrong = []
    for name, part in parts:
        title(f"Check: {name}")
        say(yaml.safe_dump(part, allow_unicode=True, sort_keys=False, width=110).strip() or "(empty)")
        if name == "Skills":
            say(f"{DIM}strong = you can answer in depth in an interview; used = used in a real project.{END}")
        if name == "CV rules":
            say(f"{DIM}titles_held: the only titles the CV may open with — titles you can truthfully claim.{END}")
        if not yes(f"Is the {name if name.startswith('CV') else name.lower()} part right?"):
            wrong.append(name)
    if wrong:
        say(f"Fix these parts in PROFILE.yaml: {', '.join(wrong)}. Add anything missing; remove anything untrue.")
        edit(config.PROFILE_PATH)
    try:
        yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8"))
        ok("PROFILE.yaml saved")
    except yaml.YAMLError as e:
        warn(f"PROFILE.yaml has a format error now: {e}")
        edit(config.PROFILE_PATH)
    config.reload()


ACRONYMS = {"ml", "ai", "qa", "ui", "ux", "sre", "nlp", "llm", "bi", "it", "ios", "devops", "mlops", "c++", "c#"}


def role_name(role: str) -> str:
    """'ml engineer' -> 'ML Engineer'; a name with capitals is kept as typed."""
    if role != role.lower():
        return role
    fix = {"ios": "iOS", "devops": "DevOps", "mlops": "MLOps"}
    return " ".join(fix.get(w, w.upper()) if w in ACRONYMS else w.capitalize() for w in role.split())


def step_search() -> None:
    title("3. The jobs you want")
    s = settings()
    se = s.get("search") or {}
    roles = words("Which roles are you looking for? (these get the top score)",
                  se.get("preferred_titles") or ["software engineer"])
    good = words("Other roles you'd also take", se.get("good_titles") or ["software developer", "backend"])
    level = ask("Your level: junior, mid or senior", se.get("level", "junior")).lower()
    level = level if level in ("junior", "mid", "senior") else "junior"
    from agent.filters import MAX_YEARS_BY_LEVEL
    years = ask("The most years of experience a job may ask for", str(se.get("max_years", MAX_YEARS_BY_LEVEL[level])))
    p = load(config.PROFILE_PATH)
    spec_default = se.get("specialty_words") or (p.get("cv_rules") or {}).get("specialty_job_words") or []
    specialty = words("Words that mean a job is in your specialty (raise its score)", spec_default)
    exclude = words("Words that rule a job out when they're in its title", se.get("exclude_title_words") or [])
    say("Job types: full-time is always included.")
    types = ["full-time"] + [t for t in ("part-time", "contract", "internship")
                             if yes(f"  Also {t} jobs?", t in (se.get("job_types") or []))]
    remote = yes("Do you want fully remote jobs too?", se.get("include_remote", True))
    avoid = words("Companies you never want jobs from", se.get("avoid_companies") or [])
    favorite = words("Companies you'd love to work for (their jobs score higher)", se.get("favorite_companies") or [])
    say(f"{DIM}Each job gets a fit score from 0 to 100. Lower threshold = more jobs, higher = only the best fits.{END}")
    threshold = ask("Send me jobs scoring at least", str(s.get("fit_threshold_default", 50)))
    langs = words("Languages you may want cover letters in", s.get("cover_letter_languages") or ["English"])
    s["search"] = {"level": level, "max_years": int(years or 3), "preferred_titles": roles, "good_titles": good,
                   "specialty_words": specialty, "exclude_title_words": exclude, "job_types": types,
                   "include_remote": remote, "avoid_companies": avoid, "favorite_companies": favorite}
    s["fit_threshold_default"] = int(threshold or 50)
    if keychain("SHEET_ID"):                    # the Sheet holds the live threshold (/threshold)
        try:
            from agent.sheet import Sheet
            sheet = Sheet()
            sheet.load("Settings")
            sheet.kv_set("Settings", "fit_threshold", s["fit_threshold_default"])
        except Exception:
            pass
    s["cover_letter_languages"] = langs or ["English"]
    s["role_folders"] = [role_name(r) for r in roles + good][:12]
    s.setdefault("worker", {"model": "sonnet", "effort": "medium"})
    s.setdefault("cv_root", "CVs")
    save_settings(s)
    ok("Saved to settings.yaml")
    if p and yes("Review your CV rules too (titles you hold, specialty, words never to write)?", False):
        edit(config.PROFILE_PATH)


def step_telegram() -> None:
    title("4. Telegram (where the agent talks to you)")
    say("1. In Telegram, open @BotFather and send /newbot. Pick any name and a username ending in 'bot'.")
    say("2. BotFather answers with a token like 123456:ABC-DEF… Copy it.")
    token = secret("The bot token") or keychain("TELEGRAM_BOT_TOKEN")
    if not token:
        warn("No token: skipped")
        return
    import requests
    me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    if not me.get("ok"):
        warn("Telegram doesn't accept that token. Copy it again from BotFather.")
        return
    store("TELEGRAM_BOT_TOKEN", token)
    bot = me["result"]["username"]
    ok(f"Bot @{bot} works")
    say(f"3. Now open your bot (https://t.me/{bot}) and send it any message, e.g. hello.")
    chat = None
    for _ in range(60):
        updates = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json().get("result", [])
        for u in updates:
            m = u.get("message") or {}
            if m.get("chat", {}).get("type") == "private":
                chat = str(m["chat"]["id"])
        if chat:
            requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": max(u["update_id"] for u in updates) + 1}, timeout=15)
            break
        time.sleep(3)
    if not chat:
        warn("I didn't see your message. Run `python setup.py telegram` to try again.")
        return
    store("TELEGRAM_CHAT_ID", chat)
    from agent import telegram
    telegram.send_message("👋 Hi! I'm your job-hunt agent. Setup is connecting me; job cards will come here.")
    ok("Connected: check Telegram for a hello")


def step_sheet() -> None:
    title("5. Google Sheet (the agent's memory and your application tracker)")
    say("The agent keeps everything in one Google Sheet, through a Google Cloud service account.")
    say("1. Open https://console.cloud.google.com/ and create a project (any name, e.g. job-hunt-agent).")
    say("2. Enable the Google Sheets API: https://console.cloud.google.com/apis/library/sheets.googleapis.com")
    say("3. IAM & Admin › Service Accounts › Create service account (any name, no roles needed).")
    say("4. Open it › Keys › Add key › Create new key › JSON. The file downloads to ~/Downloads.")
    wait("Press Enter when the key file is in Downloads")
    sys.path.insert(0, str(ROOT / "scripts"))
    import import_sa_key
    try:
        path, data = import_sa_key.find_key()
    except SystemExit as e:
        warn(str(e))
        return
    email = import_sa_key.import_key(path, data)
    say(f"5. Create a new Google Sheet (https://sheets.new), click Share, and add {BOLD}{email}{END} as Editor.")
    url = ask("6. Paste the Sheet's URL", required=True)
    m = re.search(r"/d/([\w-]{20,})", url)
    if not m:
        warn("That doesn't look like a Google Sheets URL.")
        return
    store("SHEET_ID", m.group(1))
    from agent.sheet import Sheet
    try:
        sheet = Sheet()
        sheet.ensure_tabs()
        sheet.load("Settings")
        sheet.kv_set("Settings", "fit_threshold", settings().get("fit_threshold_default", 50))
    except Exception as e:
        warn(f"I couldn't open the Sheet ({e.__class__.__name__}). Check that it's shared with {email}.")
        return
    subprocess.run([sys.executable, str(ROOT / "scripts" / "format_sheet.py")], cwd=ROOT)
    ok("The Sheet is ready: its 'My Applications' tab is your tracker")


def step_gmail() -> None:
    title("6. Gmail (job-alert emails and company replies)")
    say("The agent reads your job alerts and replies from companies; it can only send email to you.")
    say("In the same Google Cloud project:")
    say("1. Enable the Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com")
    say("2. APIs & Services › OAuth consent screen: External; add your own Gmail address as a Test user.")
    say("3. Credentials › Create credentials › OAuth client ID › Desktop app. Download the JSON to ~/Downloads.")
    say(f"{DIM}While the app is in 'Testing', Google ends the sign-in after 7 days and the agent reminds you")
    say("to run `python scripts/gmail_auth.py` again. To avoid that, publish the app (consent screen ›")
    say(f"Publish app), then sign in with `python scripts/gmail_auth.py --production`.{END}")
    wait("Press Enter when the client file is in Downloads")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gmail_auth.py")], cwd=ROOT)
    if r.returncode == 0:
        ok("Gmail connected")
    else:
        warn("Gmail sign-in didn't finish. Run `python scripts/gmail_auth.py` when ready.")


def step_alerts() -> None:
    title("7. Job alerts by email")
    say("The agent reads job-alert emails in your Gmail (it never opens the job sites themselves).")
    say("LinkedIn: on linkedin.com/jobs, search for your role and place, turn on 'Set alert', and")
    say("choose email delivery. The alerts come from jobalerts-noreply@linkedin.com.")
    s = settings()
    alerts = s.get("email_alerts") or {}
    alerts["linkedin"] = yes("Read LinkedIn job alerts?", alerts.get("linkedin", True))
    boards = alerts.get("boards") or []
    say("Other job sites that email you alerts (any site): give each one's name and the address the")
    say("alerts come from (open one alert email and look at the sender).")
    if boards:
        say("Now: " + ", ".join(f"{b['name']} ({b['sender']})" for b in boards))
        if not yes("Keep these?"):
            boards = []
    while yes("Add a job site?", not boards):
        name = ask("  Site name (e.g. Indeed)", required=True)
        sender = ask("  Sender address or domain (e.g. alert@indeed.com)", required=True)
        boards.append({"name": name, "sender": sender})
    alerts["boards"] = boards
    s["email_alerts"] = alerts
    save_settings(s)
    ok("Saved")


def save_companies(data: dict) -> None:
    """companies.yaml, keeping the example's explanation at the top."""
    head = "".join(line + "\n" for line in EXAMPLE_COMPANIES.read_text(encoding="utf-8").splitlines()
                   if line.startswith("#"))
    body = {"companies": data.get("companies") or [], "tier1_names": data.get("tier1_names") or [],
            "tier2_names": data.get("tier2_names") or []}
    config.COMPANIES_PATH.write_text(head + "\n" + yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                                                                  default_flow_style=None, width=110),
                                     encoding="utf-8")


def step_companies() -> None:
    title("8. Companies to watch")
    say("The agent reads the job boards of these companies every 15 minutes, and only their jobs in")
    say(f"{settings_country()} reach you.")
    data = load(config.COMPANIES_PATH) or load(EXAMPLE_COMPANIES)
    comps = data.get("companies") or []
    for i, c in enumerate(comps, 1):
        print(f"  {i:2}. {c['name']}  {DIM}({c['ats']}){END}")
    drop = ask("Numbers of companies to remove (e.g. 3, 7), or Enter to keep them all")
    gone = {int(n) for n in re.findall(r"\d+", drop) if 1 <= int(n) <= len(comps)}
    comps = [c for i, c in enumerate(comps, 1) if i not in gone]
    if gone:
        ok(f"Removed {len(gone)}")

    from agent import alerts, company_add
    say("\nAdd companies you want to watch: paste a company's careers page link, or just its name.")
    say(f"{DIM}The agent reads Greenhouse, Lever, Ashby, Comeet and Workday job boards; it finds which one a{END}")
    say(f"{DIM}company uses. Companies on other systems can still reach you through job-alert emails.{END}")
    while True:
        q = ask("Careers page link or company name (Enter when done)")
        if not q:
            break
        say("  Looking…")
        found, why = company_add.detect(q)
        if not found:
            warn(why)
            continue
        if company_add.known(found, [c["name"] for c in comps]):
            warn(f"{found['name']} is already on the list.")
            continue
        ok(f"{found['name']}: a {found['ats']} board with {alerts.count(found['jobs'], 'open job')}, "
           f"{found['local']} in {settings_country()}")
        name = ask("  Company name", found["name"])
        favorite = yes("  Is it one of your favorite companies (its jobs score higher)?", False)
        comps.append({"name": name, "ats": found["ats"], "board": found["board"], "tier": 1 if favorite else 3})
    data["companies"] = comps
    save_companies(data)
    ok(f"companies.yaml saved: {len(comps)} companies")
    say(f"{DIM}Later, add more in Telegram with /company <link or name>, or edit companies.yaml.{END}")
    if not yes("Ask Claude to find more tech companies near you now? (a few minutes, uses web search)"):
        return
    from agent import scout
    from agent.sheet import Sheet
    try:
        found, added, watched = scout.scout(Sheet(), notify=False, log=lambda m: say(f"  {m}"))
    except Exception as e:
        warn(f"The company search failed: {e.__class__.__name__}: {e}")
        return
    ok(f"{added} new compan{'y' if added == 1 else 'ies'} found; {watched} with a public job board, "
       "now watched every run")
    say(f"{DIM}The agent keeps looking for more every day at 11:00 and tells you on Telegram.{END}")


def step_basecv() -> None:
    title("9. Your general CV")
    say("The agent tailors every CV from one general CV. I'll build it now from your profile.")
    from agent import cv_builder
    roles = settings().get("role_folders") or ["Software Engineer"]
    job = {"company": "General", "title": f"{roles[0]} (general CV)", "location": "",
           "description": "A general CV for the roles: " + ", ".join(roles) + ". Show the strongest true "
                          "projects and skills for these roles."}
    try:
        res = cv_builder.build(job, out_dir=config.cv_root() / "General CV", log=lambda m: say(f"  {m}"))
    except Exception as e:
        warn(f"The CV build failed: {e}")
        say("Fix what it names in PROFILE.yaml and run `python setup.py basecv` again.")
        return
    shutil.copy(res["content"], config.BASE_CONTENT)
    ok(f"Saved {res['pdf']}")
    ok("base_cv.yaml saved: every tailored CV starts from it")
    open_file(res["pdf"])


def step_test() -> None:
    title("10. A first search")
    say("Searching now: new jobs that fit will come to Telegram as cards (this takes a minute or two).")
    from agent import run
    failed = run.run_once(send=True, force=True, owner="local", log=lambda m: say(f"  {m}"))
    if failed:
        warn(f"Some steps failed: {', '.join(failed)} (see the lines above)")
    else:
        ok("Done. Check Telegram")


def step_worker() -> None:
    title("11. Run in the background")
    say("The worker runs while your computer is awake: it answers Telegram, builds CVs and searches")
    say("every 15 minutes. It starts by itself when you log in.")
    if not yes("Install the background worker now?"):
        return
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_worker.py")], cwd=ROOT)
    if r.returncode != 0:
        warn("The installer failed; see the message above.")
        return
    ok("Installed. Logs: ~/.job-hunt-agent/logs/worker.log")
    tip = {"Darwin": "System Settings › Battery › Options (or run: sudo pmset -c sleep 0)",
           "Windows": "Settings › System › Power & battery › Screen and sleep: 'Never' when plugged in",
           "Linux": "your desktop's Power settings (automatic suspend: off when plugged in)"}
    say(f"{DIM}Tip: the agent works only while the computer is awake. To keep it awake when plugged in:")
    say(f"{tip.get(platform.system(), 'your power settings')}{END}")


STEPS = [("about", "About you", step_about), ("cv", "Your profile, from your CV", step_cv),
         ("search", "The jobs you want", step_search), ("telegram", "Telegram", step_telegram),
         ("sheet", "Google Sheet", step_sheet), ("gmail", "Gmail", step_gmail),
         ("alerts", "Job alerts by email", step_alerts), ("companies", "Companies to watch", step_companies),
         ("basecv", "Your general CV", step_basecv), ("test", "A first search", step_test),
         ("worker", "Run in the background", step_worker)]


def done(key: str) -> bool:
    s = load(config.SETTINGS_PATH)
    return {"about": bool((s.get("location") or {}).get("home")), "cv": config.PROFILE_PATH.exists(),
            "search": bool(s.get("search")), "telegram": bool(keychain("TELEGRAM_CHAT_ID")),
            "sheet": bool(keychain("SHEET_ID")), "gmail": bool(keychain("GMAIL_REFRESH_TOKEN")),
            "alerts": "email_alerts" in s, "companies": config.COMPANIES_PATH.exists(),
            "basecv": config.BASE_CONTENT.exists(),
            "worker": worker_installed()}.get(key, False)


def worker_installed() -> bool:
    sys.path.insert(0, str(ROOT / "scripts"))
    import install_worker
    return install_worker.installed()


def main() -> None:
    by_key = {k: fn for k, _, fn in STEPS}
    if len(sys.argv) > 1:
        fn = by_key.get(sys.argv[1])
        if not fn:
            sys.exit(f"unknown step '{sys.argv[1]}'; steps: {', '.join(by_key)}")
        fn()
        return
    print(f"{BOLD}Job-hunt agent setup{END}  {DIM}(Ctrl+C stops at any time; your answers so far are kept){END}")
    if not step_check():
        sys.exit("Fix the items above, then run setup again.")
    title("Steps")
    for i, (key, label, _) in enumerate(STEPS, 1):
        print(f"  {i:2}. {GREEN + '✓' + END if done(key) else ' '} {label}")
    pick = ask("\nPress Enter to go through every step not done yet, or type a step's number", "")
    if pick.isdigit() and 1 <= int(pick) <= len(STEPS):
        STEPS[int(pick) - 1][2]()
        return
    for key, label, fn in STEPS:
        if done(key) and not yes(f"'{label}' is done. Do it again?", False):
            continue
        fn()
    title("All set")
    say("Your agent is running. In Telegram, send /help to see what it can do.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run `python setup.py` again to continue.")
