"""Every test runs as the made-up Alex Morgan (examples/), never with the user's own files."""
import pytest

from agent import config


@pytest.fixture(autouse=True)
def example_user(monkeypatch):
    monkeypatch.setattr(config, "PROFILE_PATH", config.EXAMPLES / "PROFILE.example.yaml")
    monkeypatch.setattr(config, "SETTINGS_PATH", config.EXAMPLES / "settings.example.yaml")
    monkeypatch.setattr(config, "COMPANIES_PATH", config.EXAMPLES / "companies.example.yaml")
    monkeypatch.setattr(config, "BASE_CONTENT", config.EXAMPLES / "base_cv.example.yaml")
    config.reload()
    from agent import score
    score._tiers.cache_clear()
    score._profile_skills.cache_clear()
    yield
    config.reload()
    score._tiers.cache_clear()
    score._profile_skills.cache_clear()
