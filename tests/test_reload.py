import os

import pytest
from conftest import NOW
from test_runner import FakeNotifier, FakeSource, watcher

import jobwatch.runner as runner
from jobwatch.config import load_config
from jobwatch.runner import Watcher

BASE = "sources:\n  - {company: Acme, type: greenhouse, board: acme}\n"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "JOBWATCH_ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


class Cfg:
    """A config file whose mtime is bumped on every write, whatever the filesystem's clock granularity."""

    def __init__(self, path, text=BASE):
        self.path, self.n = path, 0
        self.write(text)

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        self.n += 1
        os.utime(self.path, ns=(self.n * 10**9, self.n * 10**9))


@pytest.fixture
def live(tmp_path, db):
    cfg = Cfg(tmp_path / "config.yaml")
    w = Watcher(load_config(cfg.path), db, FakeNotifier(), config_path=cfg.path)
    return w, cfg


def test_unchanged_file_is_not_reloaded(live):
    w, _ = live
    before = w.cfg
    assert w.reload_if_changed() == set()
    assert w.cfg is before


def test_added_source_is_picked_up_and_existing_one_is_kept(live):
    w, cfg = live
    acme = w.cfg.sources[0]
    cfg.write(BASE + "  - {company: Beta, type: ashby, board: beta}\n")
    assert w.reload_if_changed() == {"Beta"}
    assert [s.company for s in w.cfg.sources] == ["Acme", "Beta"]
    assert w.cfg.sources[0] is acme  # untouched source keeps its state


def test_snooze_edit_applies_without_touching_sources(live):
    w, cfg = live
    cfg.write("sources:\n  - {company: Acme, type: greenhouse, board: acme, snooze_until: 2026-12-01}\n")
    assert w.reload_if_changed() == set()
    assert w.cfg.is_snoozed("Acme", NOW)


def test_changed_adapter_spec_rebuilds_that_source(live):
    w, cfg = live
    acme = w.cfg.sources[0]
    cfg.write("sources:\n  - {company: Acme, type: greenhouse, board: acme-new}\n")
    assert w.reload_if_changed() == {"Acme"}
    assert w.cfg.sources[0] is not acme


def test_removed_source_drops_its_backoff_state(live):
    w, cfg = live
    w.failures["Acme"], w.retry_at["Acme"], w.last_poll["Acme"] = 2, NOW, NOW
    cfg.write("sources:\n  - {company: Acme, type: greenhouse, board: acme, enabled: false}\n"
              "  - {company: Beta, type: ashby, board: beta}\n")
    assert w.reload_if_changed() == {"Beta"}
    assert [s.company for s in w.cfg.sources] == ["Beta"]
    assert not (w.failures or w.retry_at or w.last_poll)


def test_broken_edit_keeps_the_old_config_and_the_next_good_save_applies(live):
    w, cfg = live
    before = w.cfg
    cfg.write("sources: [unclosed")
    assert w.reload_if_changed() == set()
    assert w.cfg is before
    assert w.reload_if_changed() == set()  # same broken save: not re-parsed
    cfg.write(BASE + "  - {company: Beta, type: ashby, board: beta}\n")
    assert w.reload_if_changed() == {"Beta"}


def test_unknown_source_type_is_a_failed_reload_not_a_crash(live):
    w, cfg = live
    before = w.cfg
    cfg.write(BASE + "  - {company: Beta, type: nope, board: beta}\n")
    assert w.reload_if_changed() == set()
    assert w.cfg is before


async def test_cycle_only_polls_the_named_sources(db):
    a, b = FakeSource([[]]), FakeSource([[]])
    b.company = "Other"
    w = watcher(db, a, FakeNotifier())
    w.cfg.sources.append(b)
    await w.cycle(None, only={"Other"})
    assert (a.attempts, b.attempts) == (0, 1)


async def test_idle_reloads_mid_wait_and_polls_only_what_changed(live, monkeypatch):
    w, cfg = live
    polled = []

    async def fake_cycle(client, only=None):
        polled.append(only)

    real_sleep = runner.asyncio.sleep

    async def sleep(s):
        if not polled and w.cfg.sources[-1].company == "Acme":
            cfg.write(BASE + "  - {company: Beta, type: ashby, board: beta}\n")
        await real_sleep(0.01)

    monkeypatch.setattr(w, "cycle", fake_cycle)
    monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    await w.idle(None, 0.2, check_every=0.05)
    assert polled == [{"Beta"}]
