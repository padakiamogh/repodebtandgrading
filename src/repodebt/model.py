"""Core data types shared by every analyzer and renderer.

Everything that leaves the process goes through ``to_dict`` so the JSON
schema is defined in exactly one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self.value]

    @property
    def label(self) -> str:
        return self.value.upper()


_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class Confidence(str, Enum):
    """How much to trust a finding.

    ``HEURISTIC`` findings are real signals measured with an approximation
    (typically a language we cannot parse exactly). They are discounted by
    the scorer rather than hidden.
    """

    HIGH = "high"
    MEDIUM = "medium"
    HEURISTIC = "heuristic"

    @property
    def multiplier(self) -> float:
        return _CONFIDENCE_MULTIPLIER[self.value]


_CONFIDENCE_MULTIPLIER = {"high": 1.0, "medium": 0.8, "heuristic": 0.5}


class Dimension(str, Enum):
    COMPLEXITY = "complexity"
    HOTSPOTS = "hotspots"
    TESTS = "tests"
    HYGIENE = "hygiene"
    DEPS = "deps"

    @property
    def label(self) -> str:
        return {
            "complexity": "Complexity",
            "hotspots": "Hotspots",
            "tests": "Test Health",
            "hygiene": "Repo Hygiene",
            "deps": "Dependency Risk",
        }[self.value]


@dataclass(frozen=True)
class Location:
    file: str
    line: int | None = None

    def render(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line}


@dataclass
class Finding:
    """A single actionable observation about the repository."""

    id: str
    dimension: Dimension
    severity: Severity
    confidence: Confidence
    title: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    remediation: str = ""
    # How many raw occurrences collapsed into this finding.
    occurrences: int = 1

    @property
    def penalty(self) -> float:
        from .scoring import SEVERITY_PENALTY

        return SEVERITY_PENALTY[self.severity] * self.confidence.multiplier

    def sort_key(self) -> tuple:
        first = self.locations[0] if self.locations else Location("", None)
        return (-self.severity.rank, -self.penalty, self.id, first.file, first.line or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dimension": self.dimension.value,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "title": self.title,
            "detail": self.detail,
            "evidence": list(self.evidence),
            "locations": [loc.to_dict() for loc in self.locations],
            "remediation": self.remediation,
            "occurrences": self.occurrences,
        }


@dataclass
class DimensionScore:
    dimension: Dimension
    score: float | None
    penalty: float
    finding_count: int
    weight: float
    grade: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": None if self.score is None else round(self.score, 1),
            "grade": self.grade,
            "weight": round(self.weight, 4),
            "finding_count": self.finding_count,
            "note": self.note,
        }


@dataclass
class Scorecard:
    overall: float | None
    grade: str | None
    dimensions: list[DimensionScore]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": None if self.overall is None else round(self.overall, 1),
            "grade": self.grade,
            "note": self.note,
            "dimensions": {d.dimension.value: d.to_dict() for d in self.dimensions},
        }

    def get(self, dimension: Dimension) -> DimensionScore | None:
        for d in self.dimensions:
            if d.dimension is dimension:
                return d
        return None


@dataclass
class RepoInfo:
    root: str
    name: str
    is_git_repo: bool
    head: str | None = None
    branch: str | None = None
    file_count: int = 0
    source_file_count: int = 0
    excluded_file_count: int = 0
    total_bytes: int = 0
    languages: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "name": self.name,
            "is_git_repo": self.is_git_repo,
            "head": self.head,
            "branch": self.branch,
            "file_count": self.file_count,
            "source_file_count": self.source_file_count,
            "excluded_file_count": self.excluded_file_count,
            "total_bytes": self.total_bytes,
            "languages": dict(sorted(self.languages.items(), key=lambda kv: -kv[1])),
            "truncated": self.truncated,
            "notes": list(self.notes),
        }


@dataclass
class Report:
    repo: RepoInfo
    scorecard: Scorecard
    metrics: dict[str, Any]
    findings: list[Finding]
    unavailable: dict[str, str] = field(default_factory=dict)
    generated_at: float = field(default_factory=time.time)
    duration_seconds: float = 0.0
    config_digest: str = ""
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "config_digest": self.config_digest,
            "repo": self.repo.to_dict(),
            "scores": self.scorecard.to_dict(),
            "metrics": self.metrics,
            "findings": [f.to_dict() for f in self.findings],
            "unavailable": self.unavailable,
        }


def grade_for(score: float | None, bands: Iterable[tuple[float, str]]) -> str | None:
    """Map a 0-100 score onto a letter grade using descending ``bands``."""
    if score is None:
        return None
    for threshold, letter in bands:
        if score >= threshold:
            return letter
    return "F"
