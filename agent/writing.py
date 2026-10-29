"""Writing and prep (step 7), all on the Mac through Claude: cover letters, interview prep,
mock interviews and free chat. Everything written about the user comes from PROFILE.yaml only
and passes truth_check.check_text before it is shown. The user sends everything themselves.
"""
import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from agent import claude_cli, config
from agent import telegram as tg
from agent.sheet import TZ, Sheet, now_iso
from agent.truth_check import check_text

CHAT_LOG = config.STATE_DIR / "chat.jsonl"
CHAT_TURNS = 12
MOCK_HOURS = 3
MAX_WORDS = 200


def send_long(text: str, buttons: list = None, pre: bool = False) -> int:
    """Telegram allows 4096 characters per message: split on paragraphs; buttons on the last part."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) > 3500 and cur:
            parts.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + para
    parts.append(cur)
    msg = None
    for i, part in enumerate(parts):
        body = f"<pre>{tg.esc(part)}</pre>" if pre else part
        msg = tg.send_message(body, buttons=buttons if i == len(parts) - 1 else None)
    return msg["message_id"] if msg else 0


def _profile_for_writing() -> str:
    p = dict(config.profile())
    p.pop("private_never_on_cv", None)                 # never handed to the writer
    return yaml.safe_dump(p, allow_unicode=True, sort_keys=False)


def _app_and_job(sheet: Sheet, app_id: str) -> tuple:
    from agent.tasks import _job_from_sheet
    sheet.load("Applications", "Jobs")
    app = sheet.t("Applications").find("App ID", app_id)
    if not app:
        raise RuntimeError("application not found")
    try:
        job = _job_from_sheet(sheet, app["Job ID"])
    except RuntimeError:
        job = {"company": app["Company"], "title": app["Role"], "description": ""}
    return app, job


def _job_folder(app: dict) -> Path:
    if not app.get("CV path"):
        return None
    d = Path(app["CV path"]).parent
    return d if d.is_absolute() else config.ROOT / d


# --- cover letters -----------------------------------------------------------------------
COVER = """Write a cover letter from {name} for this job.

Rules:
- Under 200 words. Formal and professional but natural; it must not read like a template or
  like AI. Specific to this job and this company: connect 2-3 true things from the profile to
  what the posting asks for, using the posting's own terms where they truthfully match.
- Use ONLY facts from PROFILE. Choose, order, shorten and rephrase; never add a tool, number,
  responsibility or claim. Obey PROFILE.writing_rules exactly (banned words, forbidden words,
  never-rules), and never write anything an experience's never_write lists.
- Language: {language}. Greeting "Dear {company} Hiring Team," (or the equivalent in that
  language), and sign "{name}".
{extra}
Answer with the letter text only.

JOB: {title} at {company}
POSTING:
{description}

PROFILE:
{profile}"""


def cover_letter(sheet: Sheet, task: dict, payload: dict, log) -> str:
    app, job = _app_and_job(sheet, task["App ID"])
    from agent.bot import cover_languages
    langs = cover_languages()
    try:
        lang = langs[int(payload.get("lang", 0))]
    except (ValueError, IndexError):
        lang = langs[0]
    folder = _job_folder(app)
    previous = (folder / "Cover Letter.txt") if folder else None
    extra = ""
    if payload.get("mode") == "fix" and previous and previous.exists():
        extra = (f"\nThis is a revision. The user asked, in their own words: \"{payload.get('feedback', '')}\"\n"
                 f"Change only what they asked; keep the rest. Previous version:\n{previous.read_text(encoding='utf-8')}\n")
    elif payload.get("mode") == "retry":
        extra = "\nThe user asked for a different version: choose different true points and different wording.\n"
    prompt = COVER.format(name=config.name(), language=lang, company=job["company"], title=job["title"], extra=extra,
                          description=(job.get("description") or "")[:8000], profile=_profile_for_writing())
    text, problems = "", []
    for _ in range(3):
        text = claude_cli.ask(prompt + (f"\n\nYour previous draft broke these rules, fix them: {problems}" if problems else "")).strip()
        problems = check_text(text, job.get("description", ""))
        words = len(text.split())
        if words > MAX_WORDS:
            problems.append(f"{words} words: shorten to under {MAX_WORDS} (drop the weakest point, keep the facts exact)")
        if not problems:
            break
    if problems:
        raise RuntimeError(f"cover letter failed the checks: {problems}")
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
        if previous.exists():
            v = folder / "_versions" / datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S")
            v.mkdir(parents=True, exist_ok=True)
            shutil.move(str(previous), v / "Cover Letter.txt")
        previous.write_text(text + "\n", encoding="utf-8")
    send_long(f"📝 Cover letter · {tg.esc(app['Company'])} · {tg.esc(app['Role'])}", None)
    send_long(text, buttons=cover_buttons(app["App ID"], str(payload.get("lang", 0))), pre=True)
    return "sent"


def cover_buttons(app_id: str, lang: str) -> list:
    return [[("✅ Good", f"clok:{app_id}"), ("🔁 Try again", f"clre:{lang}:{app_id}")],
            [("✏️ Fix", f"clfx:{lang}:{app_id}")]]


# --- interview prep and mock interview ---------------------------------------------------
PREP = """Prepare {first} for an interview at {company}{role_part}. Write in English. Sections, with
short headings:
1. Company brief (5-7 lines): what they do, products, size, their office near {country}, recent
   news. Search the web for this; never open linkedin.com. Say when something could not be verified.
2. Likely questions for this role: 4 algorithms / LeetCode-style problems (name the pattern and
   a one-line approach), 4 ML theory questions if the role touches ML/data (else 4 core CS
   questions), 2 system-design prompts sized for a {level} engineer, 4 behavioral questions.
3. {first}'s stories: the 3 projects or experiences from PROFILE that fit this role best, each
   with a 3-line STAR outline using only facts from PROFILE (never write anything an
   experience's never_write lists; never invent numbers).
4. Three questions {first} can ask them.
5. What to learn or sharpen before the interview: compare the posting with PROFILE. For each
   requirement {first} lacks or has only used lightly (at most 6, the most important first):
   what it is in one line, why this role needs it, and a concrete way to close the gap in the
   time left (a free official tutorial or docs page by name, a small hands-on exercise, the 3-5
   concepts to be able to explain). Then the skills {first} already has that this role leans
   on, to review so they can be explained in depth. End with a short day-by-day plan for the
   next 3-5 days. Never suggest claiming experience that is not in PROFILE; say how to answer
   honestly when asked about a gap ("I haven't used X in production; here is what I know and
   how I would learn it").
Plain text, no tables.

POSTING:
{description}

PROFILE:
{profile}"""


def prep(sheet: Sheet, task: dict, payload: dict, log) -> str:
    company = payload.get("company", "").strip()
    sheet.load("Applications")
    app = None
    if task.get("App ID"):
        app = sheet.t("Applications").find("App ID", task["App ID"])
    elif company:
        from agent.sources.linkedin import company_key
        k = company_key(company)
        hits = [a for a in sheet.t("Applications").rows if company_key(a["Company"]) == k]
        app = hits[-1] if hits else None
    job = _app_and_job(sheet, app["App ID"])[1] if app else {"company": company, "title": "", "description": ""}
    tg.send_message(f"📚 Preparing interview prep for <b>{tg.esc(job['company'])}</b>… (a few minutes)")
    from agent import country, filters
    text = claude_cli.ask(PREP.format(first=config.first_name(), country=country.name(), level=filters.level(),
                                      company=job["company"], role_part=f" for the role \"{job['title']}\"" if job["title"] else "",
                                      description=(job.get("description") or "(no posting available)")[:8000],
                                      profile=_profile_for_writing()), allow_web=True, timeout=1200)
    folder = _job_folder(app) if app else None
    if folder:
        (folder / "Interview Prep.md").write_text(text, encoding="utf-8")
    key = app["App ID"] if app else re.sub(r"\W+", "_", company)[:30]
    send_long(tg.esc(text), buttons=[[("🎤 Mock interview", f"mock:{key}")]])
    return "sent"


def start_mock(sheet: Sheet, key: str) -> None:
    sheet.kv_set("Settings", "mock", f"{key}|{now_iso()}")
    _log_turn("system", f"MOCK INTERVIEW STARTED for {key}")


def mock_active(sheet: Sheet) -> str:
    key, _, since = sheet.kv_get("Settings", "mock", "").partition("|")
    try:
        if key and datetime.now(TZ) - datetime.fromisoformat(since) < timedelta(hours=MOCK_HOURS):
            return key
    except ValueError:
        pass
    return ""


# --- free chat ---------------------------------------------------------------------------
CHAT = """You are {first}'s personal job-search assistant, chatting on Telegram. Answer in English,
short and direct, like a smart friend who knows this search. You may help with anything in the
search: advice, which jobs to prioritize, explaining a posting, drafting a message to a
recruiter or a LinkedIn note (drafts only, {first} sends them). Anything written about {first}
must use only facts from PROFILE and obey PROFILE.writing_rules; never invent experience. Never
reveal anything from private_never_on_cv. If you do not know something, say so.
ACTIONS: the chat can show real job cards with Approve/Skip buttons. If {first} asks to see or get
the jobs not handled yet (open / pending / not approved / not skipped), do NOT write a list:
answer with one short line and add the token [[PENDING_JOBS]]. If {first} asks for the CVs that
are ready / waiting to apply / not marked applied, add [[WAITING_CVS]]. You may add both. Never
write those tokens otherwise.
JOB ACTIONS: when {first} says something that changes a job's state, add ONE token for it, with
the job's id from THE JOB or THE SEARCH NOW (never guess an id; if unsure which job, ask):
[[ACT:applied:<job id>]] applied / sent the CV; [[ACT:closed:<job id>]] the job is closed or
taken; [[ACT:skip:<job id>]] not interested; [[ACT:approve:<job id>]] wants a tailored CV;
[[ACT:card:<job id>]] wants to see the job again. A token only shows a button to confirm; say
in your answer to tap it.
{mode}
PROFILE:
{profile}

THE SEARCH NOW:
{state}
{job}{replied}
RECENT CONVERSATION:
{history}

{first}'S MESSAGE:
{text}"""

MOCK = """MOCK INTERVIEW MODE: you are the interviewer for {target}. Ask ONE question at a time.
After each answer: 2-3 lines of honest feedback (what was good, what to improve, a model answer
point), then the next question. Mix technical (algorithms, ML if relevant, system design for a
{level} engineer) and behavioral. If the answer is "stop", end with a short summary of strengths
and what to practice."""


def _log_turn(role: str, text: str) -> None:
    CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CHAT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now_iso(), "role": role, "text": text[:3000]}, ensure_ascii=False) + "\n")


def _history() -> str:
    if not CHAT_LOG.exists():
        return "(none)"
    lines = CHAT_LOG.read_text(encoding="utf-8").splitlines()[-CHAT_TURNS:]
    return "\n".join(f"{json.loads(l)['role']}: {json.loads(l)['text']}" for l in lines)


def _state(sheet: Sheet) -> str:
    sheet.load("Applications", "Jobs")
    apps = [f"- {a['Company']} · {a['Role']} · {a['Status']} · applied {a['Date applied'] or '-'} · job id {a['Job ID']}"
            for a in sheet.t("Applications").rows if a["Status"] != "Withdrawn"]
    alerted = [r for r in sheet.t("Jobs").rows if r["Decision"] in ("alerted", "forwarded")][-15:]
    jobs = [f"- {r['Company']} · {r['Title']} · score {r['Score']} · {r['User action'] or 'no action'}"
            f"{' · closed' if r.get('Closed') else ''} · job id {r['Job ID']}" for r in alerted]
    return "Applications:\n" + ("\n".join(apps) or "none") + "\nRecent job alerts:\n" + "\n".join(jobs)


ACT_LABEL = {"applied": "📨 I applied", "closed": "🔒 It's closed", "skip": "⏭ Skip", "approve": "✅ Approve (CV)"}


def _act_buttons(sheet: Sheet, acts: list) -> tuple:
    """Chat tokens -> confirm buttons (rows) and job cards to show. Unknown job ids are ignored."""
    sheet.load("Jobs", "Applications")
    rows, cards = [], []
    for kind, job_id in acts[:3]:
        job = sheet.t("Jobs").find("Job ID", job_id)
        if not job:
            continue
        if kind == "card":
            cards.append(job)
            continue
        app = sheet.t("Applications").find("Job ID", job_id)
        data = {"applied": f"applied:{app['App ID']}" if app else f"japplied:{job_id}",
                "closed": f"closed:{job_id}", "skip": f"sk:{job_id}", "approve": f"ap:{job_id}"}[kind]
        rows.append([(f"{ACT_LABEL[kind]}: {job['Company'][:20]}", data)])
    return rows, cards


def chat(sheet: Sheet, task: dict, payload: dict, log) -> str:
    text = payload.get("text", "")
    from agent import filters
    if re.fullmatch(r"\s*(stop|end|finish)\s*", text, re.I) and mock_active(sheet):
        mode = MOCK.format(target=mock_active(sheet), level=filters.level()) + "\nThe user asked to stop now: give the summary."
        sheet.kv_set("Settings", "mock", "")
    elif mock_active(sheet):
        mode = MOCK.format(target=mock_active(sheet), level=filters.level())
    else:
        mode = ""
    job = ""
    if payload.get("job_id"):
        sheet.load("Jobs")
        r = sheet.t("Jobs").find("Job ID", payload["job_id"])
        if r:
            job = (f"THE JOB (the message is about it; job id {r['Job ID']}): {r['Title']} at {r['Company']} "
                   f"({r['URL']}). Fit: {r['Fit']}. Gaps: {r['Gaps']}\n")
    replied = ""
    if payload.get("replied_text"):
        replied = f"THIS IS A REPLY TO A MESSAGE YOU SENT EARLIER:\n{payload['replied_text'][:1500]}\n"
    tg.send_chat_action("typing")
    answer = claude_cli.ask(CHAT.format(first=config.first_name(), mode=mode, profile=_profile_for_writing(),
                                        state=_state(sheet), job=job, replied=replied,
                                        history=_history(), text=text[:4000])).strip()
    _log_turn("user", text)
    _log_turn("assistant", answer)
    buttons = [[("⏹ End interview", "mockend:")]] if mock_active(sheet) else None
    actions = re.findall(r"\[\[(PENDING_JOBS|WAITING_CVS)\]\]", answer)
    answer = re.sub(r"\s*\[\[(PENDING_JOBS|WAITING_CVS)\]\]", "", answer).strip()
    acts = re.findall(r"\[\[ACT:(applied|closed|skip|approve|card):([\w-]+)\]\]", answer)
    answer = re.sub(r"\s*\[\[ACT:[^\]]*\]\]", "", answer).strip()
    act_rows, cards = _act_buttons(sheet, acts)
    if act_rows:
        buttons = (buttons or []) + act_rows
    if answer:
        send_long(tg.esc(answer), buttons=buttons)
    elif act_rows:
        tg.send_message("👇", buttons=act_rows)
    if cards:
        from agent.bot import send_job_card
        for c in cards:
            send_job_card(sheet, c)
    from agent import alerts
    sheet.load("Jobs", "Applications")
    if "PENDING_JOBS" in actions:
        alerts.send_pending(sheet)
    if "WAITING_CVS" in actions:
        alerts.send_waiting_cvs(sheet)
    return "answered" + (f" + {','.join(actions)}" if actions else "")
