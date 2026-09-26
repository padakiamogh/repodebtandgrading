"""Analyzer registry."""

from __future__ import annotations

from .base import Analyzer, Context, Result
from .complexity import ComplexityAnalyzer
from .deps import DepsAnalyzer
from .hotspots import HotspotsAnalyzer
from .hygiene import HygieneAnalyzer
from .tests import TestsAnalyzer

#: Order matters: complexity publishes per-file metrics that hotspots reuses.
REGISTRY: tuple[type[Analyzer], ...] = (
    ComplexityAnalyzer,
    TestsAnalyzer,
    HotspotsAnalyzer,
    HygieneAnalyzer,
    DepsAnalyzer,
)

BY_NAME: dict[str, type[Analyzer]] = {cls().name: cls for cls in REGISTRY}


__all__ = [
    "Analyzer",
    "Context",
    "Result",
    "REGISTRY",
    "BY_NAME",
    "ComplexityAnalyzer",
    "TestsAnalyzer",
    "HotspotsAnalyzer",
    "HygieneAnalyzer",
    "DepsAnalyzer",
]
