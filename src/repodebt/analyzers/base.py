"""Analyzer interface and the shared analysis context."""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..gitlog import GitRepo
from ..model import Dimension, Finding, Location, Severity
from ..rules import RULES
from ..walker import FileInfo

if TYPE_CHECKING:  # pragma: no cover
    from ..audit import Audit


@dataclass
class Context:
    """Everything an analyzer may read, plus a scratch pad for sharing."""

    root: Path
    files: list[FileInfo]
    config: Config
    git: GitRepo
    now: float
    online: bool = False
    verbose: bool = False
    scratch: dict[str, Any] = field(default_factory=dict)

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"  [debug] {message}", flush=True)

    def source_files(self, include_tests: bool = False) -> list[FileInfo]:
        return [
            f
            for f in self.files
            if f.is_source and (include_tests or not f.is_test)
        ]

    def test_files(self) -> list[FileInfo]:
        return [f for f in self.files if f.is_source and f.is_test]

    def by_language(self, *names: str) -> list[FileInfo]:
        wanted = {n.lower() for n in names}
        return [f for f in self.files if f.language and f.language.name.lower() in wanted]


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    #: False when the analyzer ran but could not actually measure its
    #: dimension. Distinct from ``available()`` returning False, and the
    #: distinction matters: a signal that was not computable must leave the
    #: dimension unscored (score None, dropped from the weighted average)
    #: rather than reporting a perfect score for a measurement that never
    #: happened. An empty finding list is not evidence of health.
    scored: bool = True


class Analyzer(abc.ABC):
    """Base class. Subclasses declare a dimension and implement ``analyze``."""

    dimension: Dimension
    name: str = ""

    def __init__(self) -> None:
        self.name = self.name or type(self).__name__

    @abc.abstractmethod
    def analyze(self, ctx: Context) -> Result:
        ...

    def available(self, ctx: Context) -> bool:
        return True

    def unavailability_reason(self, ctx: Context) -> str:
        return "not computed"


def make_finding(
    rule_id: str,
    detail: str = "",
    severity: Severity | None = None,
    confidence=None,
    evidence: list[str] | None = None,
    locations: list[Location] | None = None,
    remediation: str | None = None,
    occurrences: int = 1,
) -> Finding:
    """Build a finding from the rule catalog, with optional overrides."""
    rule = RULES.get(rule_id)
    if rule is None:
        raise KeyError(f"unknown rule id: {rule_id!r} (register it in rules.py)")
    from ..model import Confidence

    return Finding(
        id=rule.id,
        dimension=rule.dimension,
        severity=severity or rule.severity,
        confidence=confidence or Confidence.HIGH,
        title=rule.title,
        detail=detail,
        evidence=list(evidence or []),
        locations=list(locations or []),
        remediation=remediation or rule.remediation,
        occurrences=occurrences,
    )


def pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return 100.0 * numerator / denominator


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_days(days: float) -> str:
    if days == float("inf"):
        return "never"
    if days < 1:
        return "today"
    if days < 45:
        return f"{int(days)}d"
    if days < 365:
        return f"{int(days / 30)}mo"
    return f"{days / 365:.1f}y"


class Timer:
    def __init__(self, label: str, enabled: bool = False):
        self.label = label
        self.enabled = enabled
        self.elapsed = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed = time.perf_counter() - self._start
        if self.enabled:
            print(f"  [time] {self.label}: {self.elapsed * 1000:.0f}ms", flush=True)
