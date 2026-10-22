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
    names = {s.company for s in cfg.sources}
    assert {"Microsoft", "Amazon", "Apple", "IBM", "Anthropic", "JPMorgan Chase"} <= names
    assert cfg.classifier is None
    assert cfg.ntfy is None


def test_shared_catalog_holds_no_personal_choices(clean_env):
    import glob

    import yaml

    entries = [s for f in glob.glob("catalog/*.yaml") for s in yaml.safe_load(open(f, encoding="utf-8"))["sources"]]
    assert not [s["company"] for s in entries if "snooze_until" in s]
    names = {s.company for s in load_config("config.yaml").sources}
    assert "SpaceX" not in names and "Anthropic" in names  # SpaceX is in the catalog, off for this user


def test_catalog_files_hold_one_adapter_each(clean_env):
    import glob
    from pathlib import Path

    import yaml

    # custom.yaml holds the single-company adapters (amazon, apple, ...); every other file is named for its adapter.
    for f in map(Path, glob.glob("catalog/*.yaml")):
        types = {s["type"] for s in yaml.safe_load(f.read_text(encoding="utf-8"))["sources"]}
        assert f.stem == "custom" or types == {f.stem}, f


CATALOG = """sources:
  - {company: Acme, type: greenhouse, board: acme}
  - {company: Beta, type: ashby, board: beta}
  - {company: Gone, type: lever, board: gone, enabled: false}   # dead board
"""


def write(tmp_path, personal, catalog=CATALOG):
    (tmp_path / "sources.yaml").write_text(catalog, encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text("catalog: sources.yaml\n" + personal, encoding="utf-8")
    return path


def test_personal_choices_apply_over_the_catalog(clean_env, tmp_path):
    from datetime import UTC, datetime

    cfg = load_config(write(tmp_path, "companies:\n  disable: [Beta]\n  enable: [Gone]\n"
                                      "  snooze: {Acme: 2026-11-09}\n"))
    assert [s.company for s in cfg.sources] == ["Acme", "Gone"]
    assert cfg.is_snoozed("Acme", datetime(2026, 11, 8, tzinfo=UTC))
    assert cfg.watched == [tmp_path / "config.yaml", tmp_path / "sources.yaml"]


def test_catalog_alone_uses_its_own_enabled_flags(clean_env, tmp_path):
    assert [s.company for s in load_config(write(tmp_path, "")).sources] == ["Acme", "Beta"]


@pytest.mark.parametrize("personal", ["companies:\n  disable: [Acme Corp]\n", "companies:\n  snooze: {Bta: 2026-11-09}\n"])
def test_a_misspelt_company_is_an_error_not_a_silent_no_op(clean_env, tmp_path, personal):
    with pytest.raises(ValueError, match="not in the catalog"):
        load_config(write(tmp_path, personal))


def test_duplicate_company_is_an_error(clean_env, tmp_path):
    # company is part of every job key, so two entries would share (and corrupt) one history.
    with pytest.raises(ValueError, match="Acme"):
        load_config(write(tmp_path, "", CATALOG + "  - {company: Acme, type: lever, board: other}\n"))


def test_company_rules_extend_the_base_rules(clean_env):
    ms = load_config("config.yaml").rules_for("Microsoft")
    assert ms.reject_reason("Software Engineering IC4") == "senior title"
    assert ms.reject_reason("Software Engineering IC3") is None
    assert ms.reject_reason("Senior Software Engineer") == "senior title"
    amazon = load_config("config.yaml").rules_for("Amazon")
    assert amazon.reject_reason("Software Development Engineer III, AWS") == "senior title"
    assert amazon.reject_reason("Software Development Engineer II, AWS") is None


def test_snooze_expires_on_its_date(clean_env, tmp_path):
    from datetime import UTC, datetime

    cfg = load_config(write(tmp_path, "companies:\n  snooze: {Acme: 2026-11-09}\n"))
    assert cfg.is_snoozed("Acme", datetime(2026, 11, 8, 23, 59, tzinfo=UTC))
    assert not cfg.is_snoozed("Acme", datetime(2026, 11, 9, 0, 0, tzinfo=UTC))
    assert not cfg.is_snoozed("Beta", datetime(2026, 11, 8, tzinfo=UTC))


def test_profile_reaches_the_classifier(clean_env, tmp_path):
    clean_env.setenv("GEMINI_API_KEY", "k")
    cfg = load_config(write(tmp_path, "profile: >\n  a data engineer with 1 year of experience\n"))
    assert cfg.classifier.profile == "a data engineer with 1 year of experience"


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
