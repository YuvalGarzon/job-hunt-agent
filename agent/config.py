"""Project paths and settings.

The user's own files (created by setup.py, never committed): PROFILE.yaml, settings.yaml,
companies.yaml and base_cv.yaml in the project folder. Tests point these paths at examples/.
"""
import platform
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
PROFILE_PATH = ROOT / "PROFILE.yaml"
SETTINGS_PATH = ROOT / "settings.yaml"
COMPANIES_PATH = ROOT / "companies.yaml"
BASE_CONTENT = ROOT / "base_cv.yaml"              # the general CV, built by setup.py
FORMAT_EXAMPLE = EXAMPLES / "base_cv.example.yaml"  # the CV file format, used until base_cv.yaml exists
TEMPLATE_DIR = ROOT / "cv_template"
STATE_DIR = Path.home() / ".job-hunt-agent"       # local scratch: logs, claude working dir


class SetupNeeded(RuntimeError):
    pass


def _load(path: Path) -> dict:
    if not path.exists():
        raise SetupNeeded(f"{path.name} is missing: run `python setup.py` first")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def settings() -> dict:
    return _load(SETTINGS_PATH)


@lru_cache(maxsize=1)
def profile() -> dict:
    return _load(PROFILE_PATH)


def reload() -> None:
    """Read the files again on next use (after setup.py writes them)."""
    for fn in (settings, profile, tz):
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()
    from agent import country
    country._pattern.cache_clear()


def name() -> str:
    return profile()["identity"]["name"]


def first_name() -> str:
    return name().split()[0]


def cv_file_name() -> str:
    return f"{name()} CV.pdf"


def cv_rules() -> dict:
    return profile().get("cv_rules") or {}


def search() -> dict:
    return settings().get("search") or {}


@lru_cache(maxsize=1)
def tz() -> ZoneInfo:
    return ZoneInfo(settings().get("timezone") or "UTC")


def cv_root() -> Path:
    return ROOT / settings().get("cv_root", "CVs")


def where_saved(path) -> str:
    """Where a file is, as Telegram HTML: its name and the full path on the computer (tap to copy).
    On macOS, a file under Desktop or Documents is also shown as it appears in the iPhone Files
    app, for Macs that sync those folders to iCloud Drive."""
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    folder = path.parent if path.suffix else path
    try:
        rel = folder.relative_to(Path.home())
    except ValueError:
        rel = None
    esc = lambda x: str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    lines = [f"💾 File: <b>{esc(path.name)}</b>"] if path.suffix else []
    lines.append(f"📁 Path: <code>{esc(path)}</code>")
    if platform.system() == "Darwin" and rel is not None and rel.parts and rel.parts[0] in ("Desktop", "Documents"):
        lines.append(f"📱 iPhone (with iCloud Desktop sync): Files › iCloud Drive › {esc(' › '.join(rel.parts))}")
    return "\n".join(lines)
