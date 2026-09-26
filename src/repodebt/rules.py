"""Catalog of every rule the auditor can emit.

Analyzers reference this registry for titles and remediation text so that
``repodebt --explain <id>`` always has something useful to say, and so rule
metadata lives in one reviewable place instead of being scattered across
analyzer bodies.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Confidence, Dimension, Severity


@dataclass(frozen=True)
class Rule:
    id: str
    dimension: Dimension
    severity: Severity
    title: str
    why: str
    remediation: str


def _rule(
    id: str,
    dimension: Dimension,
    severity: Severity,
    title: str,
    why: str,
    remediation: str,
) -> Rule:
    return Rule(id, dimension, severity, title, why, remediation)


RULES: dict[str, Rule] = {}


def _add(*rules: Rule) -> None:
    for rule in rules:
        RULES[rule.id] = rule


# --------------------------------------------------------------------------
# Complexity
# --------------------------------------------------------------------------

_add(
    _rule(
        "complexity.large_file",
        Dimension.COMPLEXITY,
        Severity.MEDIUM,
        "File is unusually long",
        "Long files concentrate change, raise review cost, and make merge conflicts and regressions more likely.",
        "Split along feature boundaries. Extract cohesive units into their own modules and keep public surfaces small.",
    ),
    _rule(
        "complexity.deep_nesting",
        Dimension.COMPLEXITY,
        Severity.MEDIUM,
        "Deeply nested control flow",
        "Deep nesting is the most reliable predictor of defects. Every extra level hides a branch that is easy to miss in review.",
        "Invert conditions and use early returns to flatten the happy path, then extract the nested block into a named function.",
    ),
    _rule(
        "complexity.long_function",
        Dimension.COMPLEXITY,
        Severity.MEDIUM,
        "Function is long",
        "Long functions are hard to test in isolation and hard to reason about without reading the whole body.",
        "Extract cohesive steps into small functions with explicit names. Prefer several short units over one long procedure.",
    ),
    _rule(
        "complexity.complex_function",
        Dimension.COMPLEXITY,
        Severity.MEDIUM,
        "Function has high branch complexity",
        "Branch count approximates the number of independent paths through a function, and therefore the number of test cases needed.",
        "Replace branching with polymorphism or a lookup table, or split the decision into smaller single-purpose functions.",
    ),
    _rule(
        "complexity.many_parameters",
        Dimension.COMPLEXITY,
        Severity.LOW,
        "Function takes many parameters",
        "Long parameter lists are a symptom of a missing abstraction and make call sites error-prone.",
        "Group related parameters into a value object, or pass a small dependency object instead of positional arguments.",
    ),
    _rule(
        "complexity.long_lines",
        Dimension.COMPLEXITY,
        Severity.LOW,
        "High proportion of very long lines",
        "Long lines usually mean logic was packed onto one line to avoid wrapping or renaming, which defeats refactoring tools.",
        "Reformat, then extract the expression into a well-named variable or function.",
    ),
    _rule(
        "complexity.duplication",
        Dimension.COMPLEXITY,
        Severity.MEDIUM,
        "Duplicated code block",
        "Copied logic drifts. A fix applied in one copy silently leaves the others wrong.",
        "Extract the shared logic into a single function or module and have all copies call it.",
    ),
    _rule(
        "complexity.parse_error",
        Dimension.COMPLEXITY,
        Severity.INFO,
        "File could not be parsed",
        "Metrics for this file are missing rather than wrong. Reported so gaps are visible instead of silent.",
        "If this file is meant to be Python, fix the syntax error. Otherwise ignore it.",
    ),
    _rule(
        "complexity.empty_file",
        Dimension.COMPLEXITY,
        Severity.INFO,
        "File has no executable content",
        "Empty or comment-only files add navigation noise without behaviour.",
        "Delete the file, or add the content that was intended.",
    ),
)


# --------------------------------------------------------------------------
# Hotspots
# --------------------------------------------------------------------------

_add(
    _rule(
        "hotspots.high_churn_complex",
        Dimension.HOTSPOTS,
        Severity.HIGH,
        "Hotspot: frequently changed and complex",
        "Change frequency multiplied by complexity is the strongest available predictor of where defects and review bottlenecks live.",
        "Refactor before the next change. These files benefit most from smaller, better-factored units and dedicated reviewers.",
    ),
    _rule(
        "hotspots.sole_owner",
        Dimension.HOTSPOTS,
        Severity.MEDIUM,
        "Hotspot has a single historical owner",
        "A hot file touched by one person is a bus-factor risk even when that person is available today.",
        "Pair on the file, or document its design intent so another contributor can safely change it.",
    ),
    _rule(
        "hotspots.history_unavailable",
        Dimension.HOTSPOTS,
        Severity.INFO,
        "Change history unavailable",
        "Churn cannot be measured without git history, so hotspot ranking is disabled rather than guessed.",
        "Run against a git clone with full history.",
    ),
)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

_add(
    _rule(
        "tests.low_ratio",
        Dimension.TESTS,
        Severity.HIGH,
        "Very few tests relative to source",
        "A low test-to-source ratio means most changes ship unverified and regressions are found by users.",
        "Add characterisation tests around the most critical paths first, then grow coverage outward from them.",
    ),
    _rule(
        "tests.no_counterpart",
        Dimension.TESTS,
        Severity.MEDIUM,
        "Source area has no corresponding tests",
        "Code with no adjacent tests is where coverage quietly stops being maintained.",
        "Add a test module mirroring this source area, even if it starts with a single smoke test.",
    ),
    _rule(
        "tests.low_assertion_density",
        Dimension.TESTS,
        Severity.MEDIUM,
        "Tests make few assertions",
        "Tests that rarely assert tend to pass regardless of behaviour, giving false confidence.",
        "Assert observable outcomes rather than just calling the code. Remove tests that cannot fail.",
    ),
    _rule(
        "tests.skipped_leftover",
        Dimension.TESTS,
        Severity.HIGH,
        "Focused or skipped test left in the suite",
        "A stray `.only` or `xit` silently disables coverage for everyone else running the suite.",
        "Re-enable the test, or delete it and open an issue so the gap stays visible.",
    ),
    _rule(
        "tests.flaky_smell",
        Dimension.TESTS,
        Severity.MEDIUM,
        "Test contains a flakiness smell",
        "Sleeps, wall-clock reads, and unseeded randomness produce tests that pass or fail depending on machine load.",
        "Replace sleeps with explicit waits on a condition, inject the clock, and seed or inject randomness.",
    ),
    _rule(
        "tests.stale",
        Dimension.TESTS,
        Severity.MEDIUM,
        "Test file unchanged while its source changed",
        "A test that has not moved while the code under it churned is unlikely to still describe current behaviour.",
        "Re-read the test and confirm it still matches the code. Update or delete it.",
    ),
    _rule(
        "tests.large_test_file",
        Dimension.TESTS,
        Severity.LOW,
        "Test file is unusually large",
        "Oversized test files are slow to run and hard to navigate, which discourages people from adding tests nearby.",
        "Split by behaviour under test, mirroring the structure of the source it covers.",
    ),
    _rule(
        "tests.empty_test",
        Dimension.TESTS,
        Severity.MEDIUM,
        "Test body is empty",
        "An empty test passes unconditionally and is counted as coverage without providing any.",
        "Delete it, or write the assertion it was meant to make.",
    ),
    _rule(
        "tests.no_framework",
        Dimension.TESTS,
        Severity.LOW,
        "No test framework detected",
        "Test files exist but no runner or framework could be identified from the dependency manifests.",
        "Declare the test framework as a development dependency so tooling can discover and run the suite.",
    ),
    _rule(
        "tests.history_unavailable",
        Dimension.TESTS,
        Severity.INFO,
        "Staleness could not be measured",
        "Comparing test and source modification dates needs change history.",
        "Run against a git clone with full history.",
    ),
)


# --------------------------------------------------------------------------
# Repo hygiene
# --------------------------------------------------------------------------

_add(
    _rule(
        "hygiene.bus_factor",
        Dimension.HYGIENE,
        Severity.HIGH,
        "Low bus factor",
        "If most work comes from very few people, the project cannot absorb their absence or review their changes.",
        "Spread ownership deliberately: rotate reviewers, pair on core areas, and document architecture so others can contribute.",
    ),
    _rule(
        "hygiene.single_author",
        Dimension.HYGIENE,
        Severity.HIGH,
        "Repository has a single author",
        "A single-author repository has no second pair of eyes and no continuity if that person leaves.",
        "Invite outside contributors, run code review, and consider pair programming on core changes.",
    ),
    _rule(
        "hygiene.dormant",
        Dimension.HYGIENE,
        Severity.MEDIUM,
        "No recent commits",
        "An inactive repository accumulates rot: dependencies drift, CI breaks silently, and assumptions expire.",
        "Resume maintenance, or archive the repository and mark it clearly as unmaintained.",
    ),
    _rule(
        "hygiene.long_dry_spell",
        Dimension.HYGIENE,
        Severity.LOW,
        "Long gap between commits",
        "Long gaps usually mean a stalled workstream or a batch of unreviewed changes landing at once.",
        "Break work into smaller, more frequent changes.",
    ),
    _rule(
        "hygiene.conventional_commits",
        Dimension.HYGIENE,
        Severity.LOW,
        "Commit messages do not follow a convention",
        "Inconsistent messages make history hard to search, hard to review, and hard to generate changelogs from.",
        "Adopt Conventional Commits or an equivalent format and enforce it with a commit hook.",
    ),
    _rule(
        "hygiene.merge_ratio",
        Dimension.HYGIENE,
        Severity.INFO,
        "Mostly linear or mostly merge history",
        "Merge-commit ratio is a weak but useful proxy for review workflow shape; extreme values usually mean tooling defaults rather than team choice.",
        "No action required unless this is unintentional. Note that squashed rebases also produce a low merge ratio.",
    ),
    _rule(
        "hygiene.long_lived_branches",
        Dimension.HYGIENE,
        Severity.MEDIUM,
        "Long-lived unmerged branches",
        "Branches that diverge for a long time accumulate conflicts and hide unfinished work.",
        "Merge, rebase, or delete them. Long-lived branches should be short-lived by policy.",
    ),
    _rule(
        "hygiene.missing_file",
        Dimension.HYGIENE,
        Severity.LOW,
        "Expected repository file is missing",
        "Standard project files carry conventions that both people and tooling rely on.",
        "Add the missing file if it applies to this project.",
    ),
    _rule(
        "hygiene.large_blob",
        Dimension.HYGIENE,
        Severity.MEDIUM,
        "Large file committed to the repository",
        "Large tracked files bloat clone time and history permanently, because history retains them even after deletion.",
        "Move large or regenerable files out of version control and add them to .gitignore.",
    ),
    _rule(
        "hygiene.no_readme",
        Dimension.HYGIENE,
        Severity.MEDIUM,
        "No usable README",
        "Without a README, nobody outside the original authors can build, test, or deploy the project.",
        "Write a README covering what the project is, how to install it, and how to run its tests.",
    ),
    _rule(
        "hygiene.no_ci",
        Dimension.HYGIENE,
        Severity.MEDIUM,
        "No continuous integration configuration",
        "Without CI, nothing verifies that the default branch is buildable or that tests pass.",
        "Add a CI workflow that installs dependencies and runs the test suite on every push.",
    ),
    _rule(
        "hygiene.history_unavailable",
        Dimension.HYGIENE,
        Severity.INFO,
        "History-based hygiene signals unavailable",
        "Cadence, ownership, and workflow signals all require git history.",
        "Run against a git clone with full history.",
    ),
)


# --------------------------------------------------------------------------
# Dependency risk
# --------------------------------------------------------------------------

_add(
    _rule(
        "deps.vulnerability",
        Dimension.DEPS,
        Severity.CRITICAL,
        "Known vulnerability in a declared dependency",
        "A published advisory applies to a version this project declares, so the weakness is present in the shipped artifact.",
        "Upgrade to the first fixed version. If no fix exists, evaluate mitigations or replace the dependency.",
    ),
    _rule(
        "deps.stale_lock",
        Dimension.DEPS,
        Severity.MEDIUM,
        "Lockfile is older than the manifest",
        "A lockfile that predates the manifest means the pinned versions no longer match what is declared, and builds are not reproducible.",
        "Regenerate the lockfile and commit it.",
    ),
    _rule(
        "deps.missing_lock",
        Dimension.DEPS,
        Severity.MEDIUM,
        "No lockfile found",
        "Without a lockfile, two installs of the same commit can produce different dependency trees.",
        "Commit a lockfile for the ecosystem.",
    ),
    _rule(
        "deps.unpinned_prod",
        Dimension.DEPS,
        Severity.MEDIUM,
        "Production dependency is not pinned to an exact version",
        "Ranges resolve to whatever is newest at install time, so builds are not reproducible and new transitive versions arrive unvetted.",
        "Pin exact versions, and let the lockfile carry transitive resolution.",
    ),
    _rule(
        "deps.version_skew",
        Dimension.DEPS,
        Severity.MEDIUM,
        "Same dependency declared at different versions",
        "Divergent declarations of one dependency across manifests guarantee a larger, more fragile install than intended.",
        "Converge on one version across all manifests.",
    ),
    _rule(
        "deps.duplicate_declaration",
        Dimension.DEPS,
        Severity.LOW,
        "Dependency declared more than once",
        "Duplicate declarations are usually a merge artefact and make the intended version ambiguous.",
        "Keep a single declaration.",
    ),
    _rule(
        "deps.dev_in_prod",
        Dimension.DEPS,
        Severity.LOW,
        "Development-only dependency in the production set",
        "Test and tooling dependencies shipped into production enlarge the attack surface for no benefit.",
        "Move the dependency to the development or optional dependency group.",
    ),
    _rule(
        "deps.old_release",
        Dimension.DEPS,
        Severity.LOW,
        "Dependency is far behind its current release",
        "Very old versions accumulate known issues and miss years of fixes and platform support.",
        "Schedule an upgrade, testing against the changelog for breaking changes.",
    ),
    _rule(
        "deps.parse_error",
        Dimension.DEPS,
        Severity.LOW,
        "Dependency manifest could not be parsed",
        "Dependencies in this manifest are invisible to the audit.",
        "Check the file for syntax errors or unusual formatting.",
    ),
    _rule(
        "deps.uncatalogued",
        Dimension.DEPS,
        Severity.INFO,
        "Dependency intelligence unavailable offline",
        "Release dates and published advisories require network access and are not bundled with the tool.",
        "Re-run with --online to fetch advisories and release ages.",
    ),
)


def get(rule_id: str) -> Rule | None:
    return RULES.get(rule_id)


def for_dimension(dimension: Dimension) -> list[Rule]:
    return [r for r in RULES.values() if r.dimension is dimension]


__all__ = [
    "Rule",
    "RULES",
    "get",
    "for_dimension",
    "Confidence",
    "Severity",
    "Dimension",
]
