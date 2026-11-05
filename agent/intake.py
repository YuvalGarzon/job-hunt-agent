"""A job the user hands the bot: a post forwarded from a Telegram group, /job <link>, a
bare link, or a screenshot / photo / PDF / Word file of a job post. Runs on your computer (Claude reads
posts that have no usable link, and the text in images).

0. Photos and files: their text is read first (images and scanned PDFs by Claude, PDF and Word
   text directly) and then handled like a pasted post.

1. Links first: a link to a company's ATS posting (Greenhouse, Lever, Ashby, Comeet, Workday)
   is read through that ATS's public API; any other careers page is read if its robots.txt
   allows it. LinkedIn links are never opened; only the bare job link is kept.
2. No usable link: Claude extracts title / company / place from the post, and the posting is
   looked up on the company's own board (known boards, then a Greenhouse/Ashby/Lever search).
3. Not found anywhere: the job is still sent, tagged as not verified, with the post text as its
   description for the CV.
The job is scored like any other and sent with Approve / Skip, also when it fails a filter
(the reason is shown): the user picked it.
"""
import json
import re
import shutil
import subprocess
import time
import urllib.robotparser
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from agent import alerts, claude_cli, config, filters, geo, sources
from agent import telegram as tg
from agent.jobs import Job, html_to_text
from agent.pipeline import evaluate
from agent.sheet import Sheet, now_iso
from agent.sources import ats, bigco, linkedin

URL = re.compile(r"https?://[^\s<>\"')\]]+")
UA = ats.UA
# Job boards are used only through their email alerts (rule 4): their pages are never read;
# a job linked there is taken from the post text instead.
JOB_BOARD = re.compile(r"(^|\.)(indeed|glassdoor|monster|ziprecruiter|wellfound|totaljobs|reed|seek|stepstone|"
                       r"naukri|jobstreet|dice|simplyhired|careerbuilder)\.", re.I)
JOB_PAGE = re.compile(r"requirements|responsibilities|qualifications|what you.ll do|apply", re.I)
POSTS_DIR = config.STATE_DIR / "posts"           # post texts kept for the CV of unverified jobs


def looks_like_posting(text: str) -> bool:
    """A pasted job posting (not a chat line): long, or medium with posting words."""
    t = (text or "").strip()
    return len(t) >= 400 or (len(t) >= 150 and bool(JOB_PAGE.search(t)))


def urls_in(msg: dict) -> list:
    """Every link in a Telegram message, including links hidden behind words (text_link)."""
    text = msg.get("text") or msg.get("caption") or ""
    found = [e["url"] for e in (msg.get("entities") or []) + (msg.get("caption_entities") or [])
             if e.get("type") == "text_link" and e.get("url")]
    found += [u.rstrip(".,;") for u in URL.findall(text)]
    return list(dict.fromkeys(found))


def is_forward(msg: dict) -> bool:
    return any(k in msg for k in ("forward_origin", "forward_from_chat", "forward_from", "forward_sender_name"))


# --- 0. photos and files -----------------------------------------------------------------
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
DOC_TYPES = {"application/pdf": ".pdf", "text/plain": ".txt",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}
EXT_TYPES = {v: k for k, v in {**IMAGE_TYPES, **DOC_TYPES}.items()} | {".jpeg": "image/jpeg", ".heic": "image/heic"}
# what the bot cannot read, with the name used in the reply
UNREADABLE = {"voice": "a voice message", "audio": "an audio file", "video": "a video", "video_note": "a video",
              "animation": "a GIF", "sticker": "a sticker", "contact": "a contact", "location": "a location",
              "venue": "a location", "poll": "a poll", "dice": "a dice", "story": "a story"}
MEDIA_DIR = config.STATE_DIR / "media"            # each task's files, deleted when it ends
MAX_PDF_PAGES = 6


def media_in(msg: dict) -> list:
    """The photos and files in a message that can be read: [{"file_id", "name", "mime", "size"}].
    Photos come in several sizes; the largest is kept."""
    if msg.get("photo"):
        big = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        return [{"file_id": big["file_id"], "name": "photo.jpg", "mime": "image/jpeg", "size": big.get("file_size", 0)}]
    d = msg.get("document")
    if d:
        name = d.get("file_name") or "file"
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        mime = d.get("mime_type") or EXT_TYPES.get(ext, "")
        if mime in IMAGE_TYPES or mime in DOC_TYPES or mime == "image/heic":
            return [{"file_id": d["file_id"], "name": name, "mime": mime, "size": d.get("file_size", 0)}]
    return []


def unreadable_kind(msg: dict) -> str:
    """What was sent when the bot cannot read it ("" when it is text, a photo or a readable file)."""
    if msg.get("document") and not media_in(msg):
        return "a file of type " + ((msg["document"].get("file_name") or "").rsplit(".", 1)[-1] or "unknown")
    return next((name for k, name in UNREADABLE.items() if k in msg), "")


READ_IMAGES = """The files {names} in the current folder are screenshots or photos of a job post (one post, in
order). Open each one with the Read tool and copy all the text in them exactly as written, in the
original language, keeping the line breaks. Include every link or web address you can see.
Answer with the copied text only. If there is no text in them, answer with nothing."""


def _pdf_text(data: bytes, folder: Path) -> tuple:
    """(text, page images): a PDF with real text is read directly; a scanned one becomes images."""
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc.pages(0, min(len(doc), MAX_PDF_PAGES)))
    if len(text.strip()) > 40:
        return text, []
    images = []
    for i, page in enumerate(doc.pages(0, min(len(doc), MAX_PDF_PAGES))):
        f = folder / f"page_{i + 1}.png"
        page.get_pixmap(dpi=150).save(f)
        images.append(f)
    return "", images


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return html_to_text(re.sub(r"<[^>]+>", "", xml)) if "<" in xml else xml


def read_media(items: list, key: str) -> str:
    """The text of the photos and files the user sent, in the order sent."""
    folder = MEDIA_DIR / re.sub(r"[^\w-]", "_", key)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    parts, images = [], []
    try:
        for n, it in enumerate(items, 1):
            if it.get("size", 0) > tg.MAX_FILE:
                parts.append(f"[{it['name']}: too big to read]")
                continue
            data = tg.get_file(it["file_id"])
            mime = it["mime"]
            if mime == "image/heic":                       # iPhone photos: Claude reads PNG/JPEG
                src, f = folder / f"src_{n}.heic", folder / f"image_{n}.png"
                src.write_bytes(data)
                if shutil.which("sips"):                   # macOS converts them; elsewhere they are skipped
                    subprocess.run(["sips", "-s", "format", "png", str(src), "--out", str(f)], capture_output=True,
                                   timeout=60)
                src.unlink()
                if f.exists():
                    images.append(f)
                else:
                    parts.append(f"[{it['name']}: could not open this HEIC photo; send it as a screenshot or JPEG]")
            elif mime in IMAGE_TYPES:
                f = folder / f"image_{n}{IMAGE_TYPES[mime]}"
                f.write_bytes(data)
                images.append(f)
            elif mime == "application/pdf":
                text, pages = _pdf_text(data, folder)
                parts.append(text)
                images += pages
            elif mime == "text/plain":
                parts.append(data.decode("utf-8", "replace"))
            else:
                parts.append(_docx_text(data))
        if images:
            names = ", ".join(f.name for f in images)
            parts.append(claude_cli.ask(READ_IMAGES.replace("{names}", names), files_dir=folder, timeout=300))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


# --- 1. links ----------------------------------------------------------------------------
def _company_for(kind: str, board: str) -> str:
    for c in sources.companies():
        if c["ats"] == kind and c["board"] == board:
            return c["name"]
    return ""


def _job(kind: str, board: str, ref: str, one: dict, company: str) -> Job:
    return Job(source=kind, source_ref=f"{kind}:{board}:{ref}", company=company or board.split("/")[0].split(".")[0].title(),
               title=one["title"].strip(), location=one.get("location", ""), url=one["url"],
               id_url=one.get("id_url", ""), description=one["description"])


def resolve_ats(url: str):
    """The posting behind an ATS link, read through the ATS's public API; None if not an ATS link."""
    p = urlsplit(url)
    host, parts = p.netloc.lower().removeprefix("www."), [x for x in p.path.split("/") if x]
    q = parse_qs(p.query)
    try:
        if host.endswith("greenhouse.io") and len(parts) >= 3 and parts[-2] == "jobs":
            board, jid = parts[-3], parts[-1]
            one = ats.greenhouse_one(board, jid)
            return one and _job("greenhouse", board, jid, one, _company_for("greenhouse", board))
        if host.endswith("greenhouse.io") and q.get("for") and q.get("token"):   # Greenhouse's embed job page
            board, jid = q["for"][0], q["token"][0]
            one = ats.greenhouse_one(board, jid)
            return one and _job("greenhouse", board, jid, one, _company_for("greenhouse", board))
        if "gh_jid" in q:                                      # a company site embedding Greenhouse
            for c in sources.companies():
                if c["ats"] == "greenhouse" and c["board"] in host:
                    one = ats.greenhouse_one(c["board"], q["gh_jid"][0])
                    if one:
                        return _job("greenhouse", c["board"], q["gh_jid"][0], one, c["name"])
        if host == "jobs.lever.co" and len(parts) >= 2:
            one = ats.lever_one(parts[0], parts[1])
            return one and _job("lever", parts[0], parts[1], one, _company_for("lever", parts[0]))
        if host == "jobs.ashbyhq.com" and len(parts) >= 2:
            one = ats.ashby_one(parts[0], parts[1])
            return one and _job("ashby", parts[0], parts[1], one, _company_for("ashby", parts[0]))
        if host == "comeet.com" and len(parts) >= 5 and parts[0] == "jobs":
            board = f"{parts[1]}/{parts[2]}"
            one = ats.comeet_one(board, parts[4])
            return one and _job("comeet", board, parts[4], one, _company_for("comeet", board))
        m = re.match(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
        if m and "job" in parts:
            i = parts.index("job")
            site = parts[i - 1]
            board, path = f"{m.group(1)}.{m.group(2)}/{m.group(1)}/{site}", "/" + "/".join(parts[i:])
            one = bigco.workday_one(board, path)
            return one and _job("workday", board, path, one, _company_for("workday", board))
    except requests.RequestException:
        return None
    return None


def resolve_page(url: str):
    """Any other careers page, read only if its robots.txt allows it. None if it is not a job page."""
    p = urlsplit(url)
    host = p.netloc.lower().removeprefix("www.")
    if not host or "linkedin.com" in host or host in ("t.me", "telegram.me") or JOB_BOARD.search(host):
        return None
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers=UA, timeout=10)
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        if not rp.can_fetch(UA["User-Agent"], url):
            return None
        page = requests.get(url, headers=UA, timeout=15)
    except requests.RequestException:
        return None
    if page.status_code != 200 or "html" not in page.headers.get("content-type", ""):
        return None
    h = page.text

    def meta(prop):
        m = re.search(r'<meta[^>]+(?:property|name)=["\']' + prop + r'["\'][^>]+content=["\']([^"\']+)', h, re.I)
        return html_to_text(m.group(1)) if m else ""

    title = meta("og:title") or html_to_text((re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S) or [None, ""])[1])
    body = html_to_text(re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", h))
    if not title or len(body) < 400 or not JOB_PAGE.search(body):
        return None
    parts = host.split(".")
    second_level = len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac")
    company = re.sub(r"\s*(careers?|jobs)$", "", meta("og:site_name"), flags=re.I) or \
        parts[-3 if second_level else -2].title()
    return Job(source="web", source_ref=f"web:page:{url}", company=company, title=title.split("|")[0].split(" - ")[0].strip(),
               location="", url=page.url, description=body[:20000])


# --- 2. no usable link: Claude reads the post --------------------------------------------
EXTRACT = """List every job offered in this post. Answer with JSON only, no other text:
{"jobs": [{"title": "...", "company": "...", "location": "...", "link": "..."}]}
Use "" for anything the post does not say. Copy titles and company names exactly as written.
"link": the link from LINKS that belongs to that job, only when that is clear; else "".
If the post offers no job, answer {"jobs": []}.

LINKS (in the order they appear in the post):
{links}

POST:
{post}"""
MAX_JOBS = 10


def extract(text: str, urls: list) -> list:
    out = claude_cli.ask(EXTRACT.replace("{links}", "\n".join(urls) or "(none)").replace("{post}", text[:6000]))
    m = re.search(r"\{.*\}", out, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    jobs = []
    for j in (data.get("jobs") or [])[:MAX_JOBS]:
        j = {k: str(j.get(k) or "").strip() for k in ("title", "company", "location", "link")}
        if j["title"]:
            jobs.append(j)
    return jobs


def find_on_board(title: str, company: str, sheet: Sheet):
    alert = linkedin.AlertJob(title=title, company=company, location="", li_id="")
    key = linkedin.company_key(company)
    boards = sources.companies() + linkedin.boards(sheet)
    for c in boards:
        if linkedin.company_key(c["name"]) == key:
            try:
                return linkedin.find_in(alert, sources.FETCHERS[c["ats"]](c))
            except Exception:
                return None
    res, jobs = linkedin.discover(company, [title])
    if res:
        sheet.t("Boards").append({"Company": linkedin.short_company(company), "ATS": res["ats"], "Board": res["board"],
                                  "Status": "found", "Checked": now_iso()})
        return linkedin.find_in(alert, jobs)
    return None


# --- the task ----------------------------------------------------------------------------
def collect(text: str, urls: list, sheet: Sheet) -> list:
    """The jobs in a post: every link that resolves to a posting, plus (when the post says more
    than its links) the jobs Claude finds in the text, each looked up on its company's board."""
    jobs, seen = [], set()

    def add(j):
        if j and j.job_id not in seen and len(jobs) < MAX_JOBS:
            seen.add(j.job_id)
            jobs.append(j)

    plain = [u for u in urls if "linkedin.com" not in u]
    for u in plain[:MAX_JOBS]:
        add(resolve_ats(u) or resolve_page(u))
    if (plain and len(jobs) >= len(plain)) or not URL.sub("", text).strip():
        return jobs
    titles = {j.title.lower() for j in jobs}
    for info in extract(text, urls):
        if info["title"].lower() in titles:
            continue
        j = None
        if info["link"] and "linkedin.com" not in info["link"]:
            j = resolve_ats(info["link"]) or resolve_page(info["link"])
        if j is None and info["company"]:
            j = find_on_board(info["title"], info["company"], sheet)
        if j is None:
            link = info["link"] or (urls[0] if len(urls) == 1 else "")
            li = re.search(r"linkedin\.com/.*?/jobs/view/(\d+)|linkedin\.com/jobs/view/(\d+)", link)
            if li:
                link = f"https://www.linkedin.com/jobs/view/{li.group(1) or li.group(2)}/"
            j = Job(source="telegram", source_ref="", company=info["company"] or "?", title=info["title"],
                    location=info["location"], url=link, description=text[:6000])
        add(j)
    return jobs


def send_job(sheet: Sheet, job: Job, post: str, n: int, total: int) -> int:
    if job.extra.get("detail"):
        bigco.DETAIL[job.source](job)
    loc = geo.locate(job.location, job.country, {}, budget=[1])
    tiers = sources.company_tiers()
    row, reason = evaluate(job, loc, tiers)
    if reason and "Score" not in row:                          # scored anyway: the user picked it
        from agent.score import fit_lines, score
        sen = filters.seniority(job)
        sc = score(job, sen, loc, tiers)
        line1, line2, gaps = fit_lines(job, sc, sen)
        row.update({"Score": sc["score"], "Score parts": " ".join(f"{k}={v}" for k, v in sc["parts"].items()),
                    "Fit": f"{line1}\n{line2}", "Gaps": gaps})
    if job.source == "telegram":
        POSTS_DIR.mkdir(parents=True, exist_ok=True)
        (POSTS_DIR / f"{job.job_id}.txt").write_text(post, encoding="utf-8")
    jobs = sheet.t("Jobs")
    existing = jobs.find("Job ID", job.job_id)
    if existing:
        row = existing
    else:
        jobs.append({**row, "Decision": "forwarded", "Reason": reason or "sent by you", "Rank": n})
        sheet.flush()
        row = jobs.find("Job ID", job.job_id)
    head = "📥 <b>The job you sent</b>" + (f" ({n}/{total})" if total > 1 else "") + \
        (" · I've seen it before" if existing else "")
    warn = f"\n⚠️ It doesn't pass the usual filters: {tg.esc(reason)}" if reason else ""
    done = row.get("User action") in ("approved", "skipped")
    msg = tg.send_message(f"{head}\n{alerts.job_text(row, n)}{warn}",
                          buttons=tg.link_row(job.url) if done else alerts.card_buttons(row))
    jobs.update(row, **{"Alert msg ID": msg["message_id"], "Updated": now_iso()})
    sheet.flush()
    return msg["message_id"]


ALBUM_WAIT = 4        # seconds: the other photos of an album arrive as separate messages


def run_task(sheet: Sheet, task: dict, payload: dict, log) -> str:
    sheet.load("Jobs", "Boards", "Settings")
    done = json.loads(task["Result"]) if task.get("Result", "").startswith("{") else {}
    if payload.get("group") and not done:                     # let the rest of the album join
        time.sleep(ALBUM_WAIT)
        sheet.load("Queue")
        row = sheet.t("Queue").find("Task ID", task["Task ID"])
        if row and row.get("Payload", "").startswith("{"):
            payload = json.loads(row["Payload"])
    text, urls = payload.get("text", ""), payload.get("urls", [])
    if payload.get("media"):
        if "media_text" not in done:
            done["media_text"] = read_media(payload["media"], task["Task ID"])
            row = sheet.t("Queue").find("Task ID", task["Task ID"])
            if row:
                sheet.t("Queue").update(row, Result=json.dumps(done, ensure_ascii=False))
                sheet.flush()
        text = "\n\n".join(t for t in (text, done["media_text"]) if t)
        urls = list(dict.fromkeys(urls + [u.rstrip(".,;") for u in URL.findall(done["media_text"])]))
        log(f"intake: read {len(payload['media'])} file(s), {len(done['media_text'])} chars")
    if payload.get("for_job"):
        return attach_posting(sheet, payload["for_job"], text, log)
    jobs = collect(text, urls, sheet)
    if not jobs and payload.get("media") and done.get("media_text"):
        # a screenshot that is not a job (an application form, an email, a question): the chat
        # answers it, with the user's words and the image's text
        from agent import writing
        said = payload.get("text", "").strip()
        writing.chat(sheet, task, {"text": (f"{said}\n\n" if said else "") +
                                   f"[THE IMAGE THE USER SENT, as the agent read it:]\n{done['media_text'][:4000]}",
                                   "job_id": payload.get("job_id", "")}, log)
        return "no job: answered in the chat"
    if not jobs:
        if payload.get("media") and not done.get("media_text"):
            tg.send_message("🤔 I couldn't read any text in the image/file. Try a sharper screenshot, "
                            "or copy the job's text and send it.")
        elif urls and all("linkedin.com" in u for u in urls) and not URL.sub("", text).strip():
            tg.send_message("🔗 I don't open LinkedIn pages. Send a screenshot of the job or its text, "
                            "and I'll check it from that.")
        else:
            tg.send_message("🤔 I didn't find a job in this message. If it has a link, the site may not allow "
                            "automatic reading. Send a screenshot or the job's text and I'll check it from that.")
        return "no job"
    sent = done.get("sent", {})
    if payload.get("media") and not sent:                     # now it is known to be a job
        names = "\n".join(f"• {tg.esc(j.title)} · {tg.esc(j.company)}" for j in jobs)
        tg.send_message(f"💼 It's a job:\n{names}\nChecking it…" if len(jobs) == 1
                        else f"💼 I found {len(jobs)} jobs:\n{names}\nChecking them…")
    q = sheet.t("Queue")
    for n, job in enumerate(jobs, 1):
        if job.job_id in sent:                                 # resumed task: never send twice
            continue
        sent[job.job_id] = send_job(sheet, job, text, n, len(jobs))
        if payload.get("approve") and n == 1:                  # /cv <link>: straight to a tailored CV
            from agent import bot
            sheet.load("Jobs", "Applications", "Queue")
            bot.on_approve(sheet, job.job_id, sent[job.job_id], {"mac_online": True})
        row = q.find("Task ID", task["Task ID"])
        if row:
            q.update(row, Result=json.dumps({**done, "sent": sent}, ensure_ascii=False))
            sheet.flush()
    return json.dumps({**done, "sent": sent}, ensure_ascii=False)


def attach_posting(sheet: Sheet, job_id: str, text: str, log) -> str:
    """The user replied to a job with its posting (the agent could not read it, e.g. LinkedIn): keep
    it as the job's description, score the job again with it, and build the CV from it."""
    from agent import bot
    from agent.score import fit_lines, score
    sheet.load("Jobs", "Applications", "Queue")
    jobs = sheet.t("Jobs")
    row = jobs.find("Job ID", job_id)
    if not row:
        tg.send_message("I couldn't find that job in the Sheet.")
        return "no job"
    if len(text.strip()) < 80:
        tg.send_message("🤔 I couldn't read enough of the job's text. Try pasting the text itself or a sharper screenshot.")
        return "too short"
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    (POSTS_DIR / f"{job_id}.txt").write_text(text, encoding="utf-8")

    job = Job(source=row["Source"], source_ref=row["Source ref"], company=row["Company"], title=row["Title"],
              location=row["Location"], url=row["URL"], description=text)
    loc = geo.locate(job.location, "", {}, budget=[1])
    tiers = sources.company_tiers()
    new, reason = evaluate(job, loc, tiers)
    if "Score" not in new:                                     # scored anyway: the user picked it
        sen = filters.seniority(job)
        sc = score(job, sen, loc, tiers)
        line1, line2, gaps = fit_lines(job, sc, sen)
        new.update({"Score": sc["score"], "Score parts": " ".join(f"{k}={v}" for k, v in sc["parts"].items()),
                    "Fit": f"{line1}\n{line2}", "Gaps": gaps})
    old = row["Score"]
    jobs.update(row, **{k: new[k] for k in ("Score", "Score parts", "Fit", "Gaps")}, Updated=now_iso())
    sheet.flush()
    fit = (new["Fit"] or "").split("\n")[0]
    lines = [f"📝 <b>Saved the job description</b>: {tg.esc(row['Title'])} · {tg.esc(row['Company'])}",
             f"⭐ Fit score: {tg.esc(old or '—')} → <b>{new['Score']}</b>"]
    if fit:
        lines.append(f"✅ {tg.esc(fit)}")
    if new.get("Gaps"):
        lines.append(f"⚠️ Gaps: {tg.esc(new['Gaps'])}")
    if reason:
        lines.append(f"⚠️ It doesn't pass the usual filters: {tg.esc(reason)}")

    app = sheet.t("Applications").find("Job ID", job_id)
    builds = [t for t in sheet.t("Queue").rows if t["Type"] == "build_cv" and app and t["App ID"] == app["App ID"]]
    if not app:
        tg.send_message("\n".join(lines))
        bot.on_approve(sheet, job_id, None, {"mac_online": True})   # builds the CV from this text
        return "approved"
    if app["Status"] in ("Applied", "Assessment", "Interview", "Offer", "Rejected", "No response"):
        lines.append("You already applied to this job, so I didn't build a new CV. The description is saved (for interview prep too).")
    elif any(t["Status"] == "pending" for t in builds):
        lines.append("The CV in the queue will be built from this description.")
    else:
        bot.queue_task(sheet, "build_cv", job_id, app["App ID"], {"mode": "posting"},
                       bot._next_cv_task_id(sheet, app["App ID"]))
        if app["Status"] in ("Withdrawn", "Closed"):
            sheet.t("Applications").update(app, Status="Approved", **{"Last update": now_iso()})
        sheet.flush()
        lines.append("📄 Building a new CV from the full description…")
    tg.send_message("\n".join(lines))
    return "attached"


def post_text(job_id: str) -> str:
    f = POSTS_DIR / f"{job_id}.txt"
    return f.read_text(encoding="utf-8") if f.exists() else ""
