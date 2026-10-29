"""Hard filters. A job is alerted only if it passes every one; each drop has a written reason.

Order: title (software / data / tech work) -> seniority and years -> employment -> recency ->
location -> scam signs. Live-posting verification runs later, only for jobs that pass and score.

The level the user searches at (settings.yaml search.level: junior | mid | senior) sets which
titles are too senior and how many years of experience a posting may ask for (search.max_years).
"""
import re
from datetime import datetime, timedelta, timezone

from agent import config

MAX_AGE_DAYS = 30

# A title passes when it names a software / tech role. Unsure -> keep, and the score decides.
ROLE_NOUN = re.compile(
    r"engineer|engineering|developer|programmer|builder|scientist|researcher|research|\bsw\b|\bdev\b|devops|"
    r"\bsre\b|data analyst|analytics|architect|\bqa\b|tester|\btest\b|automation", re.I)
SOFTWARE_CONTEXT = re.compile(
    r"software|\bsw\b|data|\bml\b|machine learning|\bai\b|genai|\bllm|\bnlp\b|algorithm|vision|"
    r"perception|robotic|autonomy|embedded|firmware|backend|back-end|frontend|front-end|full.?stack|"
    r"mobile|ios|android|devops|\bsre\b|reliability|platform|infrastructure|cloud|security|cyber|"
    r"integration|simulation|game|unity|automation|\bqa\b|test|python|java|c\+\+|golang|node|react|"
    r"web|api|deep learning|research|engineer|developer|programmer|scientist", re.I)

NOT_SOFTWARE = [
    (r"\b(sales|account (executive|manager)|business development|bdr|sdr|partner(ship)? manager|"
     r"customer success|presales|pre-sales|sales engineer|solutions? consultant)\b", "sales role"),
    (r"\b(recruit|talent|hr\b|human resources|people (partner|operations)|payroll|benefits)", "HR role"),
    (r"\b(it support|helpdesk|help desk|desktop support|it technician|it system|it engineer|"
     r"system administrator|sysadmin|it operations|technical support|support engineer|"
     r"customer support|support specialist|tier [123])\b", "IT/support role"),
    (r"\b(manual qa|manual test)", "manual QA"),
    (r"\b(hardware engineer|asic|vlsi|layout engineer|analog|rf engineer|pcb|board design|"
     r"mechanical|electrical engineer|electronics engineer|physical design|verification engineer|"
     r"dft engineer|chip|silicon validation|optical engineer)\b", "hardware role"),
    (r"\b(physicist|electro[- ]?optics?|optics?|photonics?|laser|practical engineer|package design|packaging|"
     r"ic|soc|rtl|ddr|design verification|design for test|dft|emulation|cad engineer|cpu design|npi|"
     r"quality engineer|supplier quality|customer engineer|field (service )?engineer|service engineer|"
     r"process engineer|manufacturing|metrology|yield|failure analysis|chemist|thermal|power electronics|"
     r"electrical|electronics|control systems)\b", "hardware role"),
    (r"\b(marketing|content writer|copywriter|designer|ux|ui/ux|product manager|product owner|"
     r"project manager|program manager|legal|counsel|finance|accountant|controller|procurement|"
     r"office manager|operations manager|business analyst|financial analyst|bookkeep|paralegal|"
     r"customer|community manager|social media|brand|events?\b|executive assistant)", "not a software role"),
]

# Titles too senior for each level
SENIOR_BY_LEVEL = {
    "junior": r"\b(senior|sr\.?|lead|leader|staff|principal|manager|head of|head,|director|vp\b|vice president|"
              r"chief|architect|expert|distinguished|fellow|group lead|tech lead|team lead|founding)\b",
    "mid": r"\b(staff|principal|manager|head of|head,|director|vp\b|vice president|chief|distinguished|fellow|"
           r"group lead)\b",
    "senior": r"\b(head of|head,|director|vp\b|vice president|chief)\b",
}
MAX_YEARS_BY_LEVEL = {"junior": 3, "mid": 6, "senior": 20}
# Titles that ask for some experience without naming years: still sent, scored as a stretch
STRETCH_TITLE = re.compile(r"\b(experienced|mid[-\s]*level|intermediate|iii)\b", re.I)
NOT_FULLTIME = re.compile(
    r"\b(intern|internship|student|working student|part[- ]time|freelance|contract|contractor|temporary|temp\b|"
    r"summer|co-op|apprentice)\b", re.I)
# A title that clearly names software work: never dropped as hardware, and scores as a software role
SOFTWARE_TITLE = re.compile(
    r"software|\bsw\b|firmware|\bfw\b|embedded|algorithm|machine learning|\bml\b|\bai\b|genai|\bllm|\bnlp\b|"
    r"\bdata\b|devops|\bsre\b|backend|back-end|frontend|front-end|full.?stack|cloud|platform|infrastructure|"
    r"security|cyber|automation|\bqa\b|developer|programmer|scientist|research|python|java|c\+\+|golang|"
    r"\bweb\b|mobile|\bios\b|android|game|vision|perception|robotic|autonomy|simulation", re.I)
# Within those, the words that outweigh a hardware word ("Firmware Engineer, SoC" stays; "AI SoC
# Design Verification" does not)
SOFTWARE_OVER_HW = re.compile(r"software|\bsw\b|firmware|\bfw\b|algorithm|machine learning|\bml\b|"
                              r"data (engineer|scien)|devops|backend|full.?stack|developer|programmer", re.I)
GRADUATE = re.compile(r"\b(junior|entry[- ]level|graduate|new grad|fresh grad|recent grad|early career|"
                      r"0-1 years|0-2 years|no experience)\b", re.I)

ADVANTAGE = re.compile(r"advantage|nice to have|a plus|bonus|preferred", re.I)
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b", re.I)

# "3+ years building and shipping ML models": years of doing something, without the word "experience"
YEARS_OF_WORK = re.compile(r"\d\s*\+\s*(?:years?|yrs?)\b|(?:at least|minimum(?: of)?)\s+\d{1,2}\s*(?:years?|yrs?)\b"
                           r"|\b\d{1,2}\s*(?:-|–|to)?\s*\d{0,2}\s*(?:years?|yrs?)\s+(?:of|in|as|building|developing|"
                           r"designing|shipping|writing|programming|coding|leading|delivering|deploying)\b", re.I)


def level() -> str:
    lv = str(config.search().get("level", "junior")).lower()
    return lv if lv in SENIOR_BY_LEVEL else "junior"


def max_years() -> int:
    try:
        return int(config.search().get("max_years", MAX_YEARS_BY_LEVEL[level()]))
    except (TypeError, ValueError):
        return MAX_YEARS_BY_LEVEL[level()]


def _senior_re():
    return re.compile(SENIOR_BY_LEVEL[level()], re.I)


def _excluded(title: str) -> str:
    """A word the user never wants in a title (settings.yaml search.exclude_title_words)."""
    for w in config.search().get("exclude_title_words") or []:
        if w and re.search(r"(?<!\w)" + re.escape(str(w)) + r"(?!\w)", title or "", re.I):
            return f"title has '{w}' (your exclude list)"
    return ""


def required_years(text: str) -> int:
    """Minimum years the posting requires (max over non-'advantage' lines). -1 if none."""
    best = -1
    for line in re.split(r"[\n•;]|(?<=\.)\s", text or ""):
        if ADVANTAGE.search(line):
            continue
        if not re.search(r"experience|background|hands-on|working|industry|proven", line, re.I) \
                and not YEARS_OF_WORK.search(line):
            continue
        for m in YEARS.finditer(line):
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 0 <= n <= 20:
                best = max(best, n)
    return best


def title_check(title: str) -> str:
    """'' if the title is a software / tech role, else the drop reason."""
    t = title or ""
    excluded = _excluded(t)
    if excluded:
        return excluded
    for pattern, reason in NOT_SOFTWARE:
        if re.search(pattern, t, re.I):
            # keep e.g. "Data Engineer, Marketing Platform" or "Security Engineer, Customer Trust"
            if re.search(r"\b(engineer|developer|scientist|programmer)\b", t, re.I) and \
                    reason in ("not a software role",):
                continue
            if reason == "hardware role" and SOFTWARE_OVER_HW.search(t):
                continue
            return reason
    if not ROLE_NOUN.search(t) or not SOFTWARE_CONTEXT.search(t):
        return "not a software role"
    if re.search(r"\b(specialist|associate|coordinator|administrator|officer|representative|"
                 r"consultant|economist|operator|technician|clerk|agent)\b", t, re.I) and \
            not re.search(r"\b(engineer|developer|scientist|programmer|researcher)\b", t, re.I):
        return "not a software role"
    return ""


def seniority(job) -> dict:
    """{"drop": reason or "", "stretch": bool, "years": int, "graduate": bool}"""
    m = _senior_re().search(job.title or "")
    if m:
        return {"drop": f"senior title ({m.group(0).strip()})", "stretch": False, "years": -1, "graduate": False}
    lv = (job.extra or {}).get("experience_level", "")
    if level() == "junior" and re.search(r"senior|lead|manager|director|executive", lv or "", re.I):
        return {"drop": f"experience level: {lv}", "stretch": False, "years": -1, "graduate": False}
    years, top = required_years(job.description), max_years()
    if years > top:
        return {"drop": f"requires {years}+ years", "stretch": False, "years": years, "graduate": False}
    grad = level() == "junior" and (bool(GRADUATE.search(job.title) or GRADUATE.search(job.description[:3000]))
                                    or bool(re.search(r"entry|junior", lv or "", re.I)))
    stretch = (top >= 2 and years >= top - 1 and years > 1) or \
        (level() == "junior" and bool(STRETCH_TITLE.search(job.title or "")))
    return {"drop": "", "stretch": stretch, "years": years, "graduate": grad}


def cheap_drop(job) -> str:
    """The checks that need only the title and the date: run before fetching a description."""
    m = _senior_re().search(job.title or "")
    if m:
        return f"senior title ({m.group(0).strip()})"
    if _not_fulltime(job.title):
        return "not a job type you want (student/intern/part-time/contract in title)"
    return title_check(job.title) or recency_check(job)


# job types the user may accept (settings.yaml search.job_types); full-time is always accepted
TYPE_WORDS = {"part-time": r"part[- ]time|part\b", "contract": r"contract(or)?|freelance|temporary|temp\b",
              "internship": r"intern(ship)?|student|working student|summer|co-op|apprentice"}


def accepted_types() -> set:
    return {str(t).lower() for t in config.search().get("job_types") or ["full-time"]}


def _type_ok(text: str) -> bool:
    """True when every non-full-time word in the text is a job type the user accepts."""
    ok_types = accepted_types()
    for kind, pattern in TYPE_WORDS.items():
        if re.search(pattern, text or "", re.I) and kind not in ok_types:
            return False
    return True


def _not_fulltime(title: str) -> bool:
    return bool(NOT_FULLTIME.search(title or "")) and not _type_ok(title)


def employment_check(job) -> str:
    if _not_fulltime(job.title):
        return "not a job type you want (student/intern/part-time/contract in title)"
    if job.employment and re.search(r"intern|part|temp|contract|freelance|student", job.employment, re.I) \
            and not _type_ok(job.employment):
        return f"not a job type you want ({job.employment})"
    head = (job.description or "")[:600]
    m = re.search(r"\b(student position|part[- ]time position|internship program)\b", head, re.I)
    if m and not _type_ok(m.group(0)):
        return "not a job type you want (description)"
    return ""


def remote_check(job, loc: dict) -> str:
    """A fully remote job when the user does not want remote work (search.include_remote: false)."""
    if config.search().get("include_remote", True):
        return ""
    if re.search(r"\bremote\b", job.location or "", re.I) and not loc.get("city"):
        return "remote job (you asked for on-site or hybrid only)"
    return ""


def recency_check(job, now: datetime = None) -> str:
    if not job.posted:
        return ""
    now = now or datetime.now(timezone.utc)
    try:
        posted = datetime.fromisoformat(job.posted)
    except ValueError:
        return ""
    if now - posted > timedelta(days=MAX_AGE_DAYS):
        return f"posted {(now - posted).days} days ago"
    return ""


SCAM = [
    (r"(registration|application|training|placement|processing)\s+fee|pay (a|the) fee", "asks for a fee"),
    (r"(id|passport|social security|national insurance) (number|copy|scan)", "asks for ID number"),
    (r"contact (us|me)? ?(only )?(via|on|through) (telegram|whatsapp)|telegram only|whatsapp only",
     "contact only on Telegram/WhatsApp"),
    (r"[\w.+-]+@(gmail|yahoo|hotmail|outlook|aol)\.com", "free-mail recruiter address"),
]


def scam_check(job) -> str:
    text = f"{job.title}\n{job.description}"
    for pattern, reason in SCAM:
        if re.search(pattern, text, re.I):
            return f"scam sign: {reason}"
    return ""
