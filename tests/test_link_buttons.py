import json

import pytest

from agent import alerts
from agent import telegram as tg


@pytest.fixture
def calls(monkeypatch):
    out = []
    monkeypatch.setattr(tg, "chat_id", lambda: "42")
    monkeypatch.setattr(tg, "_call", lambda method, data, **k: out.append((method, dict(data))) or {"message_id": 5})
    return out


def kb(data):
    return json.loads(data["reply_markup"])["inline_keyboard"]


def test_link_is_a_button():
    rows = alerts.job_buttons("J1", url="https://boards.greenhouse.io/acme/jobs/1")
    assert json.loads(tg.keyboard(rows))["inline_keyboard"][1] == \
        [{"text": "🔗 Open job", "url": "https://boards.greenhouse.io/acme/jobs/1"}]
    assert alerts.job_buttons("J1", url="") == alerts.job_buttons("J1")
    assert tg.link_row("not a link") == []


def test_card_text_has_no_text_link():
    row = {"Title": "Data Engineer", "Company": "Acme", "City": "", "Location": "", "Location flag": "",
           "Score": "70", "Fit": "", "Gaps": "", "Stretch": "", "Source": "greenhouse", "URL": "https://x.io/1"}
    assert "href" not in alerts.job_text(row, 1)


def test_edit_keeps_the_link_button(calls):
    tg.keep_links(7, {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "ap:J1"}],
                                          [{"text": "🔗 Open job", "url": "https://x.io/1"}]]})
    tg.edit_buttons(7, [[("✅ Approved", "noop:")]])
    tg.edit_buttons(7, [])
    tg.edit_buttons(8, [])                                                    # another message: nothing kept
    assert kb(calls[0][1])[-1] == [{"text": "🔗 Open job", "url": "https://x.io/1"}]
    assert kb(calls[1][1]) == [[{"text": "🔗 Open job", "url": "https://x.io/1"}]]
    assert kb(calls[2][1]) == []


def test_refused_link_never_blocks_the_message(monkeypatch):
    sent = []

    def call(method, data, **k):
        if "url" in data.get("reply_markup", ""):
            raise tg.TelegramError("sendMessage failed: Bad Request: BUTTON_URL_INVALID")
        sent.append(data)
        return {"message_id": 6}
    monkeypatch.setattr(tg, "chat_id", lambda: "42")
    monkeypatch.setattr(tg, "_call", call)
    tg.send_message("job", buttons=alerts.job_buttons("J1", url="https://bad link"))
    assert kb(sent[0]) == [[{"text": "✅ Approve", "callback_data": "ap:J1"}, {"text": "⏭ Skip", "callback_data": "sk:J1"}]]
