"""The truth check, on the made-up Alex Morgan (examples/)."""
import copy

import pytest
import yaml

from agent import config
from agent.truth_check import check, check_text

BASE = yaml.safe_load((config.EXAMPLES / "base_cv.example.yaml").read_text(encoding="utf-8"))
SHORT = "Maintained warehouse picking robots."
RELATED = ["Maintained a fleet of warehouse picking robots and diagnosed sensor and drive faults.",
           "Trained new technicians on robot safety procedures and fault reports."]


@pytest.fixture
def cv():
    return copy.deepcopy(BASE)


def base():
    return copy.deepcopy(BASE)


def has(problems, text):
    return any(text in p for p in problems)


def test_base_passes(cv):
    assert check(cv) == []


def test_invented_skill(cv):
    cv["skills"][0]["items"].append("Kubernetes")
    assert has(check(cv), "skill 'Kubernetes'")


def test_skill_alias_from_parentheses_allowed(cv):
    p = yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8"))
    p["skills"]["used"].append("Hugging Face (transformers, Florence-2)")
    cv["skills"][1]["items"].append("transformers")
    assert check(cv, profile=p) == []


def test_invented_number_in_bullet(cv):
    cv["main_project"]["bullets"][1] += " Compared 12 models."
    assert has(check(cv), "number '12'")


def test_k_rounding_follows_the_facts(cv):
    cv["main_project"]["bullets"][1] += " Trained on 200K+ records."
    assert has(check(cv), "200K+")


def test_invented_link(cv):
    cv["also_built"][0]["url"] = "https://github.com/alex-morgan-example/other"
    assert has(check(cv), "the url must be exactly the profile's")


def test_unknown_project_not_allowed(cv):
    cv["also_built"].append({"name": "Hackathon Bot", "text": "Chat bot in Java"})
    assert has(check(cv), "is not a project in the profile")


def test_also_built_repeats_main(cv):
    cv["also_built"][0] = {"name": cv["main_project"]["name"], "text": "Route planner",
                           "url": cv["main_project"]["url"]}
    assert has(check(cv), "already a full project")


def test_unapproved_experience_bullet(cv):
    cv["experience"][0]["bullets"].append("Ran the lab's servers.")
    assert has(check(cv), "not an approved bullet")


def test_related_org_only_for_related_jobs(cv):
    cv["experience"][1]["org"] = "Brightline Logistics, Warehouse Robotics"
    cv["experience"][1]["bullets"] = RELATED
    assert has(check(cv), "org")
    assert check(cv, related=True) == []


def test_unrelated_jobs_get_the_short_bullet(cv):
    cv["experience"][1]["bullets"] = RELATED[:1]
    assert has(check(cv), "exactly one bullet, the short one")
    assert not has(check(cv, related=True), "exactly one bullet")


def test_related_bullets_never_mix_with_the_short_one(cv):
    cv["experience"][1]["bullets"] = [SHORT, RELATED[1]]
    assert has(check(cv, related=True), "mixes its default bullets")


def test_title_claim(cv):
    cv["summary_lead"] = "ML Engineer with production experience."
    assert has(check(cv), "summary_lead")


def test_banned_word(cv):
    cv["summary"] += " A passionate builder."
    assert has(check(cv), "banned word")


def test_home_city_and_forbidden_words(cv):
    cv["summary"] += " Based in Manchester. Student."
    probs = check(cv)
    assert has(probs, "manchester") and has(probs, "student")


def test_never_write_phrase(cv):
    cv["summary"] += " Worked on Project Bluebird."
    assert has(check(cv), "never write: 'Project Bluebird'")


def test_unknown_tech_term_in_text(cv):
    cv["summary"] += " Deployed on Kubernetes."
    assert has(check(cv), "Kubernetes")


def test_studied_areas(cv):
    cv["education"][0]["studied"] = "machine learning, quantum computing and algorithms."
    assert has(check(cv), "quantum computing")


def test_contact_changed(cv):
    cv["contact"]["phone"] = "+44 7700 000000"
    assert has(check(cv), "contact.phone")


def test_headline_must_be_null(cv):
    cv["headline"] = "ML Engineer"
    assert has(check(cv), "headline")


def test_bullet_count(cv):
    cv["main_project"]["bullets"] = cv["main_project"]["bullets"][:2]
    assert has(check(cv), "3 to 5 bullets")


def test_duplicate_skill(cv):
    cv["skills"][2]["items"].append("SQL")
    assert has(check(cv), "listed twice")


def test_unsupported_context_claim(cv):
    cv["summary"] += " Used Spark in coursework and in production."
    probs = check(cv)
    assert has(probs, "coursework") and has(probs, "production")


def test_claims_are_whole_words(cv):
    cv["summary"] += " Labeled data by hand."
    assert not has(check(cv), "claim 'led'")


def test_second_project_is_checked_like_the_main_one():
    cv = base()
    cv["second_project"]["bullets"].append("Reached 99% accuracy.")
    assert has(check(cv), "not in its facts")
    cv = base()
    cv["second_project"] = dict(cv["main_project"])
    assert has(check(cv), "second_project repeats the main project")


def test_summary_word_limit():
    cv = base()
    cv["summary"] += " Also enjoys building reliable software with clean code, good tests and clear docs for every team."
    cv["summary"] += " More words here to pass the limit for sure."
    assert has(check(cv), "keep it to 65 or fewer")


def test_no_counts_of_data_or_tests():
    cv = base()
    cv["main_project"]["bullets"][1] = "Trained models on 120,000 delivery records."
    problems = check(cv)
    assert has(problems, "120,000 delivery records")


def test_no_results_on_the_cv():
    cv = base()
    cv["second_project"]["bullets"][0] += " Reached 88.6% accuracy."
    problems = check(cv)
    assert has(problems, "'88.6%'") and has(problems, "'accuracy'")


def test_the_word_map_is_not_a_metric():
    cv = base()
    cv["second_project"]["bullets"][0] += " Added an interactive Leaflet map with the route."
    assert not has(check(cv), "no results or metrics")
    cv["second_project"]["bullets"][0] += " Reached 0.71 mAP and high Accuracy."
    problems = check(cv)
    assert has(problems, "'mAP'") and has(problems, "'Accuracy'")


def test_metrics_allowed_when_the_profile_allows_them():
    p = yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8"))
    p["cv_rules"]["no_metrics"] = False
    cv = base()
    cv["main_project"]["bullets"][1] = "Trained models on 120,000 delivery records."
    assert check(cv, profile=p) == []


def test_projects_without_a_link():
    p = yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8"))
    p["projects"][2]["url"] = None
    cv = base()
    assert has(check(cv, profile=p), "the url must be exactly the profile's")
    del cv["also_built"][0]["url"]
    assert check(cv, profile=p) == []


def test_check_text_for_letters():
    assert check_text("I completed my B.Sc. in Computer Science at Northfield University.") == []
    probs = check_text("I am a student in Manchester who used Kubernetes on Project Bluebird.")
    assert has(probs, "student") and has(probs, "manchester") and has(probs, "Kubernetes") and has(probs, "Bluebird")
    assert check_text("The role uses Kubernetes; I have used Docker.", job_text="Kubernetes, Docker") == []


# --- the CV builder's rules, from the profile's cv_rules ----------------------------------
def test_bullets_keep_the_profile_order():
    from agent import cv_builder
    cv = base()
    cv["experience"][0]["bullets"].reverse()
    cv_builder.order_bullets(cv)
    assert cv["experience"][0]["bullets"][0].startswith("Built Python data pipelines")


def test_specialty_and_related_jobs_come_from_the_profile():
    from agent import cv_builder
    assert cv_builder.job_flags({"title": "Junior ML Engineer", "description": ""})["specialty"]
    assert not cv_builder.job_flags({"title": "Backend Engineer",
                                     "description": "researchers in machine learning"})["specialty"]
    assert cv_builder.job_flags({"title": "Software Engineer", "description": "warehouse robotics"})["related"]
    assert not cv_builder.job_flags({"title": "Software Engineer", "description": "payments"})["related"]
    assert cv_builder.lead_project()["name"] == BASE["main_project"]["name"]


def test_specialty_first_only_on_specialty_jobs():
    from agent import cv_builder
    assert cv_builder.specialty_first("Software Engineer with a specialization in Machine Learning, building models.")
    assert cv_builder.specialty_first("Software Developer with a specialization in ML, building models.")
    assert not cv_builder.specialty_first(
        "Software Engineer building Python test automation, with a specialization in Machine Learning.")


def test_bold_line_fits_one_line_and_prompt_is_built_from_the_profile():
    from agent import cv_builder
    assert len(BASE["summary_lead"]) <= cv_builder.LEAD_MAX
    prompt = cv_builder.system_prompt()
    assert "Alex Morgan" in prompt and "Delivery Route Planner" in prompt and "specialization in Machine Learning" in prompt
    assert "B.Sc. Final Project" in prompt and "{" not in prompt.split("```")[0]


def test_writer_decides_specialty_and_keywords_are_matched_in_the_postings_words():
    from agent import cv_builder
    answer = ("```yaml\n" + yaml.safe_dump(BASE) + "```\n```notes\nspecialty_job: no - the company uses AI, the job "
              "is compliance\nkeywords: test automation; REST APIs; Kubernetes-free\nmatch: a => b\nfit: x\ngaps: none\n```")
    content, fit, gaps, match, notes = cv_builder._parse(answer)
    assert notes["specialty"] is False and notes["keywords"][:2] == ["test automation", "REST APIs"]
    assert cv_builder.job_flags({"title": "Compliance Analyst", "description": "AI AI AI machine learning"})["specialty"] is False
    cover = cv_builder.keyword_cover({"title": "", "description": ""}, content, ["FastAPI", "test automation", "REST-APIs"])
    assert cover["terms_shown"] == ["FastAPI", "REST-APIs"] and cover["terms_missing"] == ["test automation"]
    assert any("test automation" in p for p in cv_builder.improvements({"title": "", "description": ""}, content,
                                                                         ["test automation"]))


def test_example_cv_renders_on_one_page(tmp_path):
    import sys
    sys.path.insert(0, str(config.TEMPLATE_DIR))
    import render_cv
    profile = yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8"))
    assert render_cv.render(str(config.EXAMPLES / "base_cv.example.yaml"), str(tmp_path / "Alex Morgan CV.pdf"),
                            profile) == 1


def test_a_line_or_two_over_one_page_is_cut_by_the_code(tmp_path):
    import pymupdf
    from agent import cv_builder
    cv = base()
    cv["also_built"] = [{"text": "a", "url": "u1"}, {"text": "b", "url": "u2"}]
    cv["second_project"]["bullets"] = ["one", "two", "three"]
    assert cv_builder.trim_cut(cv) == "dropped the last Also built line" and len(cv["also_built"]) == 1
    assert cv_builder.trim_cut(cv) == "dropped the last second-project bullet"
    assert cv["second_project"]["bullets"] == ["one", "two"]
    assert cv_builder.trim_cut(cv) == ""                 # the minimums stay
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "page one")
    doc.new_page().insert_text((72, 72), "spilled line one\nspilled line two")
    doc.save(tmp_path / "cv.pdf")
    assert cv_builder.overflow_lines(tmp_path / "cv.pdf") == 2


def test_also_built_as_plain_lines_is_a_problem_not_a_crash(cv):
    cv["also_built"] = ["Crowd anomaly detection with CLIP and YOLOv8"]
    assert has(check(cv), "Each also_built item must be")
