"""Repository hygiene: history shape, ownership, and project conventions.

History-derived signals degrade to an explicit ``unavailable`` entry when git
is missing, so a directory without history is never scored as if it were
healthy.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from ..model import Confidence, Dimension, Location, Severity
from .base import Analyzer, Context, Result, human_bytes, human_days, make_finding, pct
from .hotspots import mask_email

# Conventional Commits: type(optional-scope)!: subject
_CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]*\))?!?:\s+\S"
)
_TICKET = re.compile(r"(?:[A-Z]{2,}-\d+|#\d+|gh-\d+)")

#: (label, accepted filenames, severity, rule) for conventions worth having.
#:
#: Grouped by label rather than per filename on purpose: LICENSE, LICENSE.md
#: and LICENSE.txt are three spellings of the same expectation, and listing
#: them separately reported one missing license as three identical findings.
_EXPECTED_CONVENTIONS: tuple[tuple[str, tuple[str, ...], Severity, str], ...] = (
    ("a README", ("README.md", "README.rst", "README", "readme.md"),
     Severity.MEDIUM, "hygiene.no_readme"),
    ("a LICENSE", ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING"),
     Severity.MEDIUM, "hygiene.missing_file"),
    ("a CONTRIBUTING guide", ("CONTRIBUTING.md", "CONTRIBUTING", "CONTRIBUTING.rst"),
     Severity.LOW, "hygiene.missing_file"),
    ("CODEOWNERS", ("CODEOWNERS",),
     Severity.LOW, "hygiene.missing_file"),
    ("an .editorconfig", (".editorconfig",),
     Severity.LOW, "hygiene.missing_file"),
    ("a .gitignore", (".gitignore",),
     Severity.LOW, "hygiene.missing_file"),
)

CI_GLOBS = (
    ".github/workflows", ".gitlab-ci.yml", ".circleci", "azure-pipelines.yml",
    "Jenkinsfile", ".travis.yml", "appveyor.yml", ".drone.yml", "bitbucket-pipelines.yml",
    ".buildkite", "buildkite.yml", "Taskfile.yml", "justfile",
)

HOOK_GLOBS = (".pre-commit-config.yaml", ".pre-commit-config.yml", ".husky")


def _is_ci_config(rel: str) -> bool:
    return any(rel == pattern or rel.startswith(pattern) for pattern in CI_GLOBS)


def _is_hook_config(rel: str) -> bool:
    return any(rel == pattern or rel.startswith(pattern) for pattern in HOOK_GLOBS)


class HygieneAnalyzer(Analyzer):
    dimension = Dimension.HYGIENE
    name = "hygiene"

    def available(self, ctx: Context) -> bool:
        return True

    def analyze(self, ctx: Context) -> Result:
        config = ctx.config
        findings: list = []
        metrics: dict[str, Any] = {}
        unavailable: dict[str, str] = {}

        # ---- filesystem conventions (always available) ---------------------
        paths = {f.rel for f in ctx.files}
        names = {f.name for f in ctx.files}
        readme = next((p for p in ("README.md", "README.rst", "README", "readme.md")
                       if p in names or p in paths), None)
        readme_ok = False
        if readme:
            info = ctx.root / readme
            try:
                readme_ok = info.stat().st_size >= int(config.get("min_readme_chars", 200))
            except OSError:
                readme_ok = False
            if not readme_ok:
                findings.append(make_finding(
                    "hygiene.no_readme",
                    detail=(f"{readme} exists but is only "
                            f"{ctx.root.joinpath(readme).stat().st_size} bytes, which is "
                            f"too short to tell anyone how to build or test this project."),
                    locations=[Location(readme)],
                ))
        else:
            findings.append(make_finding(
                "hygiene.no_readme",
                detail="No README found in the repository root.",
                locations=[Location(".")],
            ))
        metrics["readme"] = readme or None
        metrics["readme_substantive"] = readme_ok

        missing: list[str] = []
        for label, candidates, severity, rule in _EXPECTED_CONVENTIONS:
            if any(p in paths or p in names for p in candidates) or any(
                any(p.endswith("/" + candidate) for p in paths | names)
                for candidate in candidates
            ):
                continue
            missing.append(label)
            findings.append(make_finding(
                rule,
                detail=f"Missing {label}.",
                severity=severity,
                locations=[Location(".")],
            ))
        metrics["missing_conventions"] = missing

        has_ci = any(_is_ci_config(p) for p in paths)
        has_hooks = any(_is_hook_config(p) for p in paths)
        if not has_ci:
            findings.append(make_finding(
                "hygiene.no_ci",
                detail=("No CI configuration found. Looked for "
                        + ", ".join(CI_GLOBS[:6]) + " and similar."),
                locations=[Location(".")],
            ))
        metrics["has_ci"] = has_ci
        metrics["has_pre_commit"] = has_hooks

        blob_limit = int(config.get("large_blob_bytes", 500 * 1024))
        large = sorted(
            ((f.rel, f.size) for f in ctx.files if f.size > blob_limit),
            key=lambda kv: -kv[1],
        )
        for rel, size in large[:10]:
            findings.append(make_finding(
                "hygiene.large_blob",
                detail=(f"{rel} is {human_bytes(size)}, over the "
                        f"{human_bytes(blob_limit)} threshold. Git keeps it in history "
                        f"even after you delete it."),
                evidence=[f"size: {human_bytes(size)}"],
                locations=[Location(rel)],
            ))
        metrics["large_files"] = [
            {"file": rel, "bytes": size, "human": human_bytes(size)} for rel, size in large[:20]
        ]
        metrics["large_file_count"] = len(large)

        # ---- history-derived signals ---------------------------------------
        if not ctx.git.available:
            reason = ctx.git.reason or "git history unavailable"
            unavailable.update({
                "commit_cadence": reason,
                "bus_factor": reason,
                "commit_convention": reason,
                "branch_lifetimes": reason,
            })
            findings.append(make_finding(
                "hygiene.history_unavailable",
                detail=(f"History-based hygiene signals were not computed: {reason}."),
                locations=[Location(".")],
            ))
        else:
            history = ctx.git.history(config.history_window_days)
            findings.extend(self._history_findings(ctx, history, metrics, unavailable))
            findings.extend(self._branch_findings(ctx, metrics))

        return Result(findings=findings, metrics=metrics, unavailable=unavailable)

    # -- history -----------------------------------------------------------
    def _history_findings(
        self, ctx: Context, history, metrics: dict[str, Any], unavailable: dict[str, str]
    ) -> list:
        config = ctx.config
        findings: list = []
        window_days = config.history_window_days

        # Dormancy is checked against the newest commit in the *whole* repo,
        # not the newest one inside the history window. A repo whose last
        # commit predates the window is precisely the dead repo this rule
        # exists to catch, and reading it off the windowed history would
        # report silence as "no data" instead of as the finding.
        last_stamp = ctx.git.last_commit_timestamp()
        if not last_stamp:
            unavailable["commit_cadence"] = "no commits on the current branch"
            return findings

        last_commit_days = (ctx.now - last_stamp) / 86400.0
        metrics["days_since_last_commit"] = round(last_commit_days, 1)
        if last_commit_days > config.dormant_days:
            findings.append(make_finding(
                "hygiene.dormant",
                detail=(f"The most recent commit is {human_days(last_commit_days)} old "
                        f"(threshold {config.dormant_days} days)."),
                evidence=[f"last commit: {human_days(last_commit_days)} ago"],
                locations=[Location(".")],
                severity=Severity.HIGH if last_commit_days > config.dormant_days * 3
                else Severity.MEDIUM,
            ))

        if not history.commits:
            unavailable["commit_cadence"] = (
                f"no commits in the last {window_days} days"
            )
            return findings

        # Cadence
        days_covered = max(1.0, (ctx.now - min(c.timestamp for c in history.commits)) / 86400.0)
        per_day = len(history.commits) / days_covered
        metrics["commits_in_window"] = len(history.commits)
        metrics["commits_per_day"] = round(per_day, 3)
        metrics["days_observed"] = round(days_covered, 1)

        dry = history.longest_dry_spell_days(ctx.now)
        metrics["longest_dry_spell_days"] = None if dry == float("inf") else round(dry, 1)
        if dry > config.dormant_branch_days:
            findings.append(make_finding(
                "hygiene.long_dry_spell",
                detail=(f"The longest gap between commits is {human_days(dry)} "
                        f"(threshold {config.dormant_branch_days} days)."),
                locations=[Location(".")],
                severity=Severity.MEDIUM if dry > config.dormant_branch_days * 2
                else Severity.LOW,
            ))

        # Ownership
        contributors = len(history.authors)
        metrics["contributors"] = contributors
        if contributors == 1:
            only = next(iter(history.authors))
            findings.append(make_finding(
                "hygiene.single_author",
                detail=(f"All {len(history.commits)} commits in the last {window_days} "
                        f"days come from a single author."),
                evidence=[f"author: {mask_email(only)}"],
                locations=[Location(".")],
            ))
        else:
            factor, share = history.bus_factor()
            metrics["bus_factor"] = factor
            metrics["bus_factor_share"] = round(share, 3)
            if factor < config.bus_factor_min_authors or share > 0.8:
                findings.append(make_finding(
                    "hygiene.bus_factor",
                    detail=(f"{factor} of {contributors} contributors account for "
                            f"{share:.0%} of commits in the last {window_days} days."),
                    evidence=[
                        f"contributors: {contributors}",
                        "top authors: " + ", ".join(
                            f"{mask_email(a)} ({c})"
                            for a, c in sorted(history.authors.items(),
                                               key=lambda kv: -kv[1])[:3]
                        ),
                    ],
                    locations=[Location(".")],
                ))

        # Unowned areas: files touched by one author, in aggregate
        owner_counts: Counter[str] = Counter()
        for entry in history.files.values():
            if entry.authors:
                owner_counts[max(entry.authors.items(), key=lambda kv: (kv[1], kv[0]))[0]] += 1
        unowned = sum(count for author, count in owner_counts.items()
                      if history.authors.get(author, 0) <= max(1, len(history.commits) * 0.05))
        metrics["single_owner_file_count"] = unowned
        metrics["file_owner_count"] = len(owner_counts)

        # Merge vs linear
        merges = sum(1 for c in history.commits if c.is_merge)
        total = len(history.commits)
        merge_ratio = merges / total if total else 0.0
        metrics["merge_commits"] = merges
        metrics["merge_ratio"] = round(merge_ratio, 3)
        if total >= 20 and (merge_ratio > config.max_merge_ratio or merge_ratio < config.min_merge_ratio):
            direction = "merge commits" if merge_ratio > config.max_merge_ratio else "linear history"
            findings.append(make_finding(
                "hygiene.merge_ratio",
                detail=(f"{merge_ratio:.0%} of commits are merge commits ({direction}). "
                        f"This is a weak proxy for review workflow shape."),
                evidence=[f"merges: {merges} of {total} commits"],
                locations=[Location(".")],
                confidence=Confidence.HEURISTIC,
            ))

        # Commit message quality
        subjects = [c.subject for c in history.commits]
        conventional = sum(1 for s in subjects if _CONVENTIONAL.match(s))
        with_ticket = sum(1 for s in subjects if _TICKET.search(s))
        long_subjects = sum(1 for s in subjects if len(s) > 72)
        conventional_ratio = pct(conventional, total)
        metrics["conventional_commit_ratio"] = round(conventional_ratio, 1)
        metrics["ticket_reference_ratio"] = round(pct(with_ticket, total), 1)
        metrics["avg_subject_length"] = round(
            sum(len(s) for s in subjects) / total, 1
        ) if total else 0
        if conventional_ratio < config.conventional_commit_min_ratio * 100:
            findings.append(make_finding(
                "hygiene.conventional_commits",
                detail=(f"{conventional} of {total} commit subjects "
                        f"({conventional_ratio:.0f}%) follow Conventional Commits, "
                        f"below the configured {config.conventional_commit_min_ratio:.0%}."),
                evidence=[
                    f"subjects over 72 chars: {long_subjects}",
                    f"with a ticket reference: {with_ticket} ({pct(with_ticket, total):.0f}%)",
                ],
                locations=[Location(".")],
            ))

        return findings

    # -- branches ----------------------------------------------------------
    def _branch_findings(self, ctx: Context, metrics: dict[str, Any]) -> list:
        config = ctx.config
        findings: list = []
        days = config.dormant_branch_days
        stale = ctx.git.branches_older_than(days, ctx.now)
        metrics["long_lived_branches"] = [
            {"branch": name, "days": round(age, 1)} for name, age in stale[:20]
        ]
        metrics["long_lived_branch_count"] = len(stale)
        for name, age in stale[:10]:
            findings.append(make_finding(
                "hygiene.long_lived_branches",
                detail=(f"Branch {name!r} has not been merged into the current branch "
                        f"for {human_days(age)} (threshold {days} days)."),
                locations=[Location(".")],
            ))
        return findings
