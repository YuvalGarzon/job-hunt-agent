"""Learning from Skip reasons.

After the user presses ⏭ Skip, the card offers a reason (never asked otherwise). The reason is
kept in the Jobs tab ("Skip reason") and later searches use it:
- 🏢 Company: no more jobs from that company (Settings "blocked_companies"; ↩️ undoes it).
- 📍 Location: jobs in that city score 8 lower.
- 🎓 Too senior / 🧭 Wrong field: a job whose title is close to a title skipped for that
  reason scores 10 lower (it can still be sent when it scores high enough).
- 🤷 Other: only recorded.
"""
import json
import re
from datetime import datetime, timedelta

from agent.jobs import norm_text
from agent.sheet import TZ, Sheet, now_iso

REASONS = {"sen": "🎓 Too senior", "field": "🧭 Wrong field", "loc": "📍 Location", "co": "🏢 Company",
           "other": "🤷 Other"}
CITY_PENALTY = 8
TITLE_PENALTY = 10
LOOKBACK_DAYS = 90
STOP = {"and", "or", "the", "of", "for", "to", "in", "a", "at", "with", "ii", "i", "iii", "jr", "junior",
        "engineer", "developer", "-", "/", "&"}


def reason_buttons(job_id: str) -> list:
    codes = list(REASONS)
    return [[(REASONS[c], f"skr:{c}:{job_id}") for c in codes[:3]],
            [(REASONS[c], f"skr:{c}:{job_id}") for c in codes[3:]]]


def blocked(sheet: Sheet) -> list:
    """Companies never sent: the ones skipped with 🏢, and settings.yaml search.avoid_companies."""
    from agent import config
    try:
        keys = json.loads(sheet.kv_get("Settings", "blocked_companies", "") or "[]")
    except ValueError:
        keys = []
    return keys + [_key(c) for c in config.search().get("avoid_companies") or [] if _key(c)]


def _key(company: str) -> str:
    from agent.sources.linkedin import company_key
    return company_key(company)


def block(sheet: Sheet, company: str, on: bool = True) -> None:
    try:
        keys = json.loads(sheet.kv_get("Settings", "blocked_companies", "") or "[]")
    except ValueError:
        keys = []
    keys = [k for k in keys if k != _key(company)]
    if on and _key(company) and company != "?":
        keys.append(_key(company))
    sheet.kv_set("Settings", "blocked_companies", json.dumps(keys, ensure_ascii=False))


def record(sheet: Sheet, job: dict, code: str) -> None:
    sheet.t("Jobs").update(job, **{"Skip reason": code, "Updated": now_iso()})
    if code == "co":
        block(sheet, job["Company"])


def words(title: str) -> set:
    return {w for w in re.split(r"[\s,()|/·]+", norm_text(title)) if w and w not in STOP}


def close_titles(a: set, b: set) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6


class Memory:
    """What the skips taught, read once per search run."""

    def __init__(self, sheet: Sheet):
        since = (datetime.now(TZ) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        rows = [r for r in sheet.t("Jobs").rows
                if r.get("Skip reason") and r["User action"] == "skipped" and r["Updated"][:10] >= since]
        self.blocked = set(blocked(sheet))
        self.cities = {r["City"] for r in rows if r["Skip reason"] == "loc" and r["City"]}
        self.titles = [(words(r["Title"]), r["Skip reason"]) for r in rows if r["Skip reason"] in ("sen", "field")]

    def drop(self, company: str) -> str:
        return "you skipped this company (🏢)" if _key(company) in self.blocked else ""

    def apply(self, row: dict) -> None:
        """Lower the score of a scored row by what was skipped before; notes why in Score parts."""
        cut = []
        if row.get("City") and row["City"] in self.cities:
            cut.append(("city", CITY_PENALTY))
        w = words(row["Title"])
        for skipped, why in self.titles:
            if close_titles(w, skipped):
                cut.append((why, TITLE_PENALTY))
                break
        if cut:
            row["Score"] = max(0, row["Score"] - sum(p for _, p in cut))
            row["Score parts"] = f"{row['Score parts']} " + " ".join(f"skipped_{k}=-{p}" for k, p in cut)
