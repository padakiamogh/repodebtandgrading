"""Audit orchestration: walk, analyze, score, and assemble the report."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import config as cfg
from . import walker
from .analyzers import REGISTRY, Context
from .analyzers.base import Timer
from .gitlog import GitRepo
from .model import Dimension, Finding, Report, RepoInfo, Scorecard
from .scoring import cap_by_rule, fold_occurrences, score_findings

#: Dimensions whose availability is decided per-analyzer rather than globally.
_ALWAYS_AVAILABLE = {Dimension.HYGIENE}


def run_audit(
    root: Path,
    config: cfg.Config,
    use_git: bool = True,
    online: bool = False,
    verbose: bool = False,
    only: set[str] | None = None,
    skip: set[str] | None = None,
) -> Report:
    started = time.perf_counter()
    root = root.resolve()

    with Timer("walk", verbose) as walk_timer:
        walk = walker.discover(root, config, use_git=use_git)

    git = GitRepo(root, enabled=use_git)
    ctx = Context(
        root=root,
        files=walk.files,
        config=config,
        git=git,
        now=time.time(),
        online=online,
        verbose=verbose,
    )

    findings: list[Finding] = []
    metrics: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    available: set[Dimension] = set()
    notes_by_dimension: dict[Dimension, str] = {}

    for cls in REGISTRY:
        analyzer = cls()
        if only and analyzer.name not in only:
            continue
        if skip and analyzer.name in skip:
            continue
        with Timer(analyzer.name, verbose) as timer:
            if not analyzer.available(ctx):
                reason = analyzer.unavailability_reason(ctx)
                notes_by_dimension[analyzer.dimension] = reason
                unavailable[f"{analyzer.dimension.value}_analysis"] = reason
                continue
            result = analyzer.analyze(ctx)
        available.add(analyzer.dimension)
        findings.extend(result.findings)
        unavailable.update(result.unavailable)
        for key, value in result.metrics.items():
            metrics.setdefault(analyzer.name, {}).setdefault(key, value)
        metrics[analyzer.name]["_elapsed_ms"] = round(timer.elapsed * 1000, 1)
        for note in result.metrics.get("notes", []) or []:
            notes_by_dimension[analyzer.dimension] = note

    findings.sort(key=lambda f: f.sort_key())
    kept, dropped = cap_by_rule(findings, config.max_findings_per_rule)
    findings = fold_occurrences(kept, dropped)
    if dropped:
        metrics.setdefault("run", {})["findings_capped"] = {
            rule: count for rule, count in sorted(dropped.items())
        }

    scorecard: Scorecard = score_findings(findings, config, available, notes_by_dimension)

    summary = walker.summarize(walk.files, walk.excluded, walk.truncated)
    repo = RepoInfo(
        root=str(root),
        name=root.name,
        is_git_repo=git.available,
        head=git.head,
        branch=git.branch,
        file_count=summary["file_count"],
        source_file_count=summary["source_file_count"],
        excluded_file_count=summary["excluded_file_count"],
        total_bytes=summary["total_bytes"],
        languages=summary["languages"],
        truncated=summary["truncated"],
        notes=[f"file discovery: {walk.source}"],
    )
    if not git.available and use_git:
        repo.notes.append(f"git history unavailable: {git.reason}")

    metrics.setdefault("run", {})["file_discovery"] = walk.source
    metrics["run"]["walk_ms"] = round(walk_timer.elapsed * 1000, 1)

    return Report(
        repo=repo,
        scorecard=scorecard,
        metrics=metrics,
        findings=findings,
        unavailable=unavailable,
        duration_seconds=time.perf_counter() - started,
        config_digest=config.digest(),
    )
