"""Test health analysis.

Everything here is derived from the working tree. The one signal that needs
git history - tests that stopped moving while their source kept changing - is
reported as unavailable rather than guessed when history is missing.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from typing import Any

from ..model import Confidence, Dimension, Location
from .base import Analyzer, Context, Result, make_finding, pct

# Focused / skipped tests that silently disable coverage for everyone else.
_SKIP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(describe|it|test)\s*\.\s*(only|skip)\b"), "focused or skipped test"),
    (re.compile(r"\bf(it|describe)\s*\("), "focused test"),
    (re.compile(r"\bx(it|describe)\s*\("), "skipped test"),
    (re.compile(r"\bf?test\s*\.\s*only\b"), "focused test"),
    (re.compile(r"@Ignore\b|@pytest\.mark\.skip\b|@pytest\.mark\.xfail\b|@unittest\.skip\b"),
     "skipped test"),
    (re.compile(r"\bT\.Skip\s*\(|\bxit\s*\(|\bxdescribe\s*\("), "skipped test"),
    (re.compile(r"\b(todo|pending)\s*\("), "pending test"),
    (re.compile(r"\bxit\s*\(|\bxcontext\s*\("), "skipped test"),
    (re.compile(r"#\s*(type:\s*ignore)?\s*skip"), "skipped test"),
    (re.compile(r"\bdescribe\s*\.\s*skip\b"), "skipped test"),
    (re.compile(r"\bit\s*\.\s*todo\b"), "todo test"),
)

# Patterns that make a test non-deterministic.
_FLAKY_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\btime\.sleep\s*\("), "time.sleep", "wait for a condition instead of a fixed duration"),
    (re.compile(r"\bnew\s+Promise\s*\(\s*\w*\s*=>\s*setTimeout"),
     "setTimeout in a test", "use fake timers or await an explicit condition"),
    (re.compile(r"\bThread\.Sleep\s*\("), "Thread.Sleep", "poll for the condition instead"),
    (re.compile(r"\bsleep\s+\d"), "shell sleep", "poll for the condition instead"),
    (re.compile(r"\bMath\.random\s*\("), "Math.random", "seed the generator or inject the value"),
    (re.compile(r"\brandom\.(random|randint|choice|shuffle|randrange)\s*\("),
     "unseeded random", "seed the generator or inject the value"),
    (re.compile(r"\brandom\.seed\s*\(\s*\)"), "unseeded random", "pass a fixed seed"),
    (re.compile(r"\bdatetime\.(now|today|utcnow)\s*\("), "wall-clock read",
     "inject a fixed clock"),
    (re.compile(r"\bDate\.now\s*\("), "wall-clock read", "inject a fixed clock"),
    (re.compile(r"\btime\.time\s*\(\s*\)"), "wall-clock read", "inject a fixed clock"),
    (re.compile(r"\bnew\s+Date\s*\(\s*\)"), "wall-clock read", "inject a fixed clock"),
    (re.compile(r"\blocalhost:\d{2,5}"), "hardcoded port",
     "bind to an ephemeral port so parallel runs do not collide"),
    (re.compile(r"\bSystem\.currentTimeMillis\s*\(\s*\)"), "wall-clock read",
     "inject a fixed clock"),
    (re.compile(r"\bassert\s+.*\bwithin\s+\d"), "timing assertion",
     "widen the tolerance or assert on a deterministic value"),
)

_ASSERT_TOKENS = (
    r"\bassert\b", r"\bassertEqual\b", r"\bassertTrue\b", r"\bassertFalse\b",
    r"\bassertThrows\b", r"\bassert[A-Z]\w*\b", r"\bexpect\s*\(", r"\bshould\b",
    r"\btoEqual\b", r"\btoBe\b", r"\btoThrow\b", r"\bExpect\s*\(", r"\bAssert\.\w+",
    r"\brequire\b", r"\bassert_equals\b", r"\bcheck_that\b", r"\bmust_equal\b",
    r"\brequirem?ents?\b", r"\bRSpec\.expect\b", r"\bassertThat\b",
)
_ASSERT_RE = re.compile("|".join(_ASSERT_TOKENS))

_EMPTY_BODY_RE = re.compile(
    r"(?:def\s+test_\w+|func\s+Test\w+|fn\s+\w+)\s*\([^)]*\)\s*(?:\([^)]*\))?\s*:\s*$",
    re.MULTILINE,
)

# Dependency names that reveal the test framework in use.
_FRAMEWORK_HINTS: dict[str, str] = {
    "pytest": "pytest", "unittest2": "unittest", "nose": "nose", "hypothesis": "hypothesis",
    "jest": "jest", "vitest": "vitest", "mocha": "mocha", "jasmine": "jasmine",
    "ava": "ava", "tap": "node-tap", "@playwright/test": "playwright",
    "cypress": "cypress", "testing-library": "testing-library",
    "junit": "JUnit", "junit-jupiter": "JUnit 5", "testng": "TestNG",
    "mockito": "Mockito", "rspec": "RSpec", "minitest": "Minitest", "cucumber": "Cucumber",
    "googletest": "GoogleTest", "catch2": "Catch2", "ginkgo": "Ginkgo", "vitest-": "Vitest",
}


def _assertion_count(text: str) -> int:
    return len(_ASSERT_RE.findall(text))


def _dir_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else "."


def _counterpart_dirs(path: str) -> list[str]:
    """Directory names that would hold tests for ``path``."""
    parts = path.split("/")
    out = []
    for index, part in enumerate(parts[:-1]):
        if part in ("src", "lib", "app", "source", "pkg", "internal", "packages", "apps"):
            continue
        out.append("/".join(parts[: index + 1]))
    return out


def _test_dirs_for(source_dir: str) -> set[str]:
    """Where tests for a source directory would plausibly live."""
    base = source_dir if source_dir != "." else ""
    candidates = set()
    if base:
        candidates.add(f"{base}/tests")
        candidates.add(f"{base}/test")
        candidates.add(f"tests/{base}")
        candidates.add(f"test/{base}")
        candidates.add(f"{base}/__tests__")
        head, _, tail = base.rpartition("/")
        if tail in ("src", "lib"):
            stem = head
            candidates.add(f"{stem}/tests/{tail}" if stem else f"tests/{tail}")
            candidates.add(f"{stem}/test/{tail}" if stem else f"test/{tail}")
    else:
        candidates.update({"tests", "test", "spec", "__tests__"})
    return candidates


def _empty_test_lines(text: str) -> list[int]:
    """Line numbers of test functions whose body asserts nothing.

    A body counts as empty when it holds no executable statement: a docstring,
    comments and bare ``pass``/``...`` placeholders do not make a test able to
    fail. Anything else -- a call, an assignment, a loop -- is real work, even
    if the assertions live in a helper it calls.

    This walks the AST rather than matching the source, because a body is
    delimited by indentation and a single-line regex cannot see past a leading
    docstring: ``def test_x():\\n    \\"\\"\\"docs\\"\\"\\"\\n    assert f() == 1``
    has code, but every regex that looks for it right after the colon reports
    an empty body and flags a perfectly good test.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []

    empty: list[int] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_test_name(child.name) and _body_is_empty(child):
                    empty.append(child.lineno)
            visit(child)

    visit(tree)
    return empty


def _is_test_name(name: str) -> bool:
    return name.startswith("test_") or name.startswith("_test") or name == "test"


def _body_is_empty(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [
        stmt
        for stmt in func.body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    if not body:
        return True
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        return False
    return True


class TestsAnalyzer(Analyzer):
    dimension = Dimension.TESTS
    name = "tests"

    def available(self, ctx: Context) -> bool:
        return bool(ctx.source_files(include_tests=True))

    def analyze(self, ctx: Context) -> Result:
        config = ctx.config
        findings: list = []

        source = ctx.source_files(include_tests=False)
        tests = ctx.test_files()
        min_ratio = float(config.get("test_min_ratio", 0.15))
        min_assertions = float(config.get("test_min_assertions_per_100_lines", 2.0))
        max_test_lines = int(config.get("test_file_lines", 800))

        stats_by_file: dict[str, Any] = ctx.scratch.get("complexity_by_file", {})

        def code_lines(files: list) -> int:
            total = 0
            for info in files:
                stat = stats_by_file.get(info.rel)
                total += stat.code_lines if stat else len(info.text().splitlines())
            return total

        source_code = code_lines(source)
        test_code = code_lines(tests)
        file_ratio = pct(len(tests), max(1, len(source)))
        loc_ratio = pct(test_code, max(1, source_code))

        # -- overall ratio ---------------------------------------------------
        if source and (file_ratio < min_ratio * 100 or loc_ratio < min_ratio * 100):
            severity_note = "no tests at all" if not tests else "very few tests"
            findings.append(make_finding(
                "tests.low_ratio",
                detail=(f"{len(tests)} test files against {len(source)} source files "
                        f"({file_ratio:.1f}%); {test_code} test code lines against "
                        f"{source_code} source lines ({loc_ratio:.1f}%). "
                        f"Configured minimum is {min_ratio:.0%}."),
                evidence=[
                    f"test files: {len(tests)}",
                    f"source files: {len(source)}",
                    f"test LOC ratio: {loc_ratio:.1f}%",
                ],
                locations=[Location(".")],
            ))
            if not tests:
                pass  # the detail already says it

        # -- uncovered source areas ------------------------------------------
        test_dir_set: set[str] = {_dir_of(t.rel) for t in tests}
        for candidate in _test_dirs_for(""):
            test_dir_set.add(candidate)
        # Add mirrored locations that do exist, e.g. src/pkg -> pkg/tests.
        existing_dirs = {p for p in {t.rel.rsplit("/", 1)[0] for t in tests} if p}
        for directory in list(existing_dirs):
            test_dir_set.add(directory)

        by_dir: dict[str, list] = defaultdict(list)
        for info in source:
            by_dir[_dir_of(info.rel)].append(info)

        uncovered: list[tuple[str, int]] = []
        for directory, files in sorted(by_dir.items()):
            if len(files) < 2:
                continue
            plausible = _test_dirs_for(directory)
            if plausible & test_dir_set:
                continue
            # A sibling test directory inside the same package counts.
            if f"{directory}/tests" in test_dir_set or f"{directory}/test" in test_dir_set:
                continue
            if directory.startswith(("tests/", "test/")):
                continue
            uncovered.append((directory, len(files)))
            findings.append(make_finding(
                "tests.no_counterpart",
                detail=(f"{directory} contains {len(files)} source files but no "
                        f"corresponding test directory. Looked for: "
                        f"{', '.join(sorted(plausible))}."),
                evidence=[f"source files: {len(files)}"],
                locations=[Location(directory if directory != "." else "<root>")],
            ))

        # -- per-test-file findings ------------------------------------------
        skipped_locations: list[Location] = []
        skipped_evidence: list[str] = []
        flaky_locations: list[Location] = []
        flaky_evidence: list[str] = []
        empty_locations: list[Location] = []
        total_assertions = 0

        for info in tests:
            text = info.text()
            stat = stats_by_file.get(info.rel)
            lines = stat.total_lines if stat else len(text.splitlines())
            code = stat.code_lines if stat else lines

            for pattern, label in _SKIP_PATTERNS:
                for match in pattern.finditer(text):
                    line_no = text.count("\n", 0, match.start()) + 1
                    skipped_locations.append(Location(info.rel, line_no))
                    skipped_evidence.append(f"{info.rel}:{line_no} {label}")
                    break

            for pattern, label, _fix in _FLAKY_PATTERNS:
                for match in pattern.finditer(text):
                    line_no = text.count("\n", 0, match.start()) + 1
                    flaky_locations.append(Location(info.rel, line_no))
                    flaky_evidence.append(f"{info.rel}:{line_no} {label}")
                    break

            assertions = _assertion_count(text)
            total_assertions += assertions
            if code >= 30 and assertions == 0:
                findings.append(make_finding(
                    "tests.low_assertion_density",
                    detail=(f"{info.rel} has {lines} lines and no recognisable "
                            f"assertions, so it cannot fail."),
                    evidence=[f"assertion-like tokens: 0"],
                    locations=[Location(info.rel)],
                ))

            if info.language and info.language.name == "Python":
                for line_no in _empty_test_lines(text):
                    empty_locations.append(Location(info.rel, line_no))

            if lines > max_test_lines:
                findings.append(make_finding(
                    "tests.large_test_file",
                    detail=f"{info.rel} is {lines} lines (threshold {max_test_lines}).",
                    locations=[Location(info.rel)],
                ))

        if skipped_locations:
            findings.append(make_finding(
                "tests.skipped_leftover",
                detail=(f"{len(skipped_locations)} test file(s) contain focused or "
                        f"skipped tests, which disables coverage for every other run."),
                evidence=skipped_evidence[:10],
                locations=skipped_locations[:20],
                occurrences=len(skipped_locations),
            ))

        if flaky_locations:
            findings.append(make_finding(
                "tests.flaky_smell",
                detail=(f"{len(flaky_locations)} test file(s) contain patterns that "
                        f"make results depend on timing or machine load."),
                evidence=flaky_evidence[:10],
                locations=flaky_locations[:20],
                occurrences=len(flaky_locations),
            ))

        if empty_locations:
            findings.append(make_finding(
                "tests.empty_test",
                detail=(f"{len(empty_locations)} test function(s) have an empty or "
                        f"comment-only body and pass unconditionally."),
                evidence=[loc.render() for loc in empty_locations[:10]],
                locations=empty_locations[:20],
                occurrences=len(empty_locations),
            ))

        # -- staleness --------------------------------------------------------
        unavailable: dict[str, str] = {}
        stale: list[tuple[str, float]] = []
        window_days = config.history_window_days
        if ctx.git.available:
            history = ctx.git.history(window_days)
            if history.files:
                cutoff = ctx.now - window_days * 86400
                test_last: dict[str, int] = {}
                source_last: dict[str, int] = {}
                for path, entry in history.files.items():
                    target = test_last if _is_test_path(path) else source_last
                    target[path] = max(target.get(path, 0), entry.last_timestamp)
                for path, last in test_last.items():
                    if last >= cutoff:
                        continue  # test changed inside the window
                    sibling = _nearest_source(path, source_last)
                    if sibling and source_last[sibling] > last:
                        age_days = (ctx.now - last) / 86400.0
                        source_age = (ctx.now - source_last[sibling]) / 86400.0
                        stale.append((path, age_days))
                        findings.append(make_finding(
                            "tests.stale",
                            detail=(f"{path} has not changed in {age_days:.0f} days "
                                    f"while {sibling} changed {source_age:.0f} days ago."),
                            evidence=[f"test last touched: {age_days:.0f}d ago"],
                            locations=[Location(path)],
                        ))
            else:
                unavailable["test_staleness"] = (
                    f"no commits in the last {window_days} days of history"
                )
        else:
            unavailable["test_staleness"] = (
                ctx.git.reason or "git history unavailable"
            )
            findings.append(make_finding(
                "tests.history_unavailable",
                detail=("Test staleness could not be measured because change history "
                        f"is unavailable ({unavailable['test_staleness']})."),
                locations=[Location(".")],
            ))

        # -- framework detection ---------------------------------------------
        frameworks = _detect_frameworks(ctx)
        if tests and not frameworks:
            findings.append(make_finding(
                "tests.no_framework",
                detail=("Test files are present but no test framework was found in "
                        "the dependency manifests."),
                locations=[Location(".")],
            ))

        assertion_density = pct(total_assertions * 100, max(1, test_code))
        if test_code >= 100 and assertion_density < min_assertions:
            findings.append(make_finding(
                "tests.low_assertion_density",
                detail=(f"The suite averages {assertion_density:.1f} assertions per "
                        f"100 test lines (configured minimum {min_assertions:.1f})."),
                evidence=[f"total assertions: {total_assertions}"],
                locations=[Location(".")],
            ))

        metrics: dict[str, Any] = {
            "test_files": len(tests),
            "source_files": len(source),
            "test_file_ratio": round(file_ratio, 1),
            "test_code_lines": test_code,
            "source_code_lines": source_code,
            "test_loc_ratio": round(loc_ratio, 1),
            "assertions": total_assertions,
            "assertions_per_100_lines": round(assertion_density, 2),
            "frameworks": frameworks,
            "uncovered_source_dirs": [d for d, _ in uncovered],
            "skipped_or_focused_files": len(skipped_locations),
            "flaky_smell_files": len(flaky_locations),
            "empty_test_files": len(empty_locations),
            "stale_test_files": len(stale),
        }

        return Result(findings=findings, metrics=metrics, unavailable=unavailable)


def _is_test_path(rel: str) -> bool:
    from ..walker import looks_like_test

    return looks_like_test(rel)


def _nearest_source(test_path: str, source_last: dict[str, int]) -> str | None:
    """Map a test path to the most plausible source file it covers."""
    base = test_path.rsplit("/", 1)[-1]
    stem = base
    for prefix in ("test_", "Test", "test"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    for suffix in ("_test", "Test", "Tests", "_spec", "Spec", "Specs", ".test", ".spec"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem.split(".")[0]
    if not stem:
        return None

    directory = test_path.rsplit("/", 1)[0] if "/" in test_path else ""
    candidates = []
    if directory:
        for replacement in ("", "/src", "/lib"):
            base_dir = directory + replacement
            if base_dir.endswith(("/tests", "/test", "/__tests__", "/spec")):
                base_dir = base_dir.rsplit("/", 1)[0]
            candidates.append(f"{base_dir}/{stem}")
    candidates.append(stem)
    for candidate in candidates:
        if candidate in source_last:
            return candidate
        for ext in (".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rb", ".java",
                    ".cs", ".php", ".rs", ".kt", ".dart", ".ex"):
            if candidate + ext in source_last:
                return candidate + ext
    return None


#: Python's own test runner needs no dependency, so a dependency scan can
#: never see it. A test file that imports it is using it, whatever the
#: manifests say.
_PY_BUILTIN_FRAMEWORKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*import\s+unittest\b", re.MULTILINE), "unittest"),
    (re.compile(r"^\s*from\s+unittest\s+import\b", re.MULTILINE), "unittest"),
    (re.compile(r"^\s*import\s+unittest2\b", re.MULTILINE), "unittest"),
    (re.compile(r"\bunittest\.TestCase\b"), "unittest"),
)


def _detect_frameworks(ctx: Context) -> list[str]:
    from .deps import collect_dependencies

    try:
        deps = collect_dependencies(ctx)
    except Exception:  # noqa: BLE001 - never let detection break the analyzer
        return []
    found: set[str] = set()
    for dep in deps:
        base = dep.name.split("/")[-1].split(":")[-1].lower()
        for hint, label in _FRAMEWORK_HINTS.items():
            if base == hint or base.startswith(hint):
                found.add(label)
    for info in ctx.test_files():
        if not (info.language and info.language.name == "Python"):
            continue
        try:
            text = info.text()
        except Exception:  # noqa: BLE001 - unreadable file is not a finding
            continue
        for pattern, label in _PY_BUILTIN_FRAMEWORKS:
            if pattern.search(text):
                found.add(label)
    return sorted(found)
