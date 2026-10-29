"""Check every link in a rendered CV before it is sent. render_cv.py runs this automatically.

A CV passes only if every link:
  1. is one of the links in PROFILE.yaml (no invented or edited URLs),
  2. sits on the right visible word (a project's link on its name, the GitHub profile on
     "GitHub", LinkedIn on "LinkedIn", the email on the address, a website on its address),
  3. is live and public for an anonymous visitor (GitHub repos and the profile are checked
     through the public GitHub API, without any login; other websites must answer).

LinkedIn blocks automated visitors, so its link is checked against PROFILE.yaml only: check it
by hand once.

Exit codes: 0 = all good; 3 = a link failed or could not be verified. Never send the CV on 3.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
CACHE = Path.home() / ".job-hunt-agent" / "link_cache.json"
CACHE_HOURS = 24
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; job-hunt-agent link check)"}


def allowed_links(profile: dict) -> set:
    links = set()
    for key, value in (profile["identity"].get("contact") or {}).items():
        if key == "email":
            links.add("mailto:" + value)
        elif isinstance(value, str) and value.startswith("http"):
            links.add(value)
    for p in profile.get("projects") or []:
        for key in ("url", "url_secondary"):
            if p.get(key):
                links.add(p[key])
    return links


def expected_words(url: str, profile: dict) -> list:
    """The visible text a link may sit on (any one of them)."""
    if url.startswith("mailto:"):
        return [url[len("mailto:"):]]
    if "linkedin.com" in url:
        return ["LinkedIn"]
    names = [p["name"].split()[0] for p in profile.get("projects") or []
             if url in (p.get("url"), p.get("url_secondary"))]
    return names + ["GitHub", url.split("//")[-1].rstrip("/")]


def is_public_on_github(url: str, cache: dict) -> tuple:
    hit = cache.get(url)
    if hit and time.time() - hit["t"] < CACHE_HOURS * 3600:
        return hit["ok"], hit["why"]
    m = re.fullmatch(r"https://github\.com/([^/]+)(?:/([^/]+))?", url)
    if not m:
        return False, "not a github.com user or repo URL"
    owner, repo = m.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}" if repo else f"https://api.github.com/users/{owner}"
    try:
        r = requests.get(api, timeout=10, headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        return False, f"could not reach GitHub ({e.__class__.__name__}); not verified"
    if r.status_code == 403 and "rate limit" in r.text.lower():
        return False, "GitHub rate limit; not verified, try again later"
    if r.status_code == 404:
        return False, "does not exist for an anonymous visitor (wrong address, renamed, deleted or private)"
    if r.status_code != 200:
        return False, f"could not verify (HTTP {r.status_code}, likely a network or proxy problem); not verified"
    data = r.json()
    if repo and data.get("private"):
        return False, "the repository is private"
    if data.get("html_url", "").lower() != url.lower():
        return False, f"canonical address is {data.get('html_url')} (renamed or moved)"
    ok, why = True, "public"
    cache[url] = {"ok": ok, "why": why, "t": time.time()}
    return ok, why


def is_live(url: str, cache: dict) -> tuple:
    """Any other website: it must answer without an error."""
    hit = cache.get(url)
    if hit and time.time() - hit["t"] < CACHE_HOURS * 3600:
        return hit["ok"], hit["why"]
    try:
        r = requests.get(url, timeout=10, headers=UA, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"could not reach it ({e.__class__.__name__}); not verified"
    if r.status_code >= 400:
        return False, f"answers HTTP {r.status_code}"
    cache[url] = {"ok": True, "why": "live", "t": time.time()}
    return True, "live"


def check(pdf_path: str, profile_path=None) -> list:
    import pdfplumber                      # only needed when checking a PDF (not in the cloud)
    profile = yaml.safe_load(Path(profile_path or HERE.parent / "PROFILE.yaml").read_text(encoding="utf-8"))
    allowed = allowed_links(profile)
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    problems = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for link in page.hyperlinks:
                url = link.get("uri") or ""
                box = (link["x0"] - 1, link["top"] - 2, link["x1"] + 1, link["bottom"] + 2)
                text = " ".join(w["text"] for w in page.within_bbox(box).extract_words())
                if url not in allowed:
                    problems.append(f"{url}: not a link from PROFILE.yaml")
                    continue
                want = expected_words(url, profile)
                if not any(w in text for w in want):
                    problems.append(f"{url}: sits on '{text}', expected one of {want}")
                if "github.com" in url:
                    ok, why = is_public_on_github(url, cache)
                elif url.startswith("http") and "linkedin.com" not in url:
                    ok, why = is_live(url, cache)
                else:
                    ok, why = True, ""
                if not ok:
                    problems.append(f"{url}: {why}")
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    except OSError:
        pass
    return problems


if __name__ == "__main__":
    found = check(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    for p in found:
        print("LINK PROBLEM:", p)
    print("links=ok" if not found else f"links=failed ({len(found)})")
    sys.exit(3 if found else 0)
