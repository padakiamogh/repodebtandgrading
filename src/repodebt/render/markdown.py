"""Human-readable Markdown report."""

from __future__ import annotations

import datetime as _dt

from ..model import Report, Severity

_SEVERITY_BADGE = {
    "critical": "🔴 CRITICAL",
    "high": "🟠 HIGH",
    "medium": "🟡 MEDIUM",
    "low": "🔵 LOW",
    "info": "⚪ INFO",
}

_GRADE_VERDICT = {
    "A": "Healthy. Keep the current practices and the regression baseline.",
    "B": "Solid, with a few concentrated problems worth scheduling.",
    "C": "Accumulated debt. Pick the top findings and work them down.",
    "D": "Significant debt. Refactor before adding more features.",
    "F": "Critical debt. Treat remediation as a prerequisite for new work.",
}


def _bar(score: float | None, width: int = 20) -> str:
    if score is None:
        return "`n/a`"
    filled = round(score / 100 * width)
    return "`" + "█" * filled + "░" * (width - filled) + f"` {score:5.1f}"


def _fmt_location(location) -> str:
    return f"`{location.render()}`"


def render(report: Report) -> str:
    lines: list[str] = []
    generated = _dt.datetime.fromtimestamp(report.generated_at).strftime("%Y-%m-%d %H:%M")
    score = report.scorecard

    lines.append("# Repository Health Report")
    lines.append("")
    lines.append(f"**{report.repo.name}** · generated {generated} · "
                 f"{report.duration_seconds:.1f}s")
    lines.append("")

    # -- headline ---------------------------------------------------------
    lines.append("## Overall")
    lines.append("")
    if score.overall is None:
        lines.append("### No score available")
        lines.append("")
        lines.append("> No analyzer produced computable signals.")
    else:
        lines.append(f"### Grade **{score.grade}** — {score.overall:.1f} / 100")
        lines.append("")
        lines.append(f"> {_GRADE_VERDICT.get(score.grade or 'F', '')}")
    if score.note:
        lines.append("")
        lines.append(f"> **Note:** {score.note}")
    lines.append("")

    lines.append("| Dimension | Score | Grade | Weight | Findings |")
    lines.append("| --- | ---: | :---: | ---: | ---: |")
    for entry in score.dimensions:
        if entry.score is None:
            lines.append(f"| {entry.dimension.label} | _n/a_ | — | — | "
                         f"{entry.finding_count} |")
        else:
            lines.append(
                f"| {entry.dimension.label} | {entry.score:.1f} | {entry.grade} | "
                f"{entry.weight:.0%} | {entry.finding_count} |"
            )
    lines.append("")

    # -- repository facts -------------------------------------------------
    repo = report.repo
    lines.append("## Repository")
    lines.append("")
    lines.append(f"- Files analysed: **{repo.file_count}** "
                 f"({repo.source_file_count} source), "
                 f"{repo.excluded_file_count} excluded")
    lines.append(f"- Version control: {'git' if repo.is_git_repo else 'none'}"
                 + (f" · branch `{repo.branch}` · HEAD `{(repo.head or '')[:8]}`"
                    if repo.is_git_repo else ""))
    top_langs = list(repo.languages.items())[:8]
    if top_langs:
        lines.append("- Languages: " + ", ".join(f"{name} ({count})" for name, count in top_langs))
    lines.append("")

    # -- findings ---------------------------------------------------------
    lines.append("## Findings")
    lines.append("")
    if not report.findings:
        lines.append("No findings. Nothing crossed a configured threshold.")
        lines.append("")
    else:
        counts: dict[str, int] = {}
        for finding in report.findings:
            counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
        summary = " · ".join(
            f"{_SEVERITY_BADGE[sev].split()[-1]} {count}"
            for sev, count in sorted(counts.items(),
                                     key=lambda kv: -Severity(kv[0]).rank)
        )
        lines.append(summary)
        lines.append("")
        for entry in score.dimensions:
            subset = [f for f in report.findings if f.dimension is entry.dimension]
            if not subset:
                continue
            if entry.score is not None:
                heading = f"### {entry.dimension.label} ({entry.score:.1f}/100)"
            else:
                heading = f"### {entry.dimension.label} (not scored)"
            lines.append(heading)
            lines.append("")
            if entry.note:
                lines.append(f"> {entry.note}")
                lines.append("")
            for finding in subset:
                badge = _SEVERITY_BADGE[finding.severity.value]
                confidence = ("" if finding.confidence.value == "high"
                              else f" _({finding.confidence.value})_")
                lines.append(f"#### {badge} {finding.title}{confidence}")
                lines.append("")
                lines.append(f"- **Rule:** `{finding.id}`")
                if finding.detail:
                    lines.append(f"- {finding.detail}")
                if finding.evidence:
                    lines.append("- Evidence:")
                    for item in finding.evidence:
                        lines.append(f"  - {item}")
                if finding.locations:
                    shown = ", ".join(_fmt_location(loc) for loc in finding.locations[:6])
                    more = (f" (+{len(finding.locations) - 6} more)"
                            if len(finding.locations) > 6 else "")
                    lines.append(f"- Locations: {shown}{more}")
                if finding.occurrences > 1:
                    lines.append(f"- Occurrences: {finding.occurrences}")
                if finding.remediation:
                    lines.append(f"- **Fix:** {finding.remediation}")
                lines.append("")

    # -- unavailable ------------------------------------------------------
    if report.unavailable:
        lines.append("## Signals not measured")
        lines.append("")
        lines.append("These were not computed. They are unknown, not passing.")
        lines.append("")
        lines.append("| Signal | Reason |")
        lines.append("| --- | --- |")
        for key, reason in sorted(report.unavailable.items()):
            lines.append(f"| `{key}` | {reason} |")
        lines.append("")

    # -- metrics appendix -------------------------------------------------
    lines.append("## Metrics")
    lines.append("")
    lines.append("```json")
    import json as _json

    trimmed = _trim_metrics(report.metrics)
    lines.append(_json.dumps(trimmed, indent=2, sort_keys=False))
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"<sub>repodebt · schema {report.schema_version} · "
                 f"config `{report.config_digest}`</sub>")
    lines.append("")
    return "\n".join(lines)


def _trim_metrics(metrics: dict, depth: int = 0) -> dict:
    """Keep the appendix readable: cap long lists, drop timing noise."""
    out: dict = {}
    for key, value in metrics.items():
        if key == "_elapsed_ms":
            continue
        if isinstance(value, dict):
            out[key] = _trim_metrics(value, depth + 1)
        elif isinstance(value, list) and len(value) > 8:
            out[key] = value[:8] + [f"... {len(value) - 8} more"]
        else:
            out[key] = value
    return out
