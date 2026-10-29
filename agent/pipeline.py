"""Find jobs and alert them in one batch (runs in the cloud; can also run on the Mac).

Every job in the user's country (or of unknown location) is recorded once in the Jobs tab with
its decision and reason; that tab is the dedup. Jobs clearly outside the country are only counted.
"""
import concurrent.futures as cf
import re
import time
import uuid
from datetime import datetime, timezone

import requests

from agent import filters, geo, gmail, learn, sources
from agent.jobs import norm_text
from agent.score import fit_lines, score
from agent.sheet import Sheet, now_iso
from agent.sources import board_mail, linkedin

BATCH_SIZE = 10
GEOCODE_BUDGET = 20          # Nominatim lookups per run (1 per second); results are cached


def threshold(sheet: Sheet) -> int:
    from agent import config
    try:
        return int(sheet.kv_get("Settings", "fit_threshold", str(config.settings()["fit_threshold_default"])))
    except ValueError:
        return 50


def verify_live(job) -> str:
    """'' if the posting is live; otherwise the reason. 'retry' = could not check now."""
    if job.extra.get("unverified"):
        # alerted, tagged. Never opened: LinkedIn and the job boards are not fetched (rule 4).
        where = "LinkedIn" if job.source == "linkedin" else f"{job.source} alert email"
        job.extra["verify_note"] = f"{where} only: not found on the company's careers page (alerted, tagged)"
        return ""
    try:
        r = requests.get(job.url, timeout=10, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; job-hunt-agent personal job search)"})
    except requests.RequestException:
        return "retry"
    if r.status_code in (404, 410):
        return "posting is gone (HTTP %d)" % r.status_code
    if r.status_code >= 500:
        return "retry"
    if r.status_code >= 400 and r.status_code not in (401, 403, 429):
        return "posting link fails (HTTP %d)" % r.status_code
    page = norm_text(re.sub(r"<[^>]+>", " ", r.text))
    words = [w for w in norm_text(job.title).split() if len(w) > 2]
    if words and sum(w in page for w in words) < max(1, int(len(words) * 0.6)):
        # Many career pages render with JavaScript; the ATS API listing it in this run is the
        # proof that it is live. Note it, do not drop.
        job.extra["verify_note"] = "title not in page HTML (JS page); live per ATS API"
    final = r.url.lower()
    if re.search(r"/(jobs|careers)/?$|error|not-?found", final) and "gh_jid" not in final and job.source != "comeet":
        return "posting redirects to a generic page (closed)"
    return ""


def evaluate(job, loc: dict, tiers: dict) -> tuple:
    """(Jobs-tab row, drop reason or ''): the hard filters, then the score for a job that passes."""
    sen = filters.seniority(job)
    reason = (filters.title_check(job.title) or sen["drop"] or filters.employment_check(job)
              or filters.recency_check(job) or loc["drop_reason"] or filters.remote_check(job, loc)
              or filters.scam_check(job))
    row = {"Job ID": job.job_id, "First seen": now_iso(), "Source": job.source, "Source ref": job.source_ref,
           "Company": job.company, "Title": job.title, "Location": job.location[:200],
           "City": loc["city"], "Distance km": loc["km"], "Posted": job.posted[:10], "URL": job.url,
           "Stretch": "yes" if sen.get("stretch") else "", "Location flag": loc["flag"], "Updated": now_iso()}
    if reason:
        return row, reason
    sc = score(job, sen, loc, tiers)
    line1, line2, gaps = fit_lines(job, sc, sen)
    parts = " ".join(f"{k}={v}" for k, v in sc["parts"].items())
    row.update({"Score": sc["score"], "Score parts": parts, "Fit": f"{line1}\n{line2}", "Gaps": gaps})
    return row, ""


def _geo_cache(sheet: Sheet) -> dict:
    cache = {}
    for r in sheet.t("Geo").rows:
        if r["Lat"] and r["Lon"]:
            cache[r["Place"]] = (float(r["Lat"]), float(r["Lon"]), r.get("Country", ""))
        else:
            cache[r["Place"]] = None
    return cache


def alert_settings() -> dict:
    from agent import config
    return config.settings().get("email_alerts") or {}


def find_and_alert(sheet: Sheet, send: bool = True, log=print) -> dict:
    """Fetch, filter, score, verify and alert. Returns run stats."""
    from agent import alerts
    from agent.bot import same_job_app
    t0 = time.time()
    sheet.load("Jobs", "Settings", "Geo", "Health")
    if sheet.kv_get("Settings", "paused", "no") == "yes":
        log("paused: not searching")
        return {"paused": True}
    jobs_tab = sheet.t("Jobs")
    seen = {r["Job ID"] for r in jobs_tab.rows}
    cache = _geo_cache(sheet)
    cache_before = set(cache)
    budget = [GEOCODE_BUDGET]
    tiers = sources.company_tiers()
    thr = threshold(sheet)
    memory = learn.Memory(sheet)                     # what the Skip reasons taught
    sent = alerts.Sent(sheet)                        # the same opening is never sent twice

    sheet.load("Boards")
    fetched, errors = sources.fetch_all(extra=linkedin.boards(sheet))
    # LinkedIn alert emails: matched to the company's own posting, or recorded as LinkedIn-only
    if alert_settings().get("linkedin", True):
        try:
            fetched += linkedin.collect(sheet, fetched, [c["name"] for c in sources.companies()], log=log)
        except gmail.GmailAuthError as e:
            errors["LinkedIn alerts"] = str(e)
        except Exception as e:
            errors["LinkedIn alerts"] = f"{e.__class__.__name__}: {str(e)[:120]}"
    # Other job boards' alert emails: the same, tagged with the board's name
    if board_mail.boards():
        try:
            fetched += board_mail.collect(sheet, fetched, [c["name"] for c in sources.companies()], log=log)
        except gmail.GmailAuthError as e:
            errors["Job board alerts"] = str(e)
        except Exception as e:
            errors["Job board alerts"] = f"{e.__class__.__name__}: {str(e)[:120]}"
    stats = {"fetched": len(fetched), "errors": errors, "outside_country": 0, "new": 0, "dropped": 0,
             "below": 0, "alert": 0, "deferred": 0}
    candidates = []
    batch_ids = set()
    fresh = []
    for job in fetched:
        if job.job_id not in seen and job.job_id not in batch_ids:
            batch_ids.add(job.job_id)
            fresh.append(job)

    # Big-company sites list postings without a description: fetch it only for new jobs whose
    # title and date already pass. Jobs not fetched this run are not recorded; next run retries.
    need = [j for j in fresh if j.extra.get("detail") and not filters.cheap_drop(j)]
    got = sources.fill_details(need) if need else set()
    skip = {j.job_id for j in need} - got
    stats["deferred"] += len(skip)
    stats["details"] = len(got)

    for job in fresh:
        jid = job.job_id
        if jid in skip:
            batch_ids.discard(jid)
            continue
        loc = geo.locate(job.location, job.country, cache, budget=budget)
        if loc["flag"] == "defer":
            stats["deferred"] += 1
            continue
        if loc["drop_reason"].startswith("outside "):
            stats["outside_country"] += 1
            continue
        stats["new"] += 1
        row, reason = evaluate(job, loc, tiers)
        reason = reason or memory.drop(job.company)
        if not reason:
            memory.apply(row)
        twin = same_job_app(sheet, job.company, job.title, jid)
        if twin:                                     # never dropped for this: sent with a warning
            row["Reason"] = alerts.twin_note(twin)
        elif not reason:
            reason = sent.before(job.company, job.title, job.source_ref)
        if reason:
            stats["dropped"] += 1
            jobs_tab.append({**row, "Decision": "dropped", "Reason": reason})
            continue
        if row["Score"] < thr:
            stats["below"] += 1
            jobs_tab.append({**row, "Decision": "below_threshold", "Reason": f"score {row['Score']} < {thr}"})
            continue
        candidates.append((job, row, tiers.get(job.company.lower(), job.tier)))
        sent.add(job.company, job.title, row["First seen"][:10], job.source_ref)

    # Verify the survivors before alerting (live posting, company ATS link), in parallel
    alertable = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        verdicts = list(ex.map(lambda c: verify_live(c[0]), candidates))
    for (job, row, tier), v in zip(candidates, verdicts):
        if v == "retry":
            batch_ids.discard(row["Job ID"])     # not recorded: checked again next run
            stats["deferred"] += 1
            continue
        if v:
            stats["dropped"] += 1
            jobs_tab.append({**row, "Decision": "dropped", "Reason": v})
            continue
        if job.extra.get("verify_note"):
            row["Reason"] = "; ".join(filter(None, [row.get("Reason", ""), job.extra["verify_note"]]))
        alertable.append((job, row, tier))

    # Save new geocoding results
    for place, hit in cache.items():
        if place not in cache_before:
            sheet.t("Geo").append({"Place": place, "Lat": hit[0] if hit else "", "Lon": hit[1] if hit else "",
                                   "Distance km": round(geo.km(geo.center(), hit)) if hit else "",
                                   "Source": "nominatim", "Updated": now_iso(),
                                   "Country": hit[2] if hit and len(hit) > 2 else ""})

    alertable.sort(key=lambda x: (-int(x[1]["Score"]), x[2]))
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M") + "-" + uuid.uuid4().hex[:4]
    for rank, (job, row, tier) in enumerate(alertable, start=1):
        decision = "alerting" if rank <= BATCH_SIZE else "more_pending"
        jobs_tab.append({**row, "Decision": decision, "Batch ID": batch_id, "Rank": rank})
    stats["alert"] = len(alertable)
    sheet.flush()                                 # record before sending: a crash never loses a job

    if alertable and send:
        alerts.send_batch(sheet, batch_id)
    sheet.kv_set("Health", "last_find_run", f"{now_iso()} fetched={stats['fetched']} new={stats['new']} "
                 f"alerted={stats['alert']} errors={len(errors)} took={round(time.time() - t0)}s")
    stats["seconds"] = round(time.time() - t0, 1)
    log(f"find: {stats}")
    return stats
