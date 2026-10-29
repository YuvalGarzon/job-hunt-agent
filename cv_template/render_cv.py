"""Render a CV content file (YAML) into a one-page PDF.

Usage:
    python render_cv.py content.yaml "CVs/General CV/<Your Name> CV.pdf" [--profile PROFILE.yaml]

Exit codes:
  0 = one page and every link verified (see check_links.py): the CV may be sent.
  2 = more than one page: shorten the content and render again (never trims by itself).
  3 = a link is wrong, misplaced, private, broken or could not be verified: do not send.
"""
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pypdf import PdfReader
from weasyprint import HTML

HERE = Path(__file__).resolve().parent


def profile_path() -> Path:
    if "--profile" in sys.argv:
        return Path(sys.argv[sys.argv.index("--profile") + 1])
    return HERE.parent / "PROFILE.yaml"


def render(content_path: str, out_path: str, profile: dict = None) -> int:
    profile = profile or yaml.safe_load(profile_path().read_text(encoding="utf-8"))
    file_name = f"{profile['identity']['name']} CV.pdf"
    if Path(out_path).name != file_name:
        sys.exit(f"The CV file must be named exactly '{file_name}', got '{Path(out_path).name}'")
    data = yaml.safe_load(Path(content_path).read_text(encoding="utf-8"))
    if not data.get("summary_lead"):
        sys.exit("summary_lead (the bold opening sentence of the summary) is required")
    if isinstance(data.get("education"), dict):
        data["education"] = [data["education"]]
    env = Environment(loader=FileSystemLoader(HERE), autoescape=select_autoescape(["html", "j2"]))
    show = (profile.get("cv_rules") or {}).get("show_context") or []
    html = env.get_template("cv.html.j2").render(show_context=show, **data)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(HERE)).write_pdf(out)
    return len(PdfReader(out).pages)


if __name__ == "__main__":
    pages = render(sys.argv[1], sys.argv[2])
    print(f"pages={pages}")
    if pages != 1:
        sys.exit(2)
    from check_links import check
    problems = check(sys.argv[2], profile_path())
    for p in problems:
        print("LINK PROBLEM:", p)
    print("links=ok" if not problems else f"links=failed ({len(problems)})")
    sys.exit(3 if problems else 0)
