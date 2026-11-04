"""The normalized job record and its stable ID."""
import hashlib
import html as htmllib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


@dataclass
class Job:
    source: str               # greenhouse | lever | comeet | ashby | linkedin | ...
    source_ref: str           # enough to fetch the posting again, e.g. "greenhouse:acme:8239378"
    company: str
    title: str
    location: str
    url: str
    posted: str = ""          # ISO date the posting was published (or last updated if unknown)
    description: str = ""     # plain text
    country: str = ""         # ISO country code when the source gives one
    employment: str = ""      # e.g. Full-time, Part-time, Intern (when the source gives one)
    workplace: str = ""       # on-site | hybrid | remote (when known)
    tier: int = 3
    extra: dict = field(default_factory=dict)
    id_url: str = ""          # the link the job's ID was first made from, when the shown link changed since

    def __post_init__(self):
        self.url = clean_url(self.url)
        self.id_url = clean_url(self.id_url)

    @property
    def job_id(self) -> str:
        return job_id(self.company, self.title, self.id_url or self.url)


def norm_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[‐-―]", "-", s)
    s = re.sub(r"[^\w֐-׿+#./ -]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


TRACKING = re.compile(r"^(utm_|gh_src|source|src|ref|lever-|trk|refid|trackingid|fbclid|gclid|igshid|mc_|_hs|mkt_tok)",
                      re.I)
# Click trackers added by Facebook, Google, Instagram and mail tools: removed from every stored link
CLICK_IDS = re.compile(r"^(utm_|fbclid|gclid|igshid|mc_|_hs|mkt_tok)", re.I)


def _stamp(k: str, v: str) -> bool:
    """A cache-busting time stamp (Comeet adds ?t=<ms> on every read): the same job, a new link."""
    return k == "t" and bool(re.fullmatch(r"\d{10,13}", v))


def clean_url(url: str) -> str:
    """The link without click-tracking parameters; everything else stays as it was."""
    if not url or "?" not in url:
        return url
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not CLICK_IDS.match(k) and not _stamp(k, v)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING.match(k) and not _stamp(k, v)]
    path = re.sub(r"/+$", "", p.path)
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), path, urlencode(q), ""))


def job_id(company: str, title: str, url: str) -> str:
    key = "|".join([norm_text(company), norm_text(title), canonical_url(url)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = htmllib.unescape(s)
    s = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()
