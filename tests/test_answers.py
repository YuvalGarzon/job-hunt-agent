import json

import pytest

from agent import answers, bot
from tests.test_bot_idempotency import cb, sheet, sent  # noqa: F401  (fixtures)
from tests.test_open_and_replies import msg


@pytest.fixture
def claude(monkeypatch):
    calls = []

    def ask(prompt, **k):
        calls.append(prompt)
        return calls_out.pop(0)
    calls_out = []
    monkeypatch.setattr(answers.claude_cli, "ask", ask)
    monkeypatch.setattr("agent.writing._profile_for_writing", lambda: "PROFILE")
    return calls, calls_out


def run_tasks(sheet):
    for t in [t for t in sheet.t("Queue").rows if t["Type"] == "answer" and t["Status"] == "pending"]:
        answers.write(sheet, t, json.loads(t["Payload"]), print)
        t["Status"] = "done"


def test_bare_answer_then_question_then_save_and_reuse(sheet, sent, claude):
    calls, out = claude
    bot.handle(sheet, msg(10, "/answer"), by="mac", mac_online=True)
    assert "send me the question" in sent[-1]
    bot.handle(sheet, msg(11, "Describe your experience with computer vision."), by="mac", mac_online=True)
    (t,) = sheet.t("Queue").rows
    assert t["Type"] == "answer" and json.loads(t["Payload"])["text"].startswith("Describe")
    out.append(json.dumps({"question": "Describe your experience with computer vision.",
                           "answer": "I built object detection models with YOLOv8 and PyTorch.", "reused": "", "note": ""}))
    run_tasks(sheet)
    row = sheet.t("Answers").rows[0]
    assert row["Status"] == "draft" and "YOLOv8" in sent[-1]
    bot.handle(sheet, cb(f"anss:{row['Q ID']}", 12), by="mac", mac_online=True)
    assert row["Status"] == "saved"

    bot.handle(sheet, msg(13, "/answer Tell us about your CV work"), by="mac", mac_online=True)
    out.append(json.dumps({"question": "Tell us about your CV work", "answer": "I built detection models.",
                           "reused": row["Q ID"], "note": ""}))
    run_tasks(sheet)
    assert row["Q ID"] in calls[-1] and row["Uses"] == "1" and any("♻️" in m for m in sent)


def test_metrics_are_refused_and_rewritten(sheet, sent, claude):
    calls, out = claude
    bot.handle(sheet, msg(20, "/answer What was your best model?"), by="mac", mac_online=True)
    out += [json.dumps({"question": "q", "answer": "My model reached 94% accuracy.", "reused": "", "note": ""}),
            json.dumps({"question": "q", "answer": "I trained a fusion model of CLIP and YOLOv8.", "reused": "",
                        "note": ""})]
    run_tasks(sheet)
    assert "no metrics" in calls[-1] and "94%" not in sheet.t("Answers").rows[0]["Answer"]


def test_reply_to_an_answer_revises_it(sheet, sent, claude, monkeypatch):
    calls, out = claude
    monkeypatch.setattr(bot.tg, "send_message", lambda text, buttons=None, **k: sent.append(text) or {"message_id": 77})
    bot.handle(sheet, msg(30, "/answer Why do you want this job?"), by="mac", mac_online=True)
    out.append(json.dumps({"question": "Why do you want this job?", "answer": "Because I like data.", "reused": "",
                           "note": ""}))
    run_tasks(sheet)
    row = sheet.t("Answers").rows[0]
    bot.handle(sheet, msg(31, "mention machine learning", reply_to_message={"message_id": 77}), by="mac", mac_online=True)
    t = sheet.t("Queue").rows[-1]
    assert json.loads(t["Payload"]) == {"qid": row["Q ID"], "mode": "fix", "feedback": "mention machine learning"}
    out.append(json.dumps({"question": "x", "answer": "Because I like data and machine learning.", "reused": "",
                           "note": ""}))
    run_tasks(sheet)
    assert len(sheet.t("Answers").rows) == 1 and "machine learning" in row["Answer"] and "Because I like data." in calls[-1]


def test_without_answer_mode_text_is_still_chat(sheet, sent):
    bot.handle(sheet, msg(40, "how are you?"), by="mac", mac_online=True)
    assert [t["Type"] for t in sheet.t("Queue").rows] == ["chat"]


def test_a_tool_named_in_the_question_may_be_denied(sheet, sent, claude):
    calls, out = claude
    bot.handle(sheet, msg(50, "/answer Do you have experience with Kubernetes?"), by="mac", mac_online=True)
    out.append(json.dumps({"question": "Do you have experience with Kubernetes?",
                           "answer": "No, I haven't used Kubernetes yet.", "reused": "", "note": ""}))
    run_tasks(sheet)
    assert len(calls) == 1 and sheet.t("Answers").rows[0]["Answer"].startswith("No")
