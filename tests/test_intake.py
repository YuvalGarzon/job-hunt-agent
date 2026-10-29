import json

from agent import bot, intake
from agent.sources import ats, bigco
from tests.test_bot_idempotency import sheet, sent  # noqa: F401  (fixtures)


def msg(uid, text, **extra):
    return {"update_id": uid, "message": {"message_id": 9, "chat": {"id": 42}, "from": {"id": 42}, "text": text, **extra}}


def test_urls_in_finds_hidden_links():
    m = {"text": "Backend dev at Acme, apply here! more: https://acme.com/careers.",
         "entities": [{"type": "text_link", "offset": 30, "length": 4, "url": "https://jobs.lever.co/acme/123"}]}
    assert intake.urls_in(m) == ["https://jobs.lever.co/acme/123", "https://acme.com/careers"]


def test_is_forward():
    assert intake.is_forward({"forward_origin": {"type": "channel"}})
    assert not intake.is_forward({"text": "hi"})


def test_forward_queues_one_intake_task(sheet, sent):
    post = msg(7, "Hiring: Python developer at Acme in Manchester", forward_origin={"type": "channel"})
    bot.handle(sheet, post, by="mac", mac_online=True)
    bot.handle(sheet, post, by="cloud", mac_online=False)          # same update again
    q = sheet.t("Queue").rows
    assert len(q) == 1 and q[0]["Type"] == "intake"
    assert "Python" in json.loads(q[0]["Payload"])["text"]


def test_bare_link_is_a_job_but_text_is_chat(sheet, sent):
    bot.handle(sheet, msg(8, "https://jobs.lever.co/acme/123"), by="mac", mac_online=True)
    bot.handle(sheet, msg(9, "what do you think about my CV?"), by="mac", mac_online=True)
    types = [r["Type"] for r in sheet.t("Queue").rows]
    assert types == ["intake", "chat"]


def test_job_command(sheet, sent):
    bot.handle(sheet, msg(10, "/job https://boards.greenhouse.io/acme/jobs/555"), by="mac", mac_online=True)
    q = sheet.t("Queue").rows
    assert len(q) == 1 and json.loads(q[0]["Payload"])["urls"] == ["https://boards.greenhouse.io/acme/jobs/555"]


def test_resolve_ats_links(monkeypatch):
    calls = []
    one = {"title": "Backend Engineer", "description": "Python", "url": "https://x"}
    for name in ("greenhouse_one", "lever_one", "ashby_one", "comeet_one"):
        monkeypatch.setattr(ats, name, lambda b, j, n=name: calls.append((n, b, j)) or one)
    monkeypatch.setattr(bigco, "workday_one", lambda b, p: calls.append(("workday_one", b, p)) or one)
    for u in ["https://job-boards.greenhouse.io/acmeco/jobs/8239378",
              "https://jobs.lever.co/acme/1a2b-3c",
              "https://jobs.ashbyhq.com/lunarlabs/uuid-1",
              "https://www.comeet.com/jobs/playforge/C2.37D/backend-engineer/A1.B2C",
              "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/UK-Manchester/Dev_JR1"]:
        job = intake.resolve_ats(u)
        assert job and job.title == "Backend Engineer", u
    assert calls == [("greenhouse_one", "acmeco", "8239378"), ("lever_one", "acme", "1a2b-3c"),
                     ("ashby_one", "lunarlabs", "uuid-1"), ("comeet_one", "playforge/C2.37D", "A1.B2C"),
                     ("workday_one", "nvidia.wd5/nvidia/NVIDIAExternalCareerSite", "/job/UK-Manchester/Dev_JR1")]


def test_linkedin_and_telegram_links_never_fetched(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(intake.requests, "get", boom)
    assert intake.resolve_page("https://www.linkedin.com/jobs/view/123/") is None
    assert intake.resolve_page("https://t.me/somegroup/55") is None
    assert intake.resolve_page("https://www.indeed.com/viewjob?jk=38827453") is None
    assert intake.resolve_page("https://www.glassdoor.com/job-listing/1") is None


def test_text_with_a_link_is_a_job(sheet, sent):
    bot.handle(sheet, msg(11, "Junior roles: Backend dev at Acme https://acme.com/jobs/1 apply now"), by="mac",
               mac_online=True)
    assert [r["Type"] for r in sheet.t("Queue").rows] == ["intake"]


def test_job_usage_text_is_valid_html(sheet, sent):
    bot.handle(sheet, msg(12, "/job"), by="mac", mac_online=True)
    assert "<link" not in sent[-1] and "send me the job" in sent[-1]


def test_multi_job_post(monkeypatch):
    from agent.jobs import Job
    monkeypatch.setattr(intake, "resolve_ats", lambda u: None)
    monkeypatch.setattr(intake, "resolve_page", lambda u: None)
    monkeypatch.setattr(intake, "find_on_board", lambda t, c, s: None)
    monkeypatch.setattr(intake, "extract", lambda text, urls: [
        {"title": "Junior Full Stack Developer", "company": "Pixelcraft", "location": "Manchester", "link": "https://x.io/1"},
        {"title": "Junior MLOps Engineer", "company": "Dell", "location": "United Kingdom", "link": ""}])
    jobs = intake.collect("Junior roles - 6 new roles ...", ["https://x.io/1", "https://x.io/2"], None)
    assert [(j.company, j.title, j.source) for j in jobs] == [("Pixelcraft", "Junior Full Stack Developer", "telegram"),
                                                             ("Dell", "Junior MLOps Engineer", "telegram")]
    assert jobs[0].url == "https://x.io/1" and jobs[1].url == ""


def test_job_command_reads_hidden_links(sheet, sent):
    m = msg(13, "/job Junior roles - Pixelcraft", entities=[{"type": "bot_command", "offset": 0, "length": 4},
                                                         {"type": "text_link", "offset": 5, "length": 6,
                                                          "url": "https://jobs.lever.co/pixelcraft/9"}])
    bot.handle(sheet, m, by="mac", mac_online=True)
    q = sheet.t("Queue").rows
    assert len(q) == 1 and json.loads(q[0]["Payload"])["urls"] == ["https://jobs.lever.co/pixelcraft/9"]


def photo_msg(uid, caption="", **extra):
    m = msg(uid, "", photo=[{"file_id": "small", "file_size": 900, "width": 90, "height": 160},
                            {"file_id": "big", "file_size": 90000, "width": 900, "height": 1600}], **extra)
    del m["message"]["text"]
    if caption:
        m["message"]["caption"] = caption
    return m


def test_photo_without_caption_is_a_job(sheet, sent):
    bot.handle(sheet, photo_msg(20), by="mac", mac_online=True)
    q = sheet.t("Queue").rows
    assert len(q) == 1 and q[0]["Type"] == "intake"
    assert json.loads(q[0]["Payload"])["media"][0]["file_id"] == "big"       # the largest size
    assert "the image" in sent[-1]


def test_album_becomes_one_task(sheet, sent):
    bot.handle(sheet, photo_msg(21, "Junior QA at Acme", media_group_id="g1"), by="mac", mac_online=True)
    second = photo_msg(22, media_group_id="g1")
    second["message"]["photo"][-1]["file_id"] = "big2"
    bot.handle(sheet, second, by="mac", mac_online=True)
    bot.handle(sheet, second, by="mac", mac_online=True)                     # same update again
    q = sheet.t("Queue").rows
    assert len(q) == 1
    p = json.loads(q[0]["Payload"])
    assert [m["file_id"] for m in p["media"]] == ["big", "big2"] and p["text"] == "Junior QA at Acme"
    assert len(sent) == 1


def test_files(sheet, sent):
    pdf = msg(23, "", document={"file_id": "f1", "file_name": "Job.PDF", "mime_type": "application/pdf"})
    word = msg(24, "", document={"file_id": "f2", "file_name": "role.docx"})
    zipf = msg(25, "", document={"file_id": "f3", "file_name": "x.zip", "mime_type": "application/zip"})
    for m in (pdf, word, zipf):
        del m["message"]["text"]
        bot.handle(sheet, m, by="mac", mac_online=True)
    q = sheet.t("Queue").rows
    assert [json.loads(r["Payload"])["media"][0]["file_id"] for r in q] == ["f1", "f2"]
    assert "zip" in sent[-1] and "can't read" in sent[-1]


def test_unreadable_messages_get_an_answer(sheet, sent):
    m = msg(26, "", voice={"file_id": "v"})
    del m["message"]["text"]
    bot.handle(sheet, m, by="mac", mac_online=True)
    assert not sheet.t("Queue").rows and "a voice message" in sent[-1]


def test_post_without_links_is_read(monkeypatch):
    monkeypatch.setattr(intake, "find_on_board", lambda t, c, s: None)
    monkeypatch.setattr(intake, "extract", lambda text, urls: [
        {"title": "Junior Data Analyst", "company": "Acme", "location": "Manchester", "link": ""}])
    jobs = intake.collect("We are hiring! Junior Data Analyst at Acme, Manchester", [], None)
    assert [(j.company, j.title) for j in jobs] == [("Acme", "Junior Data Analyst")]


def test_read_media(monkeypatch, tmp_path):
    from agent import telegram as tg
    monkeypatch.setattr(intake, "MEDIA_DIR", tmp_path)
    files = {"p": b"\x89PNG fake", "t": "Backend Developer at Acme\nhttps://jobs.lever.co/acme/1".encode()}
    monkeypatch.setattr(tg, "get_file", lambda fid: files[fid])
    seen = {}

    def ask(prompt, files_dir=None, **k):
        seen["files"] = sorted(f.name for f in files_dir.iterdir())
        return "Junior QA Engineer - Acme"
    monkeypatch.setattr(intake.claude_cli, "ask", ask)
    text = intake.read_media([{"file_id": "p", "name": "photo.jpg", "mime": "image/png", "size": 9},
                              {"file_id": "t", "name": "a.txt", "mime": "text/plain", "size": 9}], "intake-1")
    assert seen["files"] == ["image_1.png"]
    assert "Backend Developer" in text and "Junior QA Engineer" in text
    assert not list(tmp_path.iterdir())                                        # files deleted afterwards


def test_docx_and_pdf_text(tmp_path):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Junior DevOps</w:t></w:r></w:p>"
                                         "<w:p><w:r><w:t>Acme &amp; Co</w:t></w:r></w:p></w:body></w:document>")
    assert intake._docx_text(buf.getvalue()).split() == ["Junior", "DevOps", "Acme", "&", "Co"]
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), "Junior Software Engineer at Acme\nManchester. Python, SQL, Linux")
    text, images = intake._pdf_text(doc.tobytes(), tmp_path)
    assert "Junior Software Engineer" in text and images == []
    scanned = pymupdf.open()
    scanned.new_page()                                                         # no text layer: read as an image
    text, images = intake._pdf_text(scanned.tobytes(), tmp_path)
    assert text == "" and [f.name for f in images] == ["page_1.png"]


def test_post_pasted_after_bare_job_is_a_job(sheet, sent):
    bot.handle(sheet, msg(30, "/job"), by="mac", mac_online=True)
    assert "screenshot" in sent[-1]
    bot.handle(sheet, msg(31, "We are hiring a Junior Data Engineer, Manchester"), by="mac", mac_online=True)
    q = sheet.t("Queue").rows
    assert [r["Type"] for r in q] == ["intake"] and "approve" not in json.loads(q[0]["Payload"])
    bot.handle(sheet, msg(32, "thanks!"), by="mac", mac_online=True)        # waiting is over: chat again
    assert [r["Type"] for r in sheet.t("Queue").rows] == ["intake", "chat"]


def test_photo_after_bare_cv_gets_a_cv(sheet, sent):
    bot.handle(sheet, msg(33, "/cv"), by="mac", mac_online=True)
    bot.handle(sheet, photo_msg(34), by="mac", mac_online=True)
    q = sheet.t("Queue").rows
    assert len(q) == 1 and json.loads(q[0]["Payload"])["approve"] is True


def test_cv_folder_without_company():
    from agent import cv_builder
    d = cv_builder.job_dir({"company": "?", "title": "Data Engineer"}, "2026-10-24")
    assert d.name == "Data Engineer - 2026-10-24"
