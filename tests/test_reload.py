import os

import pytest
from conftest import NOW
from test_runner import FakeNotifier, FakeSource, watcher

import jobwatch.runner as runner
from jobwatch.config import load_config
from jobwatch.runner import Watcher

BASE = "sources:\n  - {company: Acme, type: greenhouse, board: acme}\n"
PERSONAL = "catalog: sources.yaml\n"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "JOBWATCH_ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


class File:
    """A file whose mtime moves forward on every write, whatever the filesystem's clock granularity."""

    clock = 0

    def __init__(self, path, text):
        self.path = path
        self.write(text)

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        File.clock += 1
        os.utime(self.path, ns=(File.clock * 10**9, File.clock * 10**9))


@pytest.fixture
def live(tmp_path, db):
    catalog = File(tmp_path / "sources.yaml", BASE)
    personal = File(tmp_path / "config.yaml", PERSONAL)
    w = Watcher(load_config(personal.path), db, FakeNotifier(), config_path=personal.path)
    return w, catalog, personal


def test_unchanged_files_are_not_reloaded(live):
    w, _, _ = live
    before = w.cfg
    assert w.reload_if_changed() == set()
    assert w.cfg is before


def test_added_source_is_picked_up_and_existing_one_is_kept(live):
    w, catalog, _ = live
    acme = w.cfg.sources[0]
    catalog.write(BASE + "  - {company: Beta, type: ashby, board: beta}\n")
    assert w.reload_if_changed() == {"Beta"}
    assert [s.company for s in w.cfg.sources] == ["Acme", "Beta"]
    assert w.cfg.sources[0] is acme  # untouched source keeps its state


def test_snooze_edit_applies_without_touching_sources(live):
    w, _, personal = live
    personal.write(PERSONAL + "companies:\n  snooze: {Acme: 2026-12-01}\n")
    assert w.reload_if_changed() == set()
    assert w.cfg.is_snoozed("Acme", NOW)


def test_changed_adapter_spec_rebuilds_that_source(live):
    w, catalog, _ = live
    acme = w.cfg.sources[0]
    catalog.write("sources:\n  - {company: Acme, type: greenhouse, board: acme-new}\n")
    assert w.reload_if_changed() == {"Acme"}
    assert w.cfg.sources[0] is not acme


def test_disabled_source_drops_its_backoff_state(live):
    w, catalog, personal = live
    w.failures["Acme"], w.retry_at["Acme"], w.last_poll["Acme"] = 2, NOW, NOW
    catalog.write(BASE + "  - {company: Beta, type: ashby, board: beta}\n")
    personal.write(PERSONAL + "companies:\n  disable: [Acme]\n")
    assert w.reload_if_changed() == {"Beta"}
    assert [s.company for s in w.cfg.sources] == ["Beta"]
    assert not (w.failures or w.retry_at or w.last_poll)


def test_broken_edit_keeps_the_old_config_and_the_next_good_save_applies(live):
    w, catalog, _ = live
    before = w.cfg
    catalog.write("sources: [unclosed")
    assert w.reload_if_changed() == set()
    assert w.cfg is before
    assert w.reload_if_changed() == set()  # same broken save: not re-parsed
    catalog.write(BASE + "  - {company: Beta, type: ashby, board: beta}\n")
    assert w.reload_if_changed() == {"Beta"}


@pytest.mark.parametrize("catalog_text, personal_text", [
    (BASE + "  - {company: Beta, type: nope, board: beta}\n", PERSONAL),   # unknown adapter
    (BASE, PERSONAL + "companies:\n  disable: [Acmee]\n"),                  # misspelt company
])
def test_invalid_edit_is_a_failed_reload_not_a_crash(live, catalog_text, personal_text):
    w, catalog, personal = live
    before = w.cfg
    catalog.write(catalog_text)
    personal.write(personal_text)
    assert w.reload_if_changed() == set()
    assert w.cfg is before


def test_new_file_in_a_catalog_directory_is_picked_up(tmp_path, db):
    (tmp_path / "catalog").mkdir()
    File(tmp_path / "catalog" / "a.yaml", BASE)
    personal = File(tmp_path / "config.yaml", "")  # catalog/ is the default
    w = Watcher(load_config(personal.path), db, FakeNotifier(), config_path=personal.path)
    File(tmp_path / "catalog" / "b.yaml", "sources:\n  - {company: Beta, type: ashby, board: beta}\n")
    os.utime(tmp_path / "catalog", ns=(File.clock * 10**9, File.clock * 10**9))
    assert w.reload_if_changed() == {"Beta"}


async def test_cycle_only_polls_the_named_sources(db):
    a, b = FakeSource([[]]), FakeSource([[]])
    b.company = "Other"
    w = watcher(db, a, FakeNotifier())
    w.cfg.sources.append(b)
    await w.cycle(None, only={"Other"})
    assert (a.attempts, b.attempts) == (0, 1)


async def test_idle_reloads_mid_wait_and_polls_only_what_changed(live, monkeypatch):
    w, catalog, _ = live
    polled = []

    async def fake_cycle(client, only=None):
        polled.append(only)

    real_sleep = runner.asyncio.sleep

    async def sleep(s):
        if not polled and w.cfg.sources[-1].company == "Acme":
            catalog.write(BASE + "  - {company: Beta, type: ashby, board: beta}\n")
        await real_sleep(0.01)

    monkeypatch.setattr(w, "cycle", fake_cycle)
    monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    await w.idle(None, 0.2, check_every=0.05)
    assert polled == [{"Beta"}]
