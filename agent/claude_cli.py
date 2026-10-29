"""Headless Claude Code calls (`claude -p`) for the worker on your computer.

Always passes an explicit model and effort from settings.yaml. Refuses Fable models and
"best". A plan, credits or usage-limit error is raised as PlanLimitError and is never
retried on another model.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from agent import config

FORBIDDEN_MODEL = re.compile(r"fable|best", re.I)
LIMIT_SIGNS = re.compile(r"credit|usage limit|rate limit|limit reached|quota|billing|subscription|"
                         r"plan does not|upgrade|out of extra usage|5-hour limit|weekly limit", re.I)
NO_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent",
            "Read", "Glob", "Grep"]


class ClaudeError(RuntimeError):
    pass


class PlanLimitError(ClaudeError):
    """The account hit a plan, credit or usage limit. Stop and tell the user; do not retry."""


def _model_and_effort() -> tuple:
    w = config.settings()["worker"]
    model, effort = str(w["model"]), str(w["effort"])
    if FORBIDDEN_MODEL.search(model):
        raise ClaudeError(f"model '{model}' is not allowed in the worker (Fable/best are billed to credits)")
    return model, effort


COUNT_FILE = config.STATE_DIR / "claude_calls.json"


def _count() -> None:
    """Calls per day (on the computer only), shown in /status so wasted calls are noticed."""
    from datetime import date
    try:
        data = json.loads(COUNT_FILE.read_text()) if COUNT_FILE.exists() else {}
    except ValueError:
        data = {}
    today = date.today().isoformat()
    data = dict(sorted(data.items())[-6:])
    data[today] = data.get(today, 0) + 1
    COUNT_FILE.write_text(json.dumps(data))


def calls_today() -> int:
    from datetime import date
    try:
        return json.loads(COUNT_FILE.read_text()).get(date.today().isoformat(), 0)
    except (OSError, ValueError):
        return 0


def ask(prompt: str, system: str = None, allow_web: bool = False, timeout: int = 900,
        files_dir: Path = None) -> str:
    """Send one prompt and return Claude's text answer. files_dir: a folder holding only the
    images Claude should look at; it becomes the working folder and Read may open files there."""
    model, effort = _model_and_effort()
    cwd = files_dir or config.STATE_DIR / "claude-cwd"   # an empty folder: no project files, no CLAUDE.md
    cwd.mkdir(parents=True, exist_ok=True)
    blocked = [t for t in NO_TOOLS if not (allow_web and t in ("WebFetch", "WebSearch"))
               and not (files_dir and t == "Read")]
    cmd = [shutil.which("claude") or "claude", "-p", "--model", model, "--effort", effort, "--output-format", "json",
           "--no-session-persistence", "--strict-mcp-config", "--disallowedTools", *blocked]
    if allow_web:
        cmd += ["--allowedTools", "WebSearch", "WebFetch"]
    if files_dir:
        cmd += ["--allowedTools", "Read(./**)"]
    if system:
        cmd += ["--append-system-prompt", system]
    try:
        _count()
    except OSError:
        pass
    try:
        r = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"claude -p timed out after {timeout}s") from None
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out.splitlines()[-1]) if out else {}
    except ValueError:
        data = {}
    text = data.get("result") or ""
    if r.returncode != 0 or data.get("is_error") or not data:
        detail = (text or r.stderr or out or "no output")[:400]
        if LIMIT_SIGNS.search(detail) or data.get("api_error_status") in (402, 429):
            raise PlanLimitError(detail)
        raise ClaudeError(f"claude -p failed (exit {r.returncode}): {detail}")
    used = list((data.get("modelUsage") or {}).keys())
    if any(FORBIDDEN_MODEL.search(m) for m in used):
        raise PlanLimitError(f"a forbidden model was used: {used}")
    return text


def extract_block(text: str, lang: str = "yaml") -> str:
    """Return the content of the first ```lang fenced block, or the whole text."""
    m = re.search(r"```(?:" + lang + r")?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()
