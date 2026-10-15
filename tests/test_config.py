import pytest

from jobwatch.config import load_config


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "JOBWATCH_ANTHROPIC_API_KEY", "NTFY_TOPIC", "NTFY_TOPIC_RESUMES", "NTFY_TOPIC_LOW", "NTFY_SERVER", "NTFY_TOKEN",
                "HEARTBEAT_URL", "JOBWATCH_DB"):
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


def test_snooze_until_is_parsed_and_expires_on_its_date(clean_env, tmp_path):
    from datetime import UTC, datetime

    path = tmp_path / "c.yaml"
    path.write_text(
        "sources:\n  - {company: Acme, type: greenhouse, board: acme, snooze_until: 2026-11-09}\n"
        "  - {company: Other, type: greenhouse, board: other}\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.is_snoozed("Acme", datetime(2026, 11, 8, 23, 59, tzinfo=UTC))
    assert not cfg.is_snoozed("Acme", datetime(2026, 11, 9, 0, 0, tzinfo=UTC))
    assert not cfg.is_snoozed("Other", datetime(2026, 11, 8, tzinfo=UTC))


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
    assert (cfg.ntfy.resume_topic, cfg.ntfy.low_topic) == (None, None)
    assert cfg.classifier.claude_api_key is None


def test_anthropic_key_adds_claude_and_alone_enables_the_classifier(clean_env):
    # Not ANTHROPIC_API_KEY: in a shell's environment that would switch the resume worker's
    # `claude -p` from the subscription to paid API billing.
    clean_env.setenv("JOBWATCH_ANTHROPIC_API_KEY", "ck")
    cfg = load_config("config.yaml")
    assert cfg.classifier.claude_api_key == "ck" and cfg.classifier.api_key is None
    assert [m for m, _, _ in cfg.classifier.chain()] == ["claude-haiku-5-5"]


def test_separate_topics_for_resumes_and_low_priority(clean_env):
    clean_env.setenv("NTFY_TOPIC", "t")
    clean_env.setenv("NTFY_TOPIC_RESUMES", "cvs")
    clean_env.setenv("NTFY_TOPIC_LOW", "low")
    cfg = load_config("config.yaml")
    assert (cfg.ntfy.topic, cfg.ntfy.resume_topic, cfg.ntfy.low_topic) == ("t", "cvs", "low")
