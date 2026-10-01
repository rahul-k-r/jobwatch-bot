"""Deterministic rules. Cheap, run before any network detail call or LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobwatch.models import Job

_COMBO_SPLIT = re.compile(r"\s*(?:\+|/|\||\bor\b)\s*", re.I)


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in patterns]


@dataclass
class TitleRules:
    include: list[re.Pattern[str]]
    seniority: list[re.Pattern[str]]
    exclude: list[re.Pattern[str]] = field(default_factory=list)

    @classmethod
    def from_config(cls, include: list[str], seniority: list[str], exclude: list[str]) -> TitleRules:
        return cls(_compile(include), _compile(seniority), _compile(exclude))

    def extend(self, seniority: list[str]) -> TitleRules:
        return TitleRules(self.include, self.seniority + _compile(seniority), self.exclude)

    def reject_reason(self, title: str) -> str | None:
        if self.include and not any(p.search(title) for p in self.include):
            return "title not in scope"
        if hit := next((p.pattern for p in self.exclude if p.search(title)), None):
            return f"title excluded ({hit})"
        # Combo postings like "Software Engineer II/Senior Software Engineer" survive
        # as long as one part is not senior. Only parts that are titles themselves count,
        # so "Senior Software Engineer - Linux / Android" isn't saved by "Android".
        parts = [p for p in _COMBO_SPLIT.split(title) if p.strip()]
        titled = [p for p in parts if not self.include or any(i.search(p) for i in self.include)] or [title]
        if all(any(s.search(p) for s in self.seniority) for p in titled):
            return "senior title"
        return None


_US = re.compile(r"\bUnited States\b|\bUSA\b|\bU\.S\.(A\.)?", re.I)
_US_CODE = re.compile(r"\bUS\b")  # case-sensitive, so the word "us" doesn't count
_REMOTE_ONLY = re.compile(r"^\W*(remote|anywhere|worldwide|global)\W*$", re.I)
_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
           "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
# "City, ST" as written in free-text locations ("Bellevue, WA"); case-sensitive to avoid words like "in".
_STATE_CODE = re.compile(r",\s*(" + "|".join(_STATES) + r")\b(?!\s*,\s*(Canada|Australia|India))")


def is_us(job: Job) -> bool:
    # Unknown location (none listed, or bare "Remote") is kept: a missed job costs more than an extra alert.
    if not job.locations:
        return True
    return any(_US.search(loc) or _US_CODE.search(loc) or _STATE_CODE.search(loc) or _REMOTE_ONLY.match(loc)
               for loc in job.locations)


# Phrases that mean the role is closed to someone needing sponsorship. Kept narrow on purpose:
# EEO boilerplate ("without regard to ... citizenship, immigration status") must not match.
_NO_SPONSORSHIP = re.compile(
    r"|".join([
        r"\b(unable|not able|will not|won'?t|cannot|can ?not|does not|do not|is not able)\s+(to\s+)?"
        r"(provide\s+|offer\s+|support\s+)?(visa\s+|immigration\s+|employment\s+)?sponsor",
        r"\bsponsorship\s+(is\s+)?(not|un)\s*available",
        r"\bno\s+(visa\s+|immigration\s+)?sponsorship",
        r"\bwithout\s+(the\s+need\s+for\s+)?(current\s+or\s+future\s+|any\s+)?(visa\s+|employer\s+)?sponsorship",
        r"\b(must|required\s+to)\s+be\s+(a\s+)?U\.?\s?S\.?\s+citizen",
        r"\bU\.?\s?S\.?\s+citizenship\s+(is\s+)?required",
        r"\brequires?\s+U\.?\s?S\.?\s+citizenship",
        r"\b(TS/SCI|top\s+secret|polygraph|active\s+(security\s+)?clearance)\b",
        # ITAR "U.S. person" means citizen or green card holder, so a visa holder doesn't qualify.
        r"\bmust\s+be\s+a\s+U\.?\s?S\.?\s+person",
        r"\bU\.?\s?S\.?\s+person\s+status\s+(is\s+)?required",
        # Clearances need citizenship; "is a plus" means it's optional.
        r"\b(ability|able|eligib\w*)\s+to\s+obtain\s+(and\s+maintain\s+)?(an?\s+)?(U\.?\s?S\.?\s+)?"
        r"(\w+\s+)?clearance\b(?!\s+(is\s+)?(a\s+plus|preferred|desired|nice))",
    ]),
    re.I,
)


def no_sponsorship_reason(job: Job) -> str | None:
    if job.description and (m := _NO_SPONSORSHIP.search(job.description)):
        return f"no sponsorship: '{m.group(0)}'"
    return None
