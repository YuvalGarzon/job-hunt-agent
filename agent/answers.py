"""Screening-question answers (/answer) and the answer bank.

The user sends an application form's question (typed, pasted, or a screenshot). Claude writes an
answer from PROFILE only, describing the work without metrics or results, and reuses an answer
saved before when the question is the same in other words. Every answer is a row in the
Answers tab: "draft" until the user taps ✅ Save, then "saved" (the bank). ✏️ Shorter and 🔁 Try
again rewrite it; a Reply to the answer in the user's own words revises it.
"""
import hashlib
import json
import re

from agent import claude_cli, config
from agent import telegram as tg
from agent.sheet import Sheet, now_iso
from agent.truth_check import check_text

BANK_MAX = 40
RESULTS = re.compile(r"\d+(\.\d+)?\s*%|\bmAP\b|\bROC\b|\bAUC\b|\bF1\b|accuracy of|precision of|recall of", re.I)

PROMPT = """Write {name}'s answer to a screening question on a job application form.

Rules:
- Use only facts from PROFILE and obey PROFILE.writing_rules. Never invent experience, tools,
  numbers, employers or durations. Never say where or how a skill was used unless PROFILE says so.
- Describe what was built and how; give no metrics or results (no accuracy, mAP, scores,
  percentages, dataset sizes).
- Never write the words in PROFILE.writing_rules.forbidden_words, nor anything an experience's
  never_write lists.
- If {first} lacks something the question asks about, say so honestly in one short sentence and
  name the closest true experience.
- If the question needs a fact PROFILE does not have (salary, notice period, start date,
  relocation, visa), write a neutral answer without inventing it, and say in "note" what {first}
  should tell you to complete it.
- Answer in the language of the question. First person, plain and confident, no buzzwords.
  Fit the form: 2-5 sentences unless the question asks for more; a yes/no question starts
  with Yes or No.
- SAVED ANSWERS are answers {first} approved. If one answers the same question in other words,
  reuse it (adapt only what this question needs) and put its id in "reused".
{job}{extra}
QUESTION:
{question}

SAVED ANSWERS:
{bank}

PROFILE:
{profile}

Return JSON only: {{"question": "the question, cleaned up (from the screenshot text if any)",
"answer": "...", "reused": "<saved id or empty>", "note": "short note to {first}, or empty"}}"""


def buttons(qid: str) -> list:
    return [[("✅ Save", f"anss:{qid}"), ("✏️ Shorter", f"ansh:{qid}"), ("🔁 Try again", f"ansr:{qid}")]]


def bank(sheet: Sheet) -> list:
    return [r for r in sheet.t("Answers").rows if r["Status"] == "saved"][-BANK_MAX:]


def new_id(question: str) -> str:
    return "Q" + hashlib.sha1(f"{question}|{now_iso()}".encode("utf-8")).hexdigest()[:8]


def _job_text(sheet: Sheet, job_id: str) -> tuple:
    if not job_id:
        return "", ""
    from agent.tasks import _job_from_sheet
    try:
        job = _job_from_sheet(sheet, job_id)
    except Exception:
        return "", ""
    if not job:
        return "", ""
    desc = (job.get("description") or "")[:5000]
    return (f"\nTHE JOB this form belongs to: {job.get('title', '')} at {job.get('company', '')}.\n"
            f"POSTING:\n{desc}\n"), desc


def write(sheet: Sheet, task: dict, payload: dict, log) -> str:
    from agent.writing import _profile_for_writing
    sheet.load("Answers", "Jobs")
    tab = sheet.t("Answers")
    row = tab.find("Q ID", payload.get("qid", "")) if payload.get("qid") else None
    question = payload.get("text", "")
    if payload.get("media"):
        from agent import intake
        question = "\n\n".join(t for t in (question, intake.read_media(payload["media"], task["Task ID"])) if t)
    mode = payload.get("mode", "new")
    extra = ""
    if row:
        question = row["Question"]
        if mode == "shorter":
            extra = f"\nMake this answer clearly shorter, keeping the strongest true points:\n{row['Answer']}\n"
        elif mode == "retry":
            extra = f"\nWrite a different version (other true points, other wording) than:\n{row['Answer']}\n"
        elif mode == "fix":
            extra = (f"\nRevise this answer. The user asked, in their own words: \"{payload.get('feedback', '')}\"\n"
                     f"Change only what they asked; keep the rest:\n{row['Answer']}\n")
    if not question.strip():
        tg.send_message("🤔 I didn't find a question. Send the question as text or a clear screenshot.")
        return "no question"
    job_id = payload.get("job_id") or (row["Job ID"] if row else "")
    job, job_desc = _job_text(sheet, job_id)
    saved = bank(sheet)
    bank_text = "\n\n".join(f"[{r['Q ID']}] Q: {r['Question']}\nA: {r['Answer']}" for r in saved) or "(none yet)"
    prompt = PROMPT.format(name=config.name(), first=config.first_name(), job=job, extra=extra,
                           question=question[:3000], bank=bank_text[:12000], profile=_profile_for_writing())
    data, problems = {}, []
    for _ in range(3):
        out = claude_cli.ask(prompt + (f"\n\nYour previous answer broke these rules, fix them: {problems}"
                                       if problems else ""))
        m = re.search(r"\{.*\}", out, re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except ValueError:
            data = {}
        answer = str(data.get("answer") or "").strip()
        # terms the question or the posting name describe what is asked, not claims about the user
        problems = check_text(answer, f"{job_desc}\n{question}") if answer else ["no answer returned"]
        if RESULTS.search(answer):
            problems.append("no metrics or results (accuracy, mAP, percentages): describe the work only")
        if not problems:
            break
    if problems:
        raise RuntimeError(f"answer failed the checks: {problems}")
    clean_q = str(data.get("question") or question).strip()[:1000]
    reused = next((r for r in saved if r["Q ID"] == str(data.get("reused") or "").strip()), None)
    if reused:
        tab.update(reused, Uses=str(int(reused["Uses"] or 0) + 1))
    if row:
        tab.update(row, Answer=answer, Status="draft", Updated=now_iso())
    else:
        row = tab.append({"Q ID": new_id(clean_q), "Question": clean_q, "Answer": answer, "Status": "draft",
                          "Job ID": job_id, "Created": now_iso(), "Updated": now_iso(), "Uses": "0"})
    sheet.flush()
    head = f"📝 <b>An answer to the question</b>\n<i>{tg.esc(clean_q[:300])}</i>"
    if reused:
        head += "\n♻️ Based on an answer you saved"
    if data.get("note"):
        head += f"\nℹ️ {tg.esc(str(data['note'])[:300])}"
    tg.send_message(head)
    msg = tg.send_message(f"<pre>{tg.esc(answer[:3800])}</pre>\n↩️ Reply with what to change.",
                          buttons=buttons(row["Q ID"]))
    tab.update(row, **{"Msg ID": str(msg["message_id"])})
    sheet.flush()
    return "sent"


def save(sheet: Sheet, qid: str) -> dict:
    row = sheet.t("Answers").find("Q ID", qid)
    if row:
        sheet.t("Answers").update(row, Status="saved", Updated=now_iso())
        sheet.flush()
    return row


def for_message(sheet: Sheet, msg_id) -> dict:
    return sheet.t("Answers").find("Msg ID", str(msg_id)) if msg_id else None
