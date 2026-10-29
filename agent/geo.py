"""Location: is a job in the user's country and within the search radius? Straight-line
distance from the home point.

The home point and radius come from settings.yaml (location.home, set by setup.py from the
home city in PROFILE.yaml, which is never written anywhere). A place that names another country
is outside at once; any other place is geocoded once with OpenStreetMap Nominatim (1 request
per second, identified User-Agent, as its usage policy asks) and cached in the Sheet's Geo tab.
"""
import math
import re
import time

import requests

from agent import config, country

DEFAULT_RADIUS_KM = 50
UA = "job-hunt-agent/1.0 (personal job search tool)"
GENERIC = re.compile(r"\b(remote|hybrid|on-?site|office|offices|anywhere|global|distributed|multiple locations|"
                     r"based|various|flexible|home|wfh|tbd|n/?a|hq|headquarters|district|region|area|metro)\b"
                     r"|[;()|\-–—]", re.I)
SPLIT = re.compile(r"\s*[|;/•]\s*|\s+or\s+|\s+and\s+", re.I)


def radius_km() -> float:
    return float((config.settings().get("location") or {}).get("radius_km") or DEFAULT_RADIUS_KM)


def center() -> tuple:
    home = (config.settings().get("location") or {}).get("home")
    if not home or len(home) != 2:
        raise config.SetupNeeded("settings.yaml has no location.home: run `python setup.py`")
    return float(home[0]), float(home[1])


def km(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


_last_nominatim = [0.0]


def geocode_nominatim(place: str, only_country: str = "") -> tuple:
    """(lat, lon, country code) of a town or city, or None."""
    if len(re.sub(r"\W", "", place)) < 4:                # an abbreviation, never a town name
        return None
    wait = 1.1 - (time.time() - _last_nominatim[0])
    if wait > 0:
        time.sleep(wait)
    _last_nominatim[0] = time.time()
    params = {"q": place, "format": "json", "limit": 1, "addressdetails": 1, "featuretype": "settlement"}
    if only_country:
        params["countrycodes"] = only_country.lower()
    r = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers={"User-Agent": UA},
                     timeout=10)
    r.raise_for_status()
    res = r.json()
    if not res:
        return None
    cc = ((res[0].get("address") or {}).get("country_code") or "").upper()
    return float(res[0]["lat"]), float(res[0]["lon"]), cc


def geocode_home(city: str, country_code: str) -> tuple:
    """Used once by setup.py: the home city's coordinates."""
    hit = geocode_nominatim(city, country_code)
    return (hit[0], hit[1]) if hit else None


def _place(part: str) -> str:
    """The town in one place string: 'Springfield, Some District' -> 'Springfield'."""
    text = part
    for n in country.names(country.code()):
        text = re.sub(r"(?<![\w])" + re.escape(n) + r"(?![\w])", " ", text, flags=re.I)
    for seg in text.split(","):
        seg = re.sub(r"\s+", " ", GENERIC.sub(" ", seg)).strip(" .")
        if seg:
            return seg
    return ""


def _one(part: str, job_country: str, geo_cache: dict, allow_network: bool, budget: list) -> dict:
    home_code, home_name = country.code(), country.name()
    out = {"city": "", "km": "", "flag": "", "drop_reason": ""}
    foreign = country.abroad(part)
    if foreign:
        return {**out, "drop_reason": f"outside {home_name} ({foreign})"}
    in_home = job_country == home_code or country.mentions_home(part)
    place = _place(part)
    if not place:
        return {**out, "flag": "location?"}
    key = place.lower()
    hit = (geo_cache or {}).get(key, "miss")
    if hit == "miss":
        if not allow_network or geo_cache is None or (budget is not None and budget[0] <= 0):
            return {**out, "flag": "location?" if in_home else "defer"}
        if budget is not None:
            budget[0] -= 1
        try:
            hit = geocode_nominatim(place, home_code if in_home else "")
        except requests.RequestException:
            return {**out, "flag": "defer"}
        geo_cache[key] = hit
    if not hit:
        if in_home:
            return {**out, "flag": "location?"}
        return {**out, "drop_reason": f"outside {home_name} ({part.strip()[:40]})"}
    if len(hit) > 2 and hit[2] and hit[2] != home_code:
        return {**out, "drop_reason": f"outside {home_name} ({place})"}
    d = round(km(center(), hit))
    if d > radius_km():
        return {**out, "city": place, "km": d, "drop_reason": f"{place} is {d} km away"}
    return {**out, "city": place, "km": d}


def locate(location: str, job_country: str = "", geo_cache: dict = None, allow_network: bool = True,
           budget: list = None) -> dict:
    """Decide where a job is.

    Returns {"city", "km", "flag", "drop_reason"}:
      flag "" = known and within the radius; "location?" = in the home country (or no place
      given) but the town is unknown; "defer" = needs a geocoding lookup that this run has no
      budget for. drop_reason is set when the job is outside the country or beyond the radius.
    A posting that lists several places counts its nearest local one.
    """
    job_country = (job_country or "").upper()
    if job_country and job_country != country.code():
        return {"city": "", "km": "", "flag": "", "drop_reason": f"outside {country.name()} ({job_country})"}
    parts = [p for p in SPLIT.split(location or "") if p.strip()]
    if not parts:
        return {"city": "", "km": "", "flag": "location?", "drop_reason": ""}
    results = [_one(p, job_country, geo_cache, allow_network, budget) for p in parts[:6]]
    near = [r for r in results if not r["drop_reason"] and not r["flag"]]
    if near:
        return min(near, key=lambda r: r["km"])
    for flag in ("location?", "defer"):
        if any(r["flag"] == flag for r in results):
            return next(r for r in results if r["flag"] == flag)
    far = [r for r in results if r["km"] != ""]
    return min(far, key=lambda r: r["km"]) if far else results[0]
