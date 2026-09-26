"""Hotspot detection: change frequency multiplied by complexity.

The ranking follows the standard formulation: churn over a time window scaled
by the file's measured complexity. Files that are frequently changed *and*
hard to change are where defects and review bottlenecks concentrate.
"""

from __future__ import annotations

from typing import Any

from ..model import Confidence, Dimension, Location
from .base import Analyzer, Context, Result, make_finding, pct

#: Report at most this many hotspots so the report stays readable.
MAX_REPORTED = 20
#: A file needs at least this many commits in the window to count as churned.
MIN_COMMITS = 3


def _complexity_of(stat) -> float:
    """Blend the signals we already measured into one complexity estimate."""
    if stat is None:
        return 0.0
    func_peak = max([f.complexity for f in stat.functions] or [0])
    return (
        1.0
        + stat.total_lines / 400.0
        + stat.max_depth
        + func_peak / 4.0
    )


class HotspotsAnalyzer(Analyzer):
    dimension = Dimension.HOTSPOTS
    name = "hotspots"

    def available(self, ctx: Context) -> bool:
        return ctx.git.available and ctx.git.history(ctx.config.history_window_days).files != {}

    def unavailability_reason(self, ctx: Context) -> str:
        if not ctx.git.available:
            return ctx.git.reason or "git history unavailable"
        window = ctx.config.history_window_days
        history = ctx.git.history(window)
        if not history.commits:
            return f"no commits in the last {window} days"
        return f"no file changes recorded in the last {window} days"

    def analyze(self, ctx: Context) -> Result:
        config = ctx.config
        findings: list = []
        window = config.history_window_days
        history = ctx.git.history(window)
        stats_by_file: dict[str, Any] = ctx.scratch.get("complexity_by_file", {})

        # ``available()`` already gated on a non-empty file history, so the
        # empty case is handled by ``unavailability_reason`` rather than by an
        # unreachable branch here. It stays as a guard because a caller can
        # invoke ``analyze`` directly.
        if not history.files:
            return Result(
                findings=[make_finding(
                    "hotspots.history_unavailable",
                    detail=(f"No file changes found in the last {window} days of history, "
                            f"so hotspot ranking is disabled."),
                    locations=[Location(".")],
                )],
                metrics={"window_days": window, "hotspots": []},
                unavailable={"churn": f"no file changes in the last {window} days"},
            )

        rows: list[dict[str, Any]] = []
        for path, entry in history.files.items():
            if path not in stats_by_file:
                continue  # deleted, vendored, or not a measured source file
            stat = stats_by_file[path]
            if stat.is_test or stat.total_lines == 0:
                continue
            complexity = _complexity_of(stat)
            if entry.commits < MIN_COMMITS:
                continue
            score = entry.commits * complexity
            owner = entry.dominant_author()
            total_authors = len(entry.authors)
            rows.append({
                "file": path,
                "commits": entry.commits,
                "churn": entry.churn,
                "complexity": round(complexity, 2),
                "score": round(score, 1),
                "lines": stat.total_lines,
                "owner": owner,
                "authors": total_authors,
                "days_since_change": (ctx.now - entry.last_timestamp) / 86400.0,
            })

        rows.sort(key=lambda r: -r["score"])
        top = rows[:MAX_REPORTED]

        if top:
            peak = top[0]["score"] or 1.0
            for rank, row in enumerate(top, start=1):
                share = row["score"] / peak
                severity = _severity_for(share, rank)
                confidence = Confidence.HIGH if _is_exact(stats_by_file.get(row["file"])) else Confidence.MEDIUM
                findings.append(make_finding(
                    "hotspots.high_churn_complex",
                    detail=(f"{row['file']} is the #{rank} hotspot: {row['commits']} commits "
                            f"in {window} days over {row['lines']} lines."),
                    evidence=[
                        f"hotspot score: {row['score']}",
                        f"churn: {row['churn']} lines changed",
                        f"complexity index: {row['complexity']}",
                        f"last changed: {row['days_since_change']:.0f} days ago",
                    ],
                    locations=[Location(row["file"])],
                    severity=severity,
                    confidence=confidence,
                ))

                owner = row.get("owner")
                if owner and row["authors"] <= 1 and rank <= 10:
                    findings.append(make_finding(
                        "hotspots.sole_owner",
                        detail=(f"{row['file']} is a top-10 hotspot whose changes all come "
                                f"from one author."),
                        evidence=[f"sole historical author: {mask_email(owner)}"],
                        locations=[Location(row["file"])],
                        severity=_sole_owner_severity(rank),
                    ))

        contributors = len(history.authors)
        metrics: dict[str, Any] = {
            "window_days": window,
            "commits_in_window": len(history.commits),
            "files_changed": len(history.files),
            "hotspots": top,
            "churn_total": sum(r["churn"] for r in rows),
            "contributors_in_window": contributors,
        }
        return Result(findings=findings, metrics=metrics)


def _is_exact(stat) -> bool:
    return bool(stat and stat.exact)


def _severity_for(share: float, rank: int):
    from ..model import Severity

    if share >= 0.85 and rank <= 3:
        return Severity.HIGH
    if share >= 0.5 or rank <= 10:
        return Severity.MEDIUM
    return Severity.LOW


def _sole_owner_severity(rank: int):
    from ..model import Severity

    return Severity.MEDIUM if rank <= 5 else Severity.LOW


def mask_email(email: str) -> str:
    """Partially mask an author identifier for report output."""
    if "@" in email:
        name, _, domain = email.partition("@")
    else:
        name, domain = email, ""
    if len(name) <= 2:
        masked = name[:1] + "*"
    else:
        masked = name[:1] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}" if domain else masked
