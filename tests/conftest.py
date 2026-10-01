import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobwatch.db import DB
from jobwatch.models import Job

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path):
    d = DB(str(tmp_path / "test.db"))
    yield d
    d.close()


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def make_job(source_id="1", title="Software Engineer II", company="Acme", **kw) -> Job:
    return Job(company=company, source_id=source_id, title=title, url=f"https://x/{source_id}",
               locations=kw.pop("locations", ["United States, WA, Redmond"]), **kw)
