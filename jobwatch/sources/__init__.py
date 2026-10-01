from __future__ import annotations

from typing import Any

from jobwatch.sources.amazon import AmazonSource
from jobwatch.sources.apple import AppleSource
from jobwatch.sources.ashby import AshbySource
from jobwatch.sources.base import Source
from jobwatch.sources.eightfold import EightfoldSource
from jobwatch.sources.google import GoogleSource
from jobwatch.sources.greenhouse import GreenhouseSource
from jobwatch.sources.ibm import IBMSource
from jobwatch.sources.jibe import JibeSource
from jobwatch.sources.lever import LeverSource
from jobwatch.sources.workable import WorkableSource
from jobwatch.sources.workday import WorkdaySource

_TYPES: dict[str, type[Source]] = {
    "amazon": AmazonSource,
    "apple": AppleSource,
    "ashby": AshbySource,
    "eightfold": EightfoldSource,
    "google": GoogleSource,
    "greenhouse": GreenhouseSource,
    "ibm": IBMSource,
    "jibe": JibeSource,
    "lever": LeverSource,
    "workable": WorkableSource,
    "workday": WorkdaySource,
}

# Keys of a `sources:` entry consumed by the pipeline rather than the adapter.
PIPELINE_KEYS = ("type", "exclude_title", "interval_seconds")


def build_source(spec: dict[str, Any]) -> Source:
    """spec is one `sources:` entry from config.yaml."""
    kwargs = {k: v for k, v in spec.items() if k not in PIPELINE_KEYS}
    try:
        cls = _TYPES[spec["type"]]
    except KeyError:
        raise ValueError(f"unknown source type {spec.get('type')!r} for {spec.get('company')}") from None
    return cls(**kwargs)
