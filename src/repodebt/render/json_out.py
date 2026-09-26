"""JSON and baseline-difference output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..model import Report


def dumps(report: Report, indent: int | None = 2, diff: dict[str, Any] | None = None) -> str:
    """Serialise a report, optionally carrying a baseline diff alongside it.

    The diff is part of the same document so a CI job gets the regression
    detail and the report in one artefact.
    """
    payload = report.to_dict()
    if diff is not None:
        payload["baseline_diff"] = diff
    return json.dumps(payload, indent=indent, sort_keys=False)


def write_text(text: str, path: Path) -> None:
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def write(report: Report, path: Path, indent: int | None = 2) -> None:
    write_text(dumps(report, indent), path)


def load_baseline(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------
# Baseline comparison
# --------------------------------------------------------------------------

def compare(
    report: Report, baseline: dict[str, Any], threshold: float = 0.0
) -> dict[str, Any]:
    """Diff this run against a stored one.

    Only *regressions* count as failures: new findings and score drops. Fixed
    findings and improvements are reported but never fail a build.
    """
    current_findings = {f.id: f for f in report.findings}
    baseline_findings = _baseline_findings(baseline)
    baseline_titles = {
        f.get("id"): f.get("title", f.get("id", ""))
        for f in baseline.get("findings", []) or []
        if f.get("id")
    }

    new_ids = sorted(set(current_findings) - set(baseline_findings))
    fixed_ids = sorted(set(baseline_findings) - set(current_findings))
    changed = [
        rule_id
        for rule_id in sorted(set(current_findings) & set(baseline_findings))
        if current_findings[rule_id].occurrences != baseline_findings[rule_id]
    ]

    base_score = ((baseline.get("scores") or {}).get("overall"))
    current_score = report.scorecard.overall
    delta = None
    if isinstance(base_score, (int, float)) and current_score is not None:
        delta = current_score - float(base_score)

    config_changed = baseline.get("config_digest") not in (None, report.config_digest)

    regressions: list[dict[str, Any]] = []
    for rule_id in new_ids:
        finding = current_findings[rule_id]
        regressions.append({
            "id": rule_id,
            "kind": "new",
            "severity": finding.severity.value,
            "title": finding.title,
            "occurrences": finding.occurrences,
        })
    for rule_id in changed:
        finding = current_findings[rule_id]
        before = baseline_findings[rule_id]
        if finding.occurrences > before:
            regressions.append({
                "id": rule_id,
                "kind": "increased",
                "severity": finding.severity.value,
                "title": finding.title,
                "from": before,
                "to": finding.occurrences,
            })
    if delta is not None and delta < -abs(threshold):
        regressions.append({
            "id": "scores.overall",
            "kind": "score_drop",
            "severity": "medium",
            "title": "Overall health score dropped",
            "from": round(float(base_score), 1),
            "to": round(current_score, 1),
        })

    dimension_deltas: dict[str, Any] = {}
    base_dims = (baseline.get("scores") or {}).get("dimensions") or {}
    for entry in report.scorecard.dimensions:
        previous = base_dims.get(entry.dimension.value)
        if not isinstance(previous, dict) or previous.get("score") is None:
            continue
        if entry.score is None:
            continue
        change = round(entry.score - float(previous["score"]), 1)
        if abs(change) >= 0.05:
            dimension_deltas[entry.dimension.value] = {
                "from": previous["score"],
                "to": round(entry.score, 1),
                "delta": change,
            }

    return {
        "schema_version": report.schema_version,
        "baseline_present": True,
        "config_changed": config_changed,
        "score": {
            "from": base_score,
            "to": None if current_score is None else round(current_score, 1),
            "delta": None if delta is None else round(delta, 1),
            "grade_from": (baseline.get("scores") or {}).get("grade"),
            "grade_to": report.scorecard.grade,
        },
        "dimensions": dimension_deltas,
        "regressions": regressions,
        "improvements": [
            {"id": rule_id, "kind": "resolved",
             "severity": _baseline_severity(baseline, rule_id),
             "title": baseline_titles.get(rule_id, rule_id),
             "from": baseline_findings[rule_id]}
            for rule_id in fixed_ids
        ]
        + [
            {"id": rule_id, "kind": "decreased",
             "severity": current_findings[rule_id].severity.value,
             "title": current_findings[rule_id].title,
             "from": baseline_findings[rule_id],
             "to": current_findings[rule_id].occurrences}
            for rule_id in changed
            if current_findings[rule_id].occurrences < baseline_findings[rule_id]
        ],
        "summary": {
            "new": len(new_ids),
            "fixed": len(fixed_ids),
            "increased": sum(1 for r in regressions if r["kind"] == "increased"),
            "regressions": len(regressions),
        },
    }


def _baseline_findings(baseline: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for finding in baseline.get("findings", []) or []:
        rule_id = finding.get("id")
        if rule_id:
            out[rule_id] = int(finding.get("occurrences", 1) or 1)
    return out


def _baseline_severity(baseline: dict[str, Any], rule_id: str) -> str | None:
    """Severity recorded for a rule in the baseline, for diff rendering."""
    for finding in baseline.get("findings", []) or []:
        if finding.get("id") == rule_id:
            severity = finding.get("severity")
            return str(severity) if severity else None
    return None


def render_diff(diff: dict[str, Any], color: bool = True) -> str:
    """Human-readable summary of a baseline comparison."""
    from .console import Style

    style = Style(color)
    lines: list[str] = []
    score = diff.get("score") or {}
    summary = diff.get("summary") or {}

    delta = score.get("delta")
    if delta is None:
        arrow = style("score unavailable", "dim")
    elif delta > 0:
        arrow = style(f"+{delta:.1f}", "green")
    elif delta < 0:
        arrow = style(f"{delta:.1f}", "red")
    else:
        arrow = style("0.0", "dim")

    lines.append("")
    lines.append("  BASELINE COMPARISON")
    lines.append("  " + "─" * 40)
    lines.append(f"    score   {score.get('from')} → {score.get('to')}   {arrow}")
    grades = f"{score.get('grade_from')} → {score.get('grade_to')}"
    lines.append(f"    grade   {grades}")
    for name, change in sorted((diff.get("dimensions") or {}).items()):
        color_name = "green" if change["delta"] > 0 else "red"
        change_text = "{:+.1f}".format(change["delta"])
        lines.append(f"      {name:<14} {change['from']:>5} → {change['to']:<5} "
                     f"{style(change_text, color_name)}")
    lines.append("")
    lines.append(f"    new findings     {summary.get('new', 0)}")
    lines.append(f"    resolved         {summary.get('fixed', 0)}")
    lines.append(f"    worsened         {summary.get('increased', 0)}")
    lines.append("")

    regressions = diff.get("regressions") or []
    if not regressions:
        lines.append("    " + style("no regressions against baseline", "green"))
        lines.append("")
        return "\n".join(lines)

    lines.append("    " + style(f"{len(regressions)} regression(s):", "red"))
    for item in regressions[:20]:
        lines.append(f"      {style('+', 'red')} {item['id']:<34} {item['title']}")
    if len(regressions) > 20:
        lines.append(style(f"      … {len(regressions) - 20} more", "dim"))
    lines.append("")
    if diff.get("config_changed"):
        lines.append("    " + style(
            "note: config changed since the baseline was written; "
            "score deltas may reflect thresholds rather than code", "yellow"))
        lines.append("")
    return "\n".join(lines)
