import pytest

from jobwatch.config import load_config


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN", "HEARTBEAT_URL", "JOBWATCH_DB"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_repo_config_loads_with_optional_integrations_off(clean_env):
    cfg = load_config("config.yaml")
    names = [s.company for s in cfg.sources]
    assert names[:4] == ["Microsoft", "Amazon", "Apple", "IBM"]
    assert len(names) == len(set(names))  # company is part of every job key
    assert cfg.classifier is None
    assert cfg.ntfy is None


def test_company_rules_extend_the_base_rules(clean_env):
    ms = load_config("config.yaml").rules_for("Microsoft")
    assert ms.reject_reason("Software Engineering IC4") == "senior title"
    assert ms.reject_reason("Software Engineering IC3") is None
    assert ms.reject_reason("Senior Software Engineer") == "senior title"
    amazon = load_config("config.yaml").rules_for("Amazon")
    assert amazon.reject_reason("Software Development Engineer III, AWS") == "senior title"
    assert amazon.reject_reason("Software Development Engineer II, AWS") is None


@pytest.mark.parametrize("utc, expected", [
    ("2026-09-29T16:00:00+00:00", 300),   # Tue 09:00 PT
    ("2026-09-29T13:00:00+00:00", 300),   # Tue 06:00 PT (start is inclusive)
    ("2026-09-29T12:59:00+00:00", 1800),  # Tue 05:59 PT
    ("2026-09-30T04:00:00+00:00", 1800),  # Tue 21:00 PT (end is exclusive)
    ("2026-10-03T18:00:00+00:00", 1800),  # Sat 11:00 PT
])
def test_offpeak_interval(clean_env, utc, expected):
    from datetime import datetime
    assert load_config("config.yaml").interval_at(datetime.fromisoformat(utc)) == expected


def test_env_enables_integrations(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "k")
    clean_env.setenv("NTFY_TOPIC", "t")
    clean_env.setenv("NTFY_TOKEN", "tk_x")
    cfg = load_config("config.yaml")
    assert cfg.classifier.api_key == "k" and cfg.classifier.batch_size == 10
    assert (cfg.ntfy.server, cfg.ntfy.topic, cfg.ntfy.token) == ("https://ntfy.sh", "t", "tk_x")
