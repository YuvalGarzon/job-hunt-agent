"""Truth check for a CV content file: everything on the CV must come from PROFILE.yaml.

    python -m agent.truth_check content.yaml [--related]

Exit code 0 = passes, 1 = problems found (printed one per line). A failing CV is fixed,
never shipped.

What it enforces:
  - header, education, languages and experience copied exactly from the profile
    (experience bullets only from the approved lists; the related-job variants only with --related),
  - every skill on the CV is a profile skill; groups are the profile's groups,
  - every project on the CV is a profile project, every number in its text appears in that
    project's facts, every link is the profile's link,
  - no banned words, no forbidden words (writing_rules.forbidden_words), never the home city,
  - no tech term that the profile does not contain,
  - the bold opening sentence claims only a title the profile says was held
    (cv_rules.titles_held),
  - with cv_rules.no_metrics: no results and no counts on the CV.
"""
import re
import sys
from pathlib import Path

import yaml

from agent import config

DEFAULT_TITLES = ("Software Engineer", "Software Developer")
# Claims about how or where something was done. Allowed only if the profile itself says it.
CONTEXT_CLAIMS = ["coursework", "course work", "production", "at scale", "large-scale", "years of",
                  "led ", "lead ", "mentored", "mentoring", "managed", "owned", "team lead",
                  "professional experience", "industry experience", "commercial", "customers",
                  "deployed to", "millions", "billions"]

# Common tech terms. If one shows up on a CV but nowhere in the profile, it was invented.
TECH_TERMS = [
    "TensorFlow", "Keras", "JAX", "Kubernetes", "K8s", "Terraform", "Ansible", "Helm", "Kafka",
    "RabbitMQ", "Redis", "Elasticsearch", "Cassandra", "DynamoDB", "BigQuery", "Snowflake",
    "Databricks", "Airflow", "dbt", "Flink", "Hadoop", "GCP", "Google Cloud", "Azure",
    "Lambda", "SageMaker", "Vertex AI", "MLflow", "Kubeflow", "LangChain", "LlamaIndex",
    "RAG", "vector database", "Pinecone", "FAISS", "Golang", "Rust", "Scala", "Kotlin",
    "Swift", "Objective-C", "C++", "C#", "Ruby", "PHP", "Perl", "MATLAB", "Julia", "Bash",
    "Angular", "Vue", "Svelte", "Django", "Flask", "Rails", "Laravel", ".NET", "ASP.NET",
    "GraphQL", "gRPC", "Protobuf", "MySQL", "SQLite", "Oracle", "SQL Server", "Jenkins",
    "GitLab", "GitHub Actions", "CircleCI", "ROS", "ROS2", "CUDA", "TensorRT", "OpenVINO",
    "ONNX", "Triton", "Tableau", "Power BI", "Looker", "Excel", "Selenium", "Cypress",
    "Playwright", "JUnit", "Jest", "Mocha", "Android", "iOS", "Flutter", "React Native",
    "RTOS", "FreeRTOS", "Verilog", "VHDL", "FPGA", "Embedded Linux", "Yocto", "Qt", "OpenGL",
    "Unreal", "SLAM", "LiDAR", "Kalman", "Spark Streaming", "Pandas UDF", "Microservices",
    "Agile", "Scrum", "Jira", "fine-tuning", "fine-tuned", "RLHF", "LoRA", "diffusion",
    "Stable Diffusion", "GPT-4", "BERT", "LLaMA", "Llama", "transformer models",
]


def load_profile() -> dict:
    return yaml.safe_load(Path(config.PROFILE_PATH).read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def skill_forms(skill: str) -> set:
    """All the ways a profile skill may be written on a CV."""
    s = _norm(skill)
    forms = {s}
    base = re.sub(r"\s*\(.*?\)", "", s).strip()
    forms.add(base)
    inner = re.findall(r"\((.*?)\)", s)
    for part in inner:
        forms |= {p.strip() for p in part.split(",") if p.strip()}
    for f in list(forms):
        if " / " in f:
            forms |= {p.strip() for p in f.split(" / ")}
    return {f for f in forms if f}


def profile_skill_forms(profile: dict) -> set:
    out = set()
    for level in ("strong", "used"):
        for s in profile["skills"].get(level) or []:
            out |= skill_forms(s)
    return out


def project_text(project: dict) -> str:
    return " ".join([project.get("name", ""), project.get("context", "") or "", " ".join(project.get("stack", [])),
                     " ".join(project.get("facts", []))])


def profile_corpus(profile: dict) -> str:
    """All text in the profile (except the private part), used to decide whether a term is known."""
    p = {k: v for k, v in profile.items() if k != "private_never_on_cv"}
    return yaml.safe_dump(p, allow_unicode=True).lower()


NUM_RE = re.compile(r"(?<![A-Za-z])(\d[\d,]*(?:\.\d+)?)(K\+|k\+)?")


def numbers_in(text: str) -> list:
    out = []
    for m in NUM_RE.finditer(text):
        raw, k = m.group(1), m.group(2)
        raw = raw.rstrip(",")
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        out.append((m.group(0), val, bool(k)))
    return out


def number_allowed(token: tuple, fact_numbers: list) -> bool:
    _, val, is_k = token
    if is_k:   # "190K+" is allowed only when a fact has a number in [190000, 191000)
        return any(int(n // 1000) == int(val) for n in fact_numbers if n >= 1000)
    return any(abs(val - n) < 1e-9 for n in fact_numbers)


def fact_numbers(text: str) -> list:
    return [v for _, v, _ in numbers_in(text)]


SUMMARY_MAX_WORDS = 65
# metric names match only in their own capitals, so the plain word "map" (a Leaflet map) passes
RESULTS = re.compile(r"\d+(?:\.\d+)?\s*%|±|\b(?:MAE|RMSE|mAP|ROC|AUC|F1)\b|(?i:\b(?:accuracy|precision|recall)\b)")
COUNTS = re.compile(r"\b\d[\d,.]*\s*K?\+?\s+(?:[\w-]+\s+){0,2}?(?:rows|tests|videos|trials|samples|images|"
                    r"records|seasons|frames|examples|files|lines of code)\b", re.I)


def claims(phrase: str, text: str) -> bool:
    """The claim as whole words ("led" is not inside "labeled")."""
    return re.search(r"(?<![\w])" + re.escape(phrase.strip()) + r"(?![\w])", text) is not None


def rules(profile: dict) -> dict:
    return profile.get("cv_rules") or {}


def titles_held(profile: dict) -> tuple:
    return tuple(rules(profile).get("titles_held") or DEFAULT_TITLES)


def forbidden_words(profile: dict) -> list:
    """Words never written anywhere: the profile's list plus the home city."""
    words = [str(w).lower() for w in (profile.get("writing_rules") or {}).get("forbidden_words") or []]
    home = (profile.get("private_never_on_cv") or {}).get("home_city")
    return words + ([str(home).lower()] if home else [])


def never_write(profile: dict) -> list:
    """Phrases an experience entry must never reveal (experience[].never_write)."""
    return [str(n) for e in profile.get("experience") or [] for n in e.get("never_write") or []]


def _project(item: dict, by_name: dict, by_url: dict):
    return by_name.get(_norm(item.get("name", ""))) or (by_url.get(item["url"]) if item.get("url") else None)


def _check_project(proj: dict, key: str, lo: int, hi: int, by_name: dict, by_url: dict, allowed_skills: set,
                   problems: list):
    """A full project on the CV (main or second): the profile's project, its stack, its numbers."""
    mp = _project(proj, by_name, by_url)
    if not mp:
        problems.append(f"{key} '{proj.get('name')}' is not a project in the profile")
        return None
    if proj.get("name") != mp["name"]:
        problems.append(f"{key} name must be '{mp['name']}'")
    if (proj.get("url") or None) != (mp.get("url") or None):
        problems.append(f"{key} url must be exactly the profile's: {mp.get('url')}")
    if proj.get("context") and proj.get("context") != mp.get("context"):
        problems.append(f"{key} context must be '{mp.get('context')}'")
    stack_forms = set()
    for s in mp.get("stack", []):
        stack_forms |= skill_forms(s)
    for s in proj.get("stack", []):
        if _norm(s) not in stack_forms and _norm(s) not in allowed_skills:
            problems.append(f"{key} stack item '{s}' is not in that project's stack")
    nb = len(proj.get("bullets", []))
    if not lo <= nb <= hi:
        problems.append(f"{key} must have {lo} to {hi} bullets, has {nb}")
    nums = fact_numbers(project_text(mp))
    for b in proj.get("bullets", []):
        for tok in numbers_in(b):
            if not number_allowed(tok, nums):
                problems.append(f"number '{tok[0]}' in a {key} bullet is not in its facts: '{b[:60]}'")
    return mp


def _check_experience(exp: list, p: dict, related: bool, problems: list) -> None:
    exp_profile = p.get("experience") or []
    if len(exp) != len(exp_profile):
        problems.append(f"experience must have {len(exp_profile)} entries, newest first, as in the profile")
    for got, want in zip(exp, exp_profile):
        if got.get("dates") != want["dates"] or got.get("role") != want["role"]:
            problems.append(f"experience '{got.get('role')}' dates/role differ from the profile")
        orgs = {want["org"]}
        if related and want.get("org_related"):
            orgs.add(want["org_related"])
        if got.get("org") not in orgs:
            problems.append(f"experience org '{got.get('org')}' not allowed here (allowed: {sorted(orgs)})")
        approved = list(want.get("bullets_approved", [])) + list(want.get("bullets_short", []))
        if related and want.get("bullets_related"):
            approved += want["bullets_related"]
        if want.get("bullets_short") and not related and \
                (len(got.get("bullets", [])) != 1 or got["bullets"][0] not in want["bullets_short"]):
            problems.append(f"experience '{want['role']}' (not a related job): exactly one bullet, "
                            f"the short one: '{want['bullets_short'][0]}'")
        for b in got.get("bullets", []):
            if b not in approved:
                problems.append(f"experience bullet is not an approved bullet: '{b[:70]}'")
        if not got.get("bullets"):
            problems.append(f"experience '{got.get('role')}' has no bullets")
        if want.get("bullets_related") and got.get("bullets"):
            uses_related = any(b in want["bullets_related"] for b in got["bullets"])
            uses_default = any(b in want.get("bullets_approved", []) + want.get("bullets_short", [])
                               and b not in want["bullets_related"] for b in got["bullets"])
            if uses_related and uses_default:
                problems.append(f"experience '{want['role']}' mixes its default bullets with its related-job bullets")


def _check_education(edu: list, p: dict, problems: list) -> None:
    want_all = p.get("education") or []
    if isinstance(want_all, dict):
        want_all = [want_all]
    if isinstance(edu, dict):
        edu = [edu]
    if len(edu or []) != len(want_all):
        problems.append(f"education must have {len(want_all)} entries, as in the profile")
    for got, want in zip(edu or [], want_all):
        for key in ("dates", "degree", "track", "school"):
            if (got.get(key) or None) != (want.get(key) or None):
                problems.append(f"education.{key} differs from the profile")
        if not want.get("studied_areas"):
            if got.get("studied"):
                problems.append("education.studied is not allowed: the profile has no studied_areas")
            continue
        studied = (got.get("studied") or "").rstrip(".")
        areas = [a.strip() for a in re.split(r",\s*|\s+and\s+", studied) if a.strip()]
        allowed_areas = {_norm(a) for a in want["studied_areas"]}
        for a in areas:
            if _norm(a) not in allowed_areas:
                problems.append(f"studied area '{a}' is not in studied_areas")
        if areas and not 3 <= len(areas) <= 5:
            problems.append(f"Studied line must name 3 to 5 areas, has {len(areas)}")


def check(content: dict, related: bool = False, profile: dict = None) -> list:
    """related: a job related to the experience the profile marks with bullets_related
    (cv_rules.related_job_words): those entries may use their longer bullets and org_related."""
    p = profile or load_profile()
    problems = []
    ident = p["identity"]
    r = rules(p)

    # Header
    if content.get("name") != ident["name"]:
        problems.append("name differs from the profile")
    if content.get("headline"):
        problems.append("headline must be null (no title line under the name)")
    for key, val in (ident.get("contact") or {}).items():
        if (content.get("contact") or {}).get(key) != val:
            problems.append(f"contact.{key} differs from the profile")
    if set((content.get("contact") or {}).keys()) - set((ident.get("contact") or {}).keys()):
        problems.append("contact has fields that are not in the profile")
    if content.get("languages") != ident.get("languages"):
        problems.append("languages line differs from the profile")

    # Summary lead: a title the profile says was held, never an invented one
    lead = content.get("summary_lead") or ""
    held = titles_held(p)
    if not lead:
        problems.append("summary_lead is missing")
    elif not lead.startswith(held):
        problems.append(f"summary_lead must open with a title held {held}: '{lead[:60]}'")
    words = len(f"{lead} {content.get('summary') or ''}".split())
    most = int(r.get("summary_max_words") or SUMMARY_MAX_WORDS)
    if words > most:
        problems.append(f"the summary (lead + text) is {words} words: keep it to {most} or fewer")

    # Skills
    allowed_skills = profile_skill_forms(p)
    labels = [g.get("label") for g in content.get("skills", [])]
    groups = p["skills"].get("skill_groups_on_cv") or []
    if sorted(labels) != sorted(groups):
        problems.append(f"skill groups must be exactly {groups}, got {labels}")
    seen_skills = set()
    for g in content.get("skills", []):
        for item in g.get("items", []):
            if _norm(item) not in allowed_skills:
                problems.append(f"skill '{item}' is not in PROFILE.yaml skills")
            if _norm(item) in seen_skills:
                problems.append(f"skill '{item}' is listed twice")
            seen_skills.add(_norm(item))

    exp = content.get("experience") or []
    _check_experience(exp, p, related, problems)

    # Projects
    by_name = {_norm(pr["name"]): pr for pr in p.get("projects") or []}
    by_url = {pr["url"]: pr for pr in p.get("projects") or [] if pr.get("url")}
    main = content.get("main_project") or {}
    mp = _check_project(main, "main_project", 3, 5, by_name, by_url, allowed_skills, problems) if main else None
    if not main and p.get("projects"):
        problems.append("main_project is missing")
    second = content.get("second_project") or {}
    sp = _check_project(second, "second_project", 2, 4, by_name, by_url, allowed_skills, problems) if second else None
    if mp and sp and mp["name"] == sp["name"]:
        problems.append("second_project repeats the main project")
    shown = {pr["name"] for pr in (mp, sp) if pr}

    also = content.get("also_built") or []
    if len(also) > 3:
        problems.append("Also built may have at most 3 projects")
    seen = set()
    for item in also:
        pr = _project(item, by_name, by_url)
        if not pr:
            problems.append(f"Also built '{item.get('name') or item.get('url')}' is not a project in the profile")
            continue
        if (item.get("url") or None) != (pr.get("url") or None):
            problems.append(f"Also built {pr['name']}: the url must be exactly the profile's: {pr.get('url')}")
        if pr["name"] in shown:
            problems.append(f"Also built repeats {pr['name']}, which is already a full project")
        if pr["name"] in seen:
            problems.append(f"Also built lists {pr['name']} twice")
        seen.add(pr["name"])
        nums = fact_numbers(project_text(pr))
        for tok in numbers_in(item.get("text", "")):
            if not number_allowed(tok, nums):
                problems.append(f"number '{tok[0]}' in Also built is not in {pr['name']} facts")
        if len(item.get("text", "")) > 110:
            problems.append(f"Also built line too long (keep it a few words): '{item['text'][:50]}...'")
    if r.get("no_metrics"):
        for text in ([content.get("summary") or ""] + main.get("bullets", []) + second.get("bullets", [])
                     + [i.get("text", "") for i in also]):
            for c in COUNTS.finditer(text):
                problems.append(f"no counts of data or tests ('{c.group(0)}'): say what was built and with what tools")
            for m in RESULTS.finditer(text):
                problems.append(f"no results or metrics ('{m.group(0)}' in '{text[:50]}...'): say what was built "
                                "and with which tools")

    _check_education(content.get("education") or [], p, problems)

    # Free text: summary, numbers, banned words, forbidden words, unknown tech terms
    all_text = yaml.safe_dump(content, allow_unicode=True)
    free_text = " ".join([lead, content.get("summary") or ""])
    all_fact_nums = []
    for pr in p.get("projects") or []:
        all_fact_nums += fact_numbers(project_text(pr))
    for tok in numbers_in(free_text):
        if not number_allowed(tok, all_fact_nums):
            problems.append(f"number '{tok[0]}' in the summary is not in any project's facts")
    low = all_text.lower()
    for w in (p.get("writing_rules") or {}).get("banned_words") or []:
        if re.search(r"\b" + re.escape(w.lower()) + r"\b", low):
            problems.append(f"banned word: '{w}'")
    for w in forbidden_words(p):
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            problems.append(f"forbidden on a CV: '{w}'")
    for never in never_write(p):
        if never.lower() in low:
            problems.append(f"never write: '{never}'")
    corpus = profile_corpus(p)
    visible_low = " ".join([free_text, " ".join(main.get("bullets", []) + second.get("bullets", [])),
                            " ".join(i.get("text", "") for i in also)]).lower()
    for phrase in CONTEXT_CLAIMS:
        if claims(phrase, visible_low) and not claims(phrase, corpus):
            problems.append(f"claim '{phrase.strip()}' is not supported by the profile")
    visible = " ".join([free_text, " ".join(b for e in exp for b in e.get("bullets", [])),
                        " ".join(main.get("bullets", []) + second.get("bullets", [])),
                        " ".join(i.get("text", "") for i in also)])
    for term in TECH_TERMS:
        pat = r"(?<![\w+#.])" + re.escape(term) + r"(?![\w+#])"
        if re.search(pat, visible) and not re.search(pat, corpus, flags=re.I):
            problems.append(f"tech term '{term}' is not in the profile")
    return problems


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit("usage: python -m agent.truth_check content.yaml [--related]")
    content = yaml.safe_load(Path(args[0]).read_text(encoding="utf-8"))
    problems = check(content, related="--related" in sys.argv)
    for pr in problems:
        print("TRUTH PROBLEM:", pr)
    print("truth=ok" if not problems else f"truth=failed ({len(problems)})")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()


def check_text(text: str, job_text: str = "", profile: dict = None) -> list:
    """Free text written about the user (cover letter, recruiter message, follow-up): the same
    truth rules as the CV. Terms that appear in the job posting may be mentioned (they describe
    the job, not the user); numbers and claims about the user must come from the profile."""
    p = profile or load_profile()
    problems = []
    low, job_low = text.lower(), (job_text or "").lower()
    corpus = profile_corpus(p)
    for w in (p.get("writing_rules") or {}).get("banned_words") or []:
        if re.search(r"\b" + re.escape(w.lower()) + r"\b", low):
            problems.append(f"banned word: '{w}'")
    for w in forbidden_words(p):
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            problems.append(f"forbidden: '{w}'")
    allowed_nums = fact_numbers(corpus) + fact_numbers(job_low)
    for tok in numbers_in(text):
        if not number_allowed(tok, allowed_nums):
            problems.append(f"number '{tok[0]}' is not in the profile")
    for phrase in CONTEXT_CLAIMS:
        if claims(phrase, low) and not claims(phrase, corpus) and not claims(phrase, job_low):
            problems.append(f"claim '{phrase.strip()}' is not supported by the profile")
    for term in TECH_TERMS:
        pat = r"(?<![\w+#.])" + re.escape(term) + r"(?![\w+#])"
        if re.search(pat, text) and not re.search(pat, corpus, flags=re.I) and not re.search(pat, job_low, flags=re.I):
            problems.append(f"tech term '{term}' is not in the profile")
    for never in never_write(p):
        if never.lower() in low:
            problems.append(f"never write: '{never}'")
    return list(dict.fromkeys(problems))
