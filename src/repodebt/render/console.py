"""Terminal report with optional ANSI colour."""

from __future__ import annotations

import os
import sys
from typing import Any

from ..model import Report, Severity

_WIDTH = 78


class Glyphs:
    """Unicode box/bar characters, with an ASCII fallback.

    Windows consoles frequently run cp1252, which cannot encode block or box
    drawing characters. Falling back beats raising UnicodeEncodeError.
    """

    UNICODE = {
        "rule": "─", "bar_full": "█", "bar_empty": "░", "arrow": "→",
        "dot": "·", "bullet": "·", "ellipsis": "…",
    }
    ASCII = {
        "rule": "-", "bar_full": "#", "bar_empty": ".", "arrow": "->",
        "dot": "-", "bullet": "*", "ellipsis": "...",
    }

    def __init__(self, stream=None, prefer_unicode: bool = True):
        self.g = self.UNICODE if prefer_unicode and self._can_encode(stream) else self.ASCII

    @staticmethod
    def _can_encode(stream) -> bool:
        encoding = getattr(stream, "encoding", None) or "ascii"
        try:
            "─█░→·…".encode(encoding)
            return True
        except (UnicodeEncodeError, LookupError):
            return False

    def __getitem__(self, key: str) -> str:
        return self.g.get(key, "")


_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "grey": "\033[90m",
}

_SEVERITY_COLOR = {
    "critical": "red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "info": "grey",
}

_SEVERITY_GLYPH = {
    "critical": "!!",
    "high": "! ",
    "medium": "~ ",
    "low": ". ",
    "info": "  ",
}

_GRADE_COLOR = {
    "A": "green", "B": "green", "C": "yellow", "D": "yellow", "F": "red",
}


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *names: str) -> str:
        if not self.enabled or not names:
            return text
        prefix = "".join(_COLORS.get(name, "") for name in names)
        return f"{prefix}{text}{_COLORS['reset']}"


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _bar(score: float | None, gl: "Glyphs", width: int = 24) -> str:
    if score is None:
        return " " * width
    filled = int(round(score / 100 * width))
    return gl["bar_full"] * filled + gl["bar_empty"] * (width - filled)


def _truncate(text: str, width: int, ellipsis: str = "…") -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: max(0, width - len(ellipsis))] + ellipsis


def render(report: Report, color: bool | None = None, verbose: bool = False) -> str:
    stream = sys.stdout
    style = Style(_supports_color(stream) if color is None else color)
    gl = Glyphs(stream)
    ellipsis = gl["ellipsis"]
    out: list[str] = []
    score = report.scorecard

    def line(text: str = "") -> None:
        out.append(text)

    def rule(char: str | None = None) -> None:
        line(style((char or gl["rule"]) * _WIDTH, "grey"))

    # -- header -----------------------------------------------------------
    line()
    banner = " REPODEBT ".center(_WIDTH, "=")
    line(style(banner, "bold", "cyan"))
    sep = f"  {gl['dot']}  "
    line(f" {report.repo.name}{sep}{report.repo.file_count} files{sep}"
         f"{report.repo.source_file_count} source{sep}{report.duration_seconds:.1f}s")
    line(style(f" {'git' if report.repo.is_git_repo else 'no git'}"
               + (f"{sep}{report.repo.branch}" if report.repo.branch else ""), "dim"))
    rule()

    # -- headline score ---------------------------------------------------
    line()
    if score.overall is None:
        line(style("  NO SCORE  ", "bold", "red"))
        line(style("  No analyzer produced a computable signal.", "dim"))
    else:
        grade = score.grade or "?"
        color = _GRADE_COLOR.get(grade, "grey")
        line("  " + style(_bar(score.overall, gl), color))
        line(f"  {style(f'{score.overall:5.1f}', 'bold', color)} / 100    "
             f"grade {style(grade, 'bold', color)}")
    if score.note:
        line("  " + style(score.note, "yellow"))
    line()

    # -- dimension table --------------------------------------------------
    for entry in score.dimensions:
        label = entry.dimension.label
        if entry.score is None:
            line(f"  {label:<18} {style('n/a', 'dim'):<24} "
                 f"{style(entry.note or 'not measured', 'dim')}")
            continue
        color = _GRADE_COLOR.get(entry.grade or "C", "grey")
        line(f"  {label:<18} {style(_bar(entry.score, gl), color)} "
             f"{entry.score:5.1f}  {style(entry.grade or '', 'bold', color):<4} "
             f"{style(f'{entry.finding_count} findings', 'dim')}")
    line()
    rule()

    # -- findings ---------------------------------------------------------
    if not report.findings:
        line()
        line(style("  No findings. Nothing crossed a configured threshold.", "green"))
        line()
    else:
        current = None
        for finding in report.findings:
            if finding.dimension is not current:
                current = finding.dimension
                line()
                line(style(f"  {current.label.upper()}", "bold"))
                line()
            sev = finding.severity.value
            color = _SEVERITY_COLOR[sev]
            glyph = _SEVERITY_GLYPH[sev]
            title = finding.title
            if finding.occurrences > 1:
                title += style(f"  x{finding.occurrences}", "dim")
            line(f"   {style(glyph, color)} {style(title, color, 'bold')}"
                 + (style(f"  ({finding.confidence.value})", "dim")
                    if finding.confidence.value != "high" else ""))
            if finding.detail:
                line(f"      {style(_truncate(finding.detail, _WIDTH - 8, ellipsis), 'dim')}")
            for item in finding.evidence[:4]:
                line(f"      {style(gl['bullet'] + ' ' + _truncate(item, _WIDTH - 10, ellipsis), 'dim')}")
            for loc in finding.locations[:3]:
                line(f"      {style(gl['arrow'] + ' ' + loc.render(), 'cyan')}")
            if len(finding.locations) > 3:
                more = f"{gl['arrow']} +{len(finding.locations) - 3} more locations"
                line(f"      {style(more, 'dim')}")
            if verbose and finding.remediation:
                line(f"      {style('fix: ' + finding.remediation, 'dim')}")
            line()

    # -- unavailable ------------------------------------------------------
    if report.unavailable:
        line()
        rule()
        line()
        line(style("  NOT MEASURED", "bold", "yellow")
             + style("  (unknown, not passing)", "dim"))
        for key, reason in sorted(report.unavailable.items()):
            line(f"    {style(gl['bullet'], 'yellow')} {key:<28} {style(reason, 'dim')}")
        line()

    if verbose:
        line()
        rule()
        line()
        line(style("  KEY METRICS", "bold"))
        _dump_metrics(line, style, report.metrics, gl)
        line()

    line()
    return "\n".join(out)


def _dump_metrics(line, style: Style, metrics: dict[str, Any], gl: "Glyphs",
                  prefix: str = "") -> None:
    for section, values in metrics.items():
        if not isinstance(values, dict):
            continue
        line(f"    {style(prefix + section, 'bold')}")
        for key, value in values.items():
            if key.startswith("_"):
                continue
            if isinstance(value, (dict, list)):
                text = f"<{len(value)} items>"
            else:
                text = str(value)
            line(f"      {key:<38} "
                 f"{style(_truncate(text, 34, gl['ellipsis']), 'dim')}")
