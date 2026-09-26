"""Turning findings into a health score.

The model is deliberately simple and explainable:

    penalty  = sum(severity_penalty[f] * confidence_multiplier[f])
    score    = 100 * (1 - min(1, penalty / budget[dimension]))

A dimension with no computable signal scores ``None`` and is dropped from the
weighted average rather than being treated as perfect.

The one deliberate departure from the formula: an open ``CRITICAL`` finding
clamps the overall score to ``config.critical_grade_cap`` (default 89.0). A
fixed penalty in one dimension gets diluted by the weighted average, so a repo
with a known-exploitable dependency could otherwise grade an A. The clamp only
lowers a score, and it annotates both the scorecard and the owning dimension so
the report never disagrees with its own arithmetic.
"""

from __future__ import annotations

from .config import SEVERITY_PENALTY, Config
from .model import Dimension, DimensionScore, Finding, Scorecard, Severity, grade_for

__all__ = ["SEVERITY_PENALTY", "score_findings", "summarize_penalties"]


def summarize_penalties(findings: list[Finding]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for finding in findings:
        totals[finding.dimension.value] = totals.get(finding.dimension.value, 0.0) + finding.penalty
    return totals


def score_findings(
    findings: list[Finding],
    config: Config,
    available: set[Dimension],
    notes: dict[Dimension, str] | None = None,
) -> Scorecard:
    notes = notes or {}
    totals = summarize_penalties(findings)
    counts: dict[Dimension, int] = {}
    for finding in findings:
        counts[finding.dimension] = counts.get(finding.dimension, 0) + 1

    dimension_scores: list[DimensionScore] = []
    weighted_sum = 0.0
    weight_total = 0.0
    any_missing = False

    for dimension in Dimension:
        weight = config.weight(dimension)
        count = counts.get(dimension, 0)
        if dimension not in available:
            any_missing = True
            dimension_scores.append(
                DimensionScore(
                    dimension=dimension,
                    score=None,
                    penalty=0.0,
                    finding_count=count,
                    weight=0.0,
                    note=notes.get(dimension, "signals unavailable"),
                )
            )
            continue

        penalty = totals.get(dimension.value, 0.0)
        budget = config.budget(dimension)
        score = 100.0 * (1.0 - min(1.0, penalty / budget)) if budget > 0 else 100.0
        dimension_scores.append(
            DimensionScore(
                dimension=dimension,
                score=score,
                penalty=penalty,
                finding_count=count,
                weight=weight,
                grade=grade_for(score, config.grade_bands),
                note=notes.get(dimension, ""),
            )
        )
        weighted_sum += score * weight
        weight_total += weight

    if weight_total > 0:
        overall = weighted_sum / weight_total
    else:
        overall = None

    if any_missing and weight_total > 0:
        covered = 100.0 * weight_total / max(1e-9, sum(config.weight(d) for d in Dimension))
        for entry in dimension_scores:
            if entry.score is not None and covered < 100.0:
                entry.note = (entry.note + " " if entry.note else "") + (
                    f"overall weighted over {covered:.0f}% of configured weight"
                ).strip()

    grade_note = ""
    overall, grade_note = _apply_critical_cap(overall, findings, config, dimension_scores)

    return Scorecard(
        overall=overall,
        grade=grade_for(overall, config.grade_bands),
        dimensions=dimension_scores,
        note=grade_note,
    )


def _apply_critical_cap(
    overall: float | None,
    findings: list[Finding],
    config: Config,
    dimension_scores: list[DimensionScore],
) -> tuple[float | None, str]:
    """Clamp the weighted average when an open critical finding exists.

    This is a deliberate override of the weighted average. A critical finding
    carries a fixed penalty, so in a large repo it can be diluted to nothing --
    grading an A on a project with a known-exploitable dependency reads as a
    bug. The clamp only ever lowers the score, never raises it, and both the
    scorecard note and the owning dimension's note say so, so the report never
    silently disagrees with its own arithmetic.
    """
    cap = config.critical_grade_cap
    if cap is None or overall is None:
        return overall, ""

    criticals = [f for f in findings if f.severity is Severity.CRITICAL]
    if not criticals or overall <= cap:
        return overall, ""

    rule_ids = ", ".join(sorted({f.id for f in criticals}))
    count = len(criticals)
    plural = "" if count == 1 else "s"
    verb = "is" if count == 1 else "are"
    note = (
        f"overall capped at {cap:.1f} despite a weighted average of {overall:.1f}: "
        f"{count} open critical finding{plural} ({rule_ids}) {verb} diluted by "
        f"the other dimensions"
    )

    by_dimension: dict[Dimension, list[str]] = {}
    for finding in criticals:
        by_dimension.setdefault(finding.dimension, []).append(finding.id)
    for entry in dimension_scores:
        owners = by_dimension.get(entry.dimension)
        if not owners:
            continue
        detail = f"critical finding here: {', '.join(sorted(set(owners)))}"
        entry.note = f"{entry.note}; {detail}" if entry.note else detail

    return cap, note


def severity_at_least(finding: Finding, threshold: Severity) -> bool:
    return finding.severity.rank >= threshold.rank


def cap_by_rule(findings: list[Finding], cap: int) -> tuple[list[Finding], dict[str, int]]:
    """Keep at most ``cap`` findings per rule id, preserving overall order.

    Returns the kept findings and a map of rule id -> how many were dropped.
    """
    if cap <= 0:
        return list(findings), {}
    seen: dict[str, int] = {}
    kept: list[Finding] = []
    dropped: dict[str, int] = {}
    for finding in findings:
        count = seen.get(finding.id, 0)
        if count >= cap:
            dropped[finding.id] = dropped.get(finding.id, 0) + 1
            # Fold the excess into the kept finding's occurrence count later.
            continue
        seen[finding.id] = count + 1
        kept.append(finding)
    return kept, dropped


def fold_occurrences(findings: list[Finding], dropped: dict[str, int]) -> list[Finding]:
    """Attribute capped findings to the last kept finding of each rule.

    Each kept finding already counts itself, so adding the dropped count to
    exactly one finding per rule makes ``occurrences`` the true total.
    """
    if not dropped:
        return findings
    last_index: dict[str, int] = {}
    for index, finding in enumerate(findings):
        last_index[finding.id] = index
    for rule_id, extra in dropped.items():
        index = last_index.get(rule_id)
        if index is not None:
            findings[index].occurrences += extra
    return findings
