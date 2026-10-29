"""The country the user searches in (settings.yaml location.country, an ISO 3166 code) and
the names of every other country, used to tell a local job from one abroad without a lookup."""
import re
from functools import lru_cache

import pycountry

from agent import config

# Everyday names that are not in the ISO list
ALIASES = {
    "US": ["usa", "u.s.", "u.s.a.", "united states of america", "america"],
    "GB": ["uk", "u.k.", "england", "scotland", "wales", "great britain", "britain"],
    "KR": ["south korea", "korea"],
    "RU": ["russia"],
    "IR": ["iran"],
    "VN": ["vietnam"],
    "TW": ["taiwan"],
    "CZ": ["czech republic"],
    "AE": ["uae", "dubai", "abu dhabi"],
    "NL": ["holland"],
    "TR": ["turkey"],
}
# Regions wider than one country: a job there is not a local job
REGIONS = ["emea", "apac", "latam", "europe", "americas", "north america", "south america", "asia",
           "africa", "middle east", "nordics", "dach", "benelux", "worldwide"]
SAME_NAME = {"US": ("georgia", "jersey")}
# "Austin, TX": a US state code at the end of a place
US_STATE = re.compile(r",\s*(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|"
                      r"PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s*$")


def code() -> str:
    return str((config.settings().get("location") or {}).get("country", "")).upper()


def _country(c: str):
    return pycountry.countries.get(alpha_2=c.upper()) if c else None


def name(c: str = None) -> str:
    """The country's common English name: 'United Kingdom', 'Germany'."""
    k = _country(c or code())
    return (getattr(k, "common_name", None) or k.name) if k else ""


def alpha3(c: str = None) -> str:
    k = _country(c or code())
    return k.alpha_3 if k else ""


def names(c: str) -> list:
    """Every lower-case name a posting may use for this country."""
    k = _country(c)
    if not k:
        return []
    out = {k.name.lower(), k.alpha_3.lower()}
    for attr in ("common_name", "official_name"):
        if getattr(k, attr, None):
            out.add(getattr(k, attr).lower())
    out |= set(ALIASES.get(c.upper(), []))
    return sorted(out, key=len, reverse=True)


@lru_cache(maxsize=4)
def _pattern(c: str) -> tuple:
    """(home names, every other country's names) as regexes."""
    home = names(c)
    others = []
    for k in pycountry.countries:
        if k.alpha_2 != c.upper():
            others += [n for n in names(k.alpha_2) if len(n) > 3 and n not in home]
    if c.upper() != "US":
        others += ["united states"]
    # names that are also places inside the home country ("Georgia" and "Jersey City" in the US)
    others = [n for n in others if n not in SAME_NAME.get(c.upper(), ())]
    # "New Mexico", "Northern Ireland", "New South Wales": not the country of that name
    words = lambda ns: re.compile(r"(?<![\w])(?<!new )(?<!northern )(?<!south )(" + "|".join(re.escape(n) for n in ns)  # noqa: E731
                                  + r")(?![\w])", re.I)
    return words(home) if home else None, words(sorted(set(others) | set(REGIONS), key=len, reverse=True))


def mentions_home(text: str) -> bool:
    home, _ = _pattern(code())
    return bool(home and home.search(text or ""))


def abroad(text: str) -> str:
    """The foreign country or region a place names ('' when none): 'Berlin, Germany' -> 'germany'."""
    home, other = _pattern(code())
    t = text or ""
    if home and home.search(t):
        # "Berlin, Germany | London, UK" lists the home country too: local, not abroad
        return ""
    m = other.search(t)
    if m:
        return m.group(1).lower()
    if code() != "US" and US_STATE.search(t):
        return "united states"
    return ""
