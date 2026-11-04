"""Build a CV tailored to one job: Claude tailors a copy of the base, code checks it.

Pipeline (each stage must pass before the next):
  1. Claude (headless, pinned model) tailors base_cv.yaml to the posting, from PROFILE.yaml only.
  2. Truth check (agent.truth_check). Problems go back to Claude to fix; never shipped.
  3. render_cv.py. Exit 2 (over one page) -> Claude shortens: fewer bullets, then drop
     Also built, then tighter wording. Never smaller font or margins.
  4. Link check (inside render_cv.py). Exit 3 -> wrong link goes back to Claude; GitHub not
     reachable -> LinkCheckPending (retry later, tell the user). Never bypassed.
  5. Rasterize and inspect: one page, nothing outside the margins, every line present.
  6. Save under CVs/<Role> CV/<Company> - <Title> - <date>/, older versions in _versions/.
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent import config, truth_check
from agent.claude_cli import ask, extract_block

MAX_FIX_ROUNDS = 4
LEAD_MAX = 105                  # the bold opening line: one line on the page
TRIM_MAX_LINES = 2              # this many lines over one page: the code cuts them, no model call

ROLE_RULES = [
    (r"computer vision|\bcv\b|perception|image processing|imaging|vision", "Computer Vision Engineer"),
    (r"algorithm", "Algorithm Developer"),
    (r"data engineer|analytics engineer|big data|data platform|\betl\b|lakehouse", "Data Engineer"),
    (r"data scien|data analy|analytics|applied scien|research scien|machine learning scien", "Data Scientist"),
    (r"\bml\b|machine learning|mlops|deep learning|\bnlp\b", "ML Engineer"),
    (r"\bai\b|genai|\bllm\b|agent|artificial intelligence", "AI Engineer"),
    (r"embedded|firmware|rtos|low.level|bsp|kernel", "Embedded Software Engineer"),
    (r"devops|\bsre\b|reliability|platform engineer|infrastructure", "DevOps Engineer"),
    (r"\bqa\b|test automation|automation engineer|quality assurance|tester", "QA Automation Engineer"),
    (r"full.?stack|front.?end|frontend", "Full Stack Developer"),
    (r"back.?end|backend|server", "Backend Engineer"),
    (r"mobile|\bios\b|android", "Mobile Developer"),
]


class CVBuildError(RuntimeError):
    pass


class LinkCheckPending(CVBuildError):
    """GitHub could not be reached to verify links. Retry later; never send unverified."""


def _words_re(words: list):
    words = [str(w).strip() for w in words or [] if str(w).strip()]
    if not words:
        return None
    return re.compile(r"(?<![\w])(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                      + r")(?![\w])", re.I)


def role_folder(title: str) -> str:
    head = re.split(r"\s+(?:with|for|-|–|—|\()\s*|\s*[–—(]\s*", title, maxsplit=1)[0]
    for text in (head, title):
        for pattern, folder in ROLE_RULES:
            if re.search(pattern, text, re.I):
                return folder
    return "Software Engineer"


def specialty() -> list:
    """The specialty's accepted spellings, the first one canonical ([] when none)."""
    s = config.cv_rules().get("specialty") or []
    return [s] if isinstance(s, str) else [str(x) for x in s]


def lead_project() -> dict:
    """The project that leads the CV on specialty jobs (cv_rules.lead_project), or None."""
    pid = config.cv_rules().get("lead_project")
    return next((p for p in config.profile().get("projects") or [] if p.get("id") == pid), None) if pid else None


def job_flags(job: dict) -> dict:
    """related: the job is related to the experience marked with bullets_related
    (cv_rules.related_job_words); specialty: the title names the specialty
    (cv_rules.specialty_job_words). The CV writer may also find the job itself is a specialty job."""
    r = config.cv_rules()
    blob = " ".join([job.get("title", ""), job.get("company", ""), job.get("description", "")[:6000]])
    related_re, spec_re = _words_re(r.get("related_job_words")), _words_re(r.get("specialty_job_words"))
    spec = bool(spec_re and spec_re.search(job.get("title", "")))
    return {"related": bool(related_re and related_re.search(blob)), "specialty_title": spec, "specialty": spec}


def specialty_first(lead: str) -> bool:
    """The bold line opens with a held title followed by the specialty."""
    low = lead.lower()
    for t in truth_check.titles_held(config.profile()):
        for sp in specialty():
            for joint in (" with a ", ", with a ", " with an ", ", with an "):
                if low.startswith(f"{t.lower()}{joint}{sp.lower()}"):
                    return True
    return False


def _specialty_text_re():
    lp = lead_project()
    words = list(config.cv_rules().get("specialty_job_words") or [])
    if lp:
        words += [lp["name"]] + list(lp.get("stack") or [])
    return _words_re(words)


def order_bullets(content: dict) -> None:
    """Experience bullets in the profile's order."""
    for got in content.get("experience") or []:
        want = next((e for e in config.profile().get("experience") or [] if e["dates"] == got.get("dates")
                     and e["role"] == got.get("role")), None)
        if not want:
            continue
        order = [b for k in ("bullets_approved", "bullets_short", "bullets_related") for b in want.get(k, [])]
        got["bullets"] = sorted(got.get("bullets", []), key=lambda b: order.index(b) if b in order else len(order))


def _safe(s: str, n: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:n].strip()


def job_dir(job: dict, date: str = None) -> Path:
    date = date or datetime.now().strftime("%Y-%m-%d")
    name = " - ".join(x for x in (_safe(job["company"], 40), _safe(job["title"]), date) if x)
    return config.cv_root() / f"{role_folder(job['title'])} CV" / name


def system_prompt() -> str:
    """The CV writer's rules, built from PROFILE.yaml (cv_rules, writing_rules)."""
    p = config.profile()
    r = config.cv_rules()
    name = p["identity"]["name"]
    titles = " / ".join(f'"{t}"' for t in truth_check.titles_held(p))
    spec = specialty()
    lp = lead_project()
    exp = p.get("experience") or []
    rules = [
        "Use ONLY facts in PROFILE.yaml. You may choose, order, shorten and rephrase. Never add a tool, "
        "number, responsibility, title or claim. If the job asks for something the profile lacks, list it "
        "under gaps; never write it into the CV.",
        'Never say where or how a skill was used unless the profile says so: no "coursework", "production", '
        '"at scale", "years of", "led", "managed", "customers" and the like.',
        "Each skill appears once on the CV (in one group only).",
        "English only, even when the posting is in another language.",
        "Keep name, contact, education entries and languages exactly as in the base. headline stays null.",
        'The summary answers THIS job. First, in the notes block (not on the CV), write "match:" with the '
        "posting's 3 most important needs quoted in the posting's own words, each followed by \"=>\" and the "
        "strongest TRUE fact from PROFILE that answers it.",
        "KEYWORDS: applicant-tracking systems and recruiters match the posting's exact words. List in notes "
        "keywords up to 15 of the posting's key terms (tools, languages, methods, practices, domain words) that "
        f"are TRUE for {name}, in the posting's own wording, and use every one of them on the CV in that same "
        "wording: in the skills list, in a bullet that is about it, or in the summary. Never a term the profile "
        "lacks (those go under gaps).",
    ]
    if spec:
        rules.append(
            "SPECIALTY_JOB: true when SPECIALTY_TITLE is true, or when the JOB ITSELF is about the specialty "
            f"({', '.join(r.get('specialty_job_words') or [])}): building, training or using it in its duties or "
            "requirements. A company that only mentions it in its product or about-us text does not count. "
            "Write your decision in notes specialty_job.")
    lead_rule = (f"summary_lead: one sentence written for THIS job: it opens with one of the titles held "
                 f"({titles}), never another title, then what this job is about in the posting's own words, as far "
                 "as it is true for the candidate.")
    if spec:
        lead_rule += (f' The specialty is written as "{spec[0]}". When SPECIALTY_JOB is true the line opens with it '
                      f'("<title> with a {spec[0]}, building ..."); otherwise the job\'s focus comes first and the '
                      f'specialty, only if the job values it, comes at the end (", with a {spec[0]}").')
    lead_rule += f" One line on the page: at most {LEAD_MAX} characters."
    rules.append(lead_rule)
    most = int(r.get("summary_max_words") or truth_check.SUMMARY_MAX_WORDS)
    rules.append(
        "summary: about three sentences built from those matches: (1) the work that answers the top needs and "
        "the tools used; (2) another true strength from the experience, in the posting's terms where it fits; "
        "(3) the posting's own tools and practices that the profile has."
        + (" When SPECIALTY_JOB is true, the summary opens with the specialty work"
           + (f" ({lp['name']})" if lp else "") + "." if spec else "")
        + " No adjectives about the candidate (strong, talented, hard-working): the facts show it. Never claim "
        f"years, scale, production use or ownership the profile does not state. summary_lead plus summary "
        f"together: at most {most} words.")
    rules.append(
        "skills: exactly the groups from the profile (labels unchanged), reordered so what the job asks for "
        "comes first; items only from profile skills (strong or used), as written there or the short form "
        "before the parenthesis.")
    if exp:
        rules.append(
            f"experience: the {len(exp)} entries, newest first, dates/role/org exactly as the profile. Bullets "
            "copied word for word from bullets_approved, in the profile's order. An entry with bullets_short: "
            "when RELATED_JOB is false, exactly ONE bullet, its bullets_short (the space goes to what the job "
            "needs). When RELATED_JOB is true: an entry with bullets_related uses those bullets instead, and its "
            "org_related when it has one.")
    rules.append(
        "Project bullets: open with a strong past-tense verb and say what was built and with which tools, and "
        "how where there was a real design decision. Lead with the tools the posting asks for.")
    show = r.get("show_context") or []
    rules.append("Never say which course or class a project was for"
                 + (f" (the page shows a project's context only when it is one of: {', '.join(show)})." if show
                    else " (the page never shows a project's context)."))
    if r.get("no_metrics"):
        rules.append(
            "No results and no counts anywhere on the CV: no metrics or scores (MAE, accuracy, ROC-AUC, mAP, F1, "
            "percentages), no amounts of data, tests or trials. Words that say how something works are fine.")
    rules.append(
        "main_project: the ONE most relevant project"
        + (f"; when SPECIALTY_JOB is true it is {lp['name']}" if lp and spec else "")
        + "; name, context and url exactly as the profile (no url key when the profile has none); stack: "
        "items from that project's stack; 3 to 5 bullets built from its facts. Every number must appear in that "
        "project's facts. No new numbers, no extra rounding.")
    rules.append(
        "second_project: a SECOND full project (same keys as main_project), 2 to 4 bullets built from its "
        "facts, same number rules"
        + (f"; {lp['name']} whenever it is not the main project" if lp else "")
        + "; otherwise the next most relevant project. Leave it out when the profile has only one project.")
    rules.append(
        "also_built: 0 to 2 OTHER projects (not main or second), one per line: name exactly as the profile, "
        "text of a few words saying what it is and with what tools (no counts), and url exactly as the profile "
        "(no url key when it has none).")
    rules.append(
        'education: the profile\'s entries in order; "studied": 3 to 5 areas from that entry\'s studied_areas '
        'that fit the job, written like "machine learning, statistics and databases." (only when the entry has '
        "studied_areas).")
    never = ", ".join(truth_check.forbidden_words(p) + truth_check.never_write(p))
    rules.append(f"Never write anything from private_never_on_cv, nor these words: {never}. Banned words: "
                 f"{', '.join((p.get('writing_rules') or {}).get('banned_words') or [])}.")
    rules.append("It must fit on one A4 page with the base design and fill it: use the room for the projects "
                 "rather than leave the bottom of the page empty.")
    notes = ["specialty_job: <yes|no> - <short reason>"] if spec else []
    notes += [f"keywords: <term; term; term ... in the posting's own wording, true for {name}>",
              "match: <need in the posting's words> => <true fact>; <need> => <fact>; <need> => <fact>",
              "fit: <2 short lines: why the candidate fits this job, from true facts>",
              'gaps: <comma-separated requirements of the job that the profile lacks; or "none">']
    return (f"You tailor {name}'s one-page CV to a job posting. You output YAML only.\n\n"
            "Hard rules (a code check rejects violations):\n- " + "\n- ".join(rules) + "\n\n"
            "Answer with two fenced blocks and nothing else:\n```yaml\n<the full CV content file, same keys as "
            "the base>\n```\n```notes\n" + "\n".join(notes) + "\n```")


def _prompt(job: dict, flags: dict, extra: str = "", previous: str = None) -> str:
    profile_text = config.PROFILE_PATH.read_text(encoding="utf-8")
    if previous:
        base_text, label = previous, "CURRENT CV CONTENT (edit this)"
    elif config.BASE_CONTENT.exists():
        base_text, label = config.BASE_CONTENT.read_text(encoding="utf-8"), "BASE CV (copy and tailor this)"
    else:
        base_text = config.FORMAT_EXAMPLE.read_text(encoding="utf-8")
        label = ("FORMAT EXAMPLE (another, made-up person: copy ONLY its keys and structure; every fact must come "
                 "from PROFILE.yaml)")
    return (f"PROFILE.yaml:\n```yaml\n{profile_text}\n```\n\n{label}:\n```yaml\n{base_text}\n```\n\n"
            f"JOB POSTING\nCompany: {job['company']}\nTitle: {job['title']}\n"
            f"Location: {job.get('location', '')}\nURL: {job.get('url', '')}\n"
            f"RELATED_JOB: {str(flags['related']).lower()}\n"
            f"SPECIALTY_TITLE: {str(flags.get('specialty_title', False)).lower()}\n\n"
            f"{job.get('description', '')[:12000]}\n\n{extra}").strip()


def _parse(answer: str) -> tuple:
    content = yaml.safe_load(extract_block(answer, "yaml"))
    if not isinstance(content, dict):
        raise CVBuildError("Claude did not return a YAML content block")
    notes_m = re.search(r"```notes\s*\n(.*?)```", answer, re.S)
    notes = notes_m.group(1) if notes_m else ""
    match = re.search(r"match:\s*(.*?)(?:\nfit:|\Z)", notes, re.S)
    fit = re.search(r"fit:\s*(.*?)(?:\ngaps:|\Z)", notes, re.S)
    gaps = re.search(r"gaps:\s*(.*)", notes, re.S)
    gap_list = [g.strip() for g in (gaps.group(1) if gaps else "").split(",")
                if g.strip() and g.strip().lower() not in ("none", "-")]
    ml = re.search(r"^\s*specialty_job:\s*(yes|no)", notes, re.I | re.M)
    kw = re.search(r"^\s*keywords:\s*(.*)$", notes, re.I | re.M)
    extra = {"specialty": ml.group(1).lower() == "yes" if ml else None,
             "keywords": [k.strip(' "\'') for k in re.split(r"[;\n]", kw.group(1) if kw else "") if k.strip(' "\'')][:15]}
    return (content, (fit.group(1).strip() if fit else ""), gap_list, (match.group(1).strip() if match else ""),
            extra)


def _render(content_path: Path, pdf_path: Path) -> tuple:
    r = subprocess.run([sys.executable, str(config.TEMPLATE_DIR / "render_cv.py"), str(content_path), str(pdf_path),
                        "--profile", str(config.PROFILE_PATH)],
                       capture_output=True, text=True, timeout=180, cwd=config.ROOT)
    return r.returncode, (r.stdout + r.stderr).strip()


def overflow_lines(pdf_path: Path) -> int:
    """How many text lines spilled past the first page."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    return sum(len({round(w[1]) for w in page.get_text("words")}) for page in doc.pages(1))


def trim_cut(content: dict) -> str:
    """Make one small cut for a CV a line or two too long, in the length rule's order; '' when
    nothing is left to cut without going below the minimums (Also built 1, second project 2 bullets)."""
    also = content.get("also_built") or []
    if len(also) > 1:
        also.pop()
        return "dropped the last Also built line"
    bullets = (content.get("second_project") or {}).get("bullets") or []
    if len(bullets) > 2:
        bullets.pop()
        return "dropped the last second-project bullet"
    return ""


def visual_check(pdf_path: Path, content: dict, png_path: Path = None) -> list:
    """Rasterize and inspect: one page, all text inside the printable area, every line present."""
    import pymupdf
    problems = []
    doc = pymupdf.open(pdf_path)
    if len(doc) != 1:
        return [f"{len(doc)} pages"]
    page = doc[0]
    w, h = page.rect.width, page.rect.height
    mm = 72 / 25.4
    left, right, top, bottom = 18 * mm - 2, w - 18 * mm + 2, 15 * mm - 2, h - 13 * mm + 2
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        if x0 < left or x1 > right or y0 < top or y1 > bottom:
            problems.append(f"text outside the margins: '{word}'")
            break
    # compare without whitespace, so line breaks inside words (e.g. "ROC-\nAUC") do not count
    text = re.sub(r"\s+", "", page.get_text()).replace("\u00ad", "")
    expected = [content.get("summary_lead", "")] + [b for e in content.get("experience", []) for b in e.get("bullets", [])]
    expected += (content.get("main_project") or {}).get("bullets", [])
    expected += (content.get("second_project") or {}).get("bullets", [])
    expected += [i.get("text", "") for i in content.get("also_built") or []]
    for line in expected:
        tail = re.sub(r"\s+", "", line)[-25:]
        if tail and tail not in text:
            problems.append(f"line missing or cut in the PDF: '...{tail}'")
    if png_path:
        page.get_pixmap(dpi=110).save(png_path)
    return problems


def _norm_words(text: str) -> str:
    """Lower case, punctuation as spaces, plurals as singular ("LLMs" matches "LLM APIs")."""
    words = re.sub(r"[\s\-/_.,;:()]+", " ", text.lower()).split()
    return " " + " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                          for w in words) + " "


def keyword_cover(job: dict, content: dict, keywords: list = ()) -> dict:
    """The posting's tools: which ones the CV shows, which ones the profile has but the CV left out, and
    which ones the profile lacks (real gaps, never added). Plus the posting's own key terms the
    CV writer found true for the candidate: which ones the CV uses in the posting's wording."""
    from agent.score import skills_in
    post = skills_in(f"{job.get('title', '')}\n{job.get('description', '')}")
    cv = skills_in(yaml.safe_dump(content, allow_unicode=True, width=1000))
    want = set(post["strong"]) | set(post["used"])
    have = set(cv["strong"]) | set(cv["used"])
    text = _norm_words(yaml.safe_dump(content, allow_unicode=True, width=1000))
    terms = [k for k in dict.fromkeys(keywords) if _norm_words(k).strip()]
    return {"shown": sorted(want & have), "left_out": sorted(want - have), "lacking": post["missing"],
            "terms_shown": [k for k in terms if _norm_words(k) in text],
            "terms_missing": [k for k in terms if _norm_words(k) not in text]}


def improvements(job: dict, content: dict, keywords: list = ()) -> list:
    """What a valid CV could still do better for this posting (one extra round, never fatal)."""
    from agent.score import skills_in
    out = []
    cover = keyword_cover(job, content, keywords)
    if cover["terms_missing"]:
        out.append("Use the posting's own words, which are true for the candidate, on the CV (skills, a bullet about "
                   f"it, or the summary), exactly as written: {'; '.join(cover['terms_missing'])}.")
    if cover["left_out"]:
        out.append(f"The posting asks for {', '.join(cover['left_out'])}. The profile has them but the CV "
                   "does not show them: add them where true (the skills list is enough).")
    summary = f"{content.get('summary_lead', '')} {content.get('summary', '')}"
    found = skills_in(summary)
    in_summary = (set(found["strong"]) | set(found["used"])) & set(cover["shown"] + cover["left_out"])
    if len(cover["shown"] + cover["left_out"]) >= 2 and len(in_summary) < 2:
        out.append("The summary should name at least two of the posting's own tools that the profile has "
                   f"(from: {', '.join(cover['shown'] + cover['left_out'])}).")
    return out


def build(job: dict, out_dir: Path = None, feedback: str = None, previous_content: Path = None,
          log=print) -> dict:
    """Build the tailored CV for one job. Returns {pdf, content, dir, fit, gaps, preview}."""
    flags = job_flags(job)
    out_dir = out_dir or job_dir(job)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = config.STATE_DIR / "work" / out_dir.name     # scratch outside the CVs folder
    work.mkdir(parents=True, exist_ok=True)
    content_path, pdf_path = work / "content.yaml", work / config.cv_file_name()

    previous = previous_content.read_text(encoding="utf-8") if previous_content else None
    extra = ""
    if feedback:
        extra = ("THE USER'S REQUEST FOR THIS VERSION (apply it only as far as the rules allow; if it "
                 f"asks for something not in the profile, do not add it and say so under gaps):\n{feedback}")
    answer = ask(_prompt(job, flags, extra, previous), system=system_prompt())
    content, fit, gaps, match, notes = _parse(answer)
    keywords = notes["keywords"]
    flags["specialty"] = flags["specialty_title"] or bool(notes["specialty"])
    lp, spec = lead_project(), specialty()
    good, keywords_tried = None, False                   # the last CV that passed every check

    for round_no in range(1, MAX_FIX_ROUNDS + 1):
        order_bullets(content)
        lead = (content.get("summary_lead") or "").strip()
        if lead and lead[-1] not in ".!?":
            content["summary_lead"] = lead + "."                 # the bold line is a sentence
        problems = truth_check.check(content, related=flags["related"])
        on_spec = flags["specialty"] and bool(spec)
        if on_spec and lp and (content.get("main_project") or {}).get("name") != lp["name"]:
            problems.append(f"This is a specialty job: {lp['name']} must be the main_project (first), "
                            "with the other project as second_project.")
        lead = content.get("summary_lead") or ""
        title = truth_check.titles_held(config.profile())[0]
        if on_spec and not specialty_first(lead):
            problems.append("This is a specialty job: the bold summary_lead opens with the specialty: "
                            f"'{title} with a {spec[0]}, <the job's focus>'.")
        if spec and not on_spec and specialty_first(lead):
            problems.append("Not a specialty job: the bold summary_lead opens with the job's focus; the "
                            f"specialty may come at the end (', with a {spec[0]}').")
        if len(lead) > LEAD_MAX:
            problems.append(f"The bold summary_lead must fit one line: {len(lead)} characters, keep it to "
                            f"{LEAD_MAX} or fewer.")
        spec_text = _specialty_text_re()
        first = re.split(r"(?<=\.)\s", (content.get("summary") or "") + " ")[0]
        if on_spec and spec_text and not spec_text.search(first):
            problems.append("This is a specialty job: the summary's first sentence must be about the specialty work"
                            + (f" ({lp['name']})." if lp else "."))
        stage = "truth"
        if not problems:
            content_path.write_text(yaml.safe_dump(content, allow_unicode=True, sort_keys=False, width=1000),
                                    encoding="utf-8")
            code, out = _render(content_path, pdf_path)
            while code == 2 and overflow_lines(pdf_path) <= TRIM_MAX_LINES:
                cut = trim_cut(content)
                if not cut:
                    break
                log(f"render round {round_no}: a line or two over one page, {cut}")
                content_path.write_text(yaml.safe_dump(content, allow_unicode=True, sort_keys=False, width=1000),
                                        encoding="utf-8")
                code, out = _render(content_path, pdf_path)
            log(f"render round {round_no}: exit {code}")
            if code == 0:
                problems = visual_check(pdf_path, content, work / "preview.png")
                stage = "visual"
                if not problems:
                    better = improvements(job, content, keywords)
                    if not better or keywords_tried or round_no == MAX_FIX_ROUNDS:
                        break
                    # one more round to show what the posting asks for that the profile really has
                    keywords_tried, stage = True, "keywords"
                    good = (yaml.safe_dump(content, allow_unicode=True, sort_keys=False, width=1000), fit, gaps)
                    shutil.copy(pdf_path, work / "good.pdf")
                    problems = better + ["Keep everything else and keep it to one page."]
            elif code == 2:
                stage = "length"
                over = overflow_lines(pdf_path)
                problems = [f"The PDF is {over} line{'s' if over != 1 else ''} longer than one page: cut at least that much. "
                            "Shorten in this order: tighter wording "
                            "(summary, long bullets), then Also built down to its single most relevant "
                            "line, then fewer second-project bullets (minimum 2), then fewer main-project "
                            "bullets (minimum 3), then drop the last Also built line. Never change the design."]
            elif code == 3:
                if re.search(r"not verified|could not reach|rate limit", out):
                    raise LinkCheckPending(out)
                stage = "links"
                problems = [l for l in out.splitlines() if l.startswith("LINK PROBLEM")]
            else:
                raise CVBuildError(f"render failed: {out[-500:]}")
        log(f"{stage} problems (round {round_no}): {problems}")
        if round_no == MAX_FIX_ROUNDS and good:          # the keyword round failed: keep the good CV
            content, fit, gaps = yaml.safe_load(good[0]), good[1], good[2]
            content_path.write_text(good[0], encoding="utf-8")
            shutil.copy(work / "good.pdf", pdf_path)
            visual_check(pdf_path, content, work / "preview.png")
            break
        if round_no == MAX_FIX_ROUNDS:
            raise CVBuildError(f"could not produce a valid CV after {MAX_FIX_ROUNDS} rounds: {problems}")
        fix = ("Your CV content failed these checks. Return the corrected full content (same two "
               "blocks). Fix only what is listed:\n- " + "\n- ".join(problems))
        answer = ask(_prompt(job, flags, fix, yaml.safe_dump(content, allow_unicode=True, sort_keys=False, width=1000)),
                     system=system_prompt())
        content, fit2, gaps2, match2, notes = _parse(answer)
        fit, gaps, match = fit2 or fit, gaps2 or gaps, match2 or match
        keywords = notes["keywords"] or keywords
        if notes["specialty"] is not None:
            flags["specialty"] = flags["specialty_title"] or notes["specialty"]

    # Keep earlier versions after "Fix" or "Try again"
    final_pdf, final_content = out_dir / config.cv_file_name(), out_dir / "content.yaml"
    if final_pdf.exists():
        vdir = out_dir / "_versions" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
        vdir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(final_pdf), vdir / config.cv_file_name())
        if final_content.exists():
            shutil.move(str(final_content), vdir / "content.yaml")
    shutil.move(str(pdf_path), final_pdf)
    shutil.move(str(content_path), final_content)
    preview = work / "preview.png"
    # remember what this build produced, so an interrupted send can resume without rebuilding
    cover = keyword_cover(job, content, keywords)
    (work / "notes.json").write_text(json.dumps({"fit": fit, "gaps": gaps, "pdf": str(final_pdf), "cover": cover,
                                                 "match": match,
                                                 "built": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                                ensure_ascii=False), encoding="utf-8")
    return {"pdf": final_pdf, "content": final_content, "dir": out_dir, "fit": fit, "gaps": gaps,
            "preview": preview, "flags": flags, "cover": cover, "match": match}
