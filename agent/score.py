"""Rule-based fit score, 0-100, with every component stored so it explains itself.

  title      0-30  a preferred title (settings search.preferred_titles) 30, a good title
                   (search.good_titles) 24, other titles that name software work 14, a bare
                   "... Engineer" 4 (it passes only when the posting's skills match the profile)
  skills     0-25  posting skills found in PROFILE.yaml: strong 3.5, used 2; minus up to 5
                   for requested tech the profile lacks
  specialty  0-15  how much the posting is about the user's specialty (search.specialty_words),
                   plus room to grow for juniors
  seniority  0-10  graduate/junior 10, no years stated 8, 0-1 years 9, stretch 3
  location   0-10  close 10, medium 8, near the radius 6, unknown 4
  company    0-10  tier 1 10, tier 2 7, other 4
"""
import re
from functools import lru_cache

from agent import config, filters
from agent.truth_check import TECH_TERMS, skill_forms

GROWTH_TEXT = re.compile(r"\b(learn|mentor|grow|growth|junior|graduate|training|onboarding)\b", re.I)
# common spellings in postings for skills a profile may have (used only when the profile has the skill)
ALIASES = {"llm": "LLM APIs", "llms": "LLM APIs", "large language models": "LLM APIs", "openai": "LLM APIs",
           "rest": "REST APIs", "restful": "REST APIs", "node.js": "Node.js", "nodejs": "Node.js",
           "postgres": "PostgreSQL", "sklearn": "scikit-learn", "k8s": "Kubernetes", "golang": "Go",
           "pyspark": "Spark", "ci/cd": "CI/CD", "js": "JavaScript", "ts": "TypeScript"}


def _words_re(words: list):
    words = [str(w).strip() for w in words or [] if str(w).strip()]
    if not words:
        return None
    return re.compile(r"(?<![\w])(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")",
                      re.I)


@lru_cache(maxsize=1)
def _tiers() -> tuple:
    s = config.search()
    return (_words_re(s.get("preferred_titles")), _words_re(s.get("good_titles")),
            _words_re(s.get("specialty_words")))


@lru_cache(maxsize=1)
def _profile_skills() -> tuple:
    p = config.profile()
    strong, used = {}, {}
    for level, target in (("strong", strong), ("used", used)):
        for s in p["skills"].get(level) or []:
            for f in skill_forms(s):
                if len(f) >= 2 and f not in ("c", "r", "go"):
                    target[f] = s
    # one-letter languages are meaningless as bare words in text; match "C language" forms only
    for s in (p["skills"].get("strong") or []) + (p["skills"].get("used") or []):
        if s.strip().lower() in ("c", "r", "go"):
            (strong if s in (p["skills"].get("strong") or []) else used)[f"{s.strip().lower()} language"] = s
    have = {s.lower(): s for s in (p["skills"].get("strong") or []) + (p["skills"].get("used") or [])}
    for k, v in ALIASES.items():
        real = have.get(v.lower())
        if real:
            (strong if real in (p["skills"].get("strong") or []) else used)[k] = real
    return strong, used


def _find(term: str, text: str) -> bool:
    pat = r"(?<![\w+#.])" + re.escape(term) + r"(?![\w+#])"
    return re.search(pat, text, re.I) is not None


def skills_in(text: str) -> dict:
    strong, used = _profile_skills()
    found_strong = sorted({v for k, v in strong.items() if _find(k, text)})
    found_used = sorted({v for k, v in used.items() if _find(k, text)} - set(found_strong))
    corpus = " ".join(list(strong) + list(used))
    missing = sorted({t for t in TECH_TERMS if _find(t, text) and t.lower() not in corpus})
    return {"strong": found_strong, "used": found_used, "missing": missing}


def title_tier(title: str) -> int:
    preferred, good, _ = _tiers()
    if preferred and preferred.search(title or ""):
        return 30
    if good and good.search(title or ""):
        return 24
    return 14 if filters.SOFTWARE_TITLE.search(title or "") else 4


def score(job, sen: dict, loc: dict, tiers: dict) -> dict:
    title = job.title or ""
    text = f"{title}\n{job.description or ''}"
    parts = {"title": title_tier(title)}

    sk = skills_in(text)
    raw = 3.5 * len(sk["strong"]) + 2 * len(sk["used"])
    parts["skills"] = round(max(0, min(25, raw) - min(5, len(sk["missing"]))), 1)

    specialty = _tiers()[2]
    hits = len(specialty.findall(text)) if specialty else 0
    title_hit = bool(specialty and specialty.search(title))
    value = 15 if (title_hit or hits >= 4) else 11 if hits >= 1 else 8
    if filters.level() == "junior" and GROWTH_TEXT.search(text) and value < 15:
        value += 2
    parts["specialty"] = value

    if sen["stretch"]:
        parts["seniority"] = 3
    elif sen["graduate"]:
        parts["seniority"] = 10
    elif sen["years"] in (0, 1):
        parts["seniority"] = 9 if filters.level() == "junior" else 8
    else:
        parts["seniority"] = 8

    d = loc.get("km")
    from agent.geo import radius_km
    r = radius_km()
    parts["location"] = 4 if d in ("", None) else 10 if d <= 0.4 * r else 8 if d <= 0.7 * r else 6

    tier = tiers.get((job.company or "").lower(), job.tier or 3)
    parts["company"] = {1: 10, 2: 7}.get(tier, 4)

    total = round(sum(parts.values()))
    return {"score": max(0, min(100, total)), "parts": parts, "skills": sk}


def fit_lines(job, result: dict, sen: dict) -> tuple:
    """Two short lines on why it fits, and a gaps line, from the score components."""
    sk = result["skills"]
    p = result["parts"]
    first = []
    if p["title"] == 30:
        first.append("a preferred role" + (", in your specialty" if p.get("specialty") == 15 else ""))
    elif p["title"] == 24:
        first.append("a good-fit role")
    else:
        first.append("a software role")
    if sen.get("graduate"):
        first.append("open to graduates")
    elif sen.get("years", -1) < 0:
        first.append("no years required")
    else:
        first.append(f"asks for {sen['years']} years")
    matched = (sk["strong"] + sk["used"])[:6]
    second = "Matching skills: " + (", ".join(matched) if matched else "few direct matches")
    gaps = ", ".join(sk["missing"][:6])
    line = " · ".join(first)
    return line[:1].upper() + line[1:], second, gaps
