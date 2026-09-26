"""Test-health signals: empty test bodies, skip and flakiness patterns."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt.analyzers import tests as ta  # noqa: E402

from fixtures import TempDirCase  # noqa: E402


class EmptyTestBodyTest(unittest.TestCase):
    """A test with no executable statement cannot fail, however it is written.

    Detection walks the AST. Matching the source text cannot work here,
    because a function body is delimited by indentation and a leading
    docstring pushes the real code out of view of any one-line pattern --
    which previously made ``assert``-carrying tests look empty.
    """

    def flagged(self, source: str) -> list[int]:
        return ta._empty_test_lines(source)

    def test_pass_is_empty(self):
        self.assertEqual(self.flagged("def test_x():\n    pass\n"), [1])

    def test_docstring_only_is_empty(self):
        self.assertEqual(
            self.flagged('def test_x():\n    """Nothing asserted."""\n'), [1])

    def test_ellipsis_placeholder_is_empty(self):
        self.assertEqual(self.flagged("def test_x():\n    ...\n"), [1])

    def test_docstring_then_pass_is_still_empty(self):
        self.assertEqual(
            self.flagged('def test_x():\n    """Docs."""\n    pass\n'), [1])

    def test_docstring_then_assertion_is_not_empty(self):
        # This is the regression: a docstring used to hide the assertion.
        self.assertEqual(
            self.flagged('def test_x():\n    """Docs."""\n    assert 1 + 1 == 2\n'),
            [],
        )

    def test_docstring_then_loop_is_not_empty(self):
        self.assertEqual(
            self.flagged(
                'def test_x():\n    """Docs."""\n\n'
                "    for value in range(3):\n        assert value < 3\n"),
            [],
        )

    def test_work_in_a_helper_still_counts_as_a_body(self):
        # A bare call is code. Whether the helper asserts is a separate
        # question, and flagging every delegation would bury the real signal.
        self.assertEqual(
            self.flagged("def test_x():\n    self.check_everything()\n"), [])

    def test_async_test_with_a_body_is_not_empty(self):
        self.assertEqual(
            self.flagged("async def test_x():\n    assert True\n"), [])

    def test_async_test_that_is_empty_is_flagged(self):
        self.assertEqual(self.flagged("async def test_x():\n    pass\n"), [1])

    def test_tests_inside_a_class_are_found(self):
        source = "class T:\n    def test_x():\n        pass\n"
        self.assertEqual(self.flagged(source), [2])

    def test_one_empty_among_several_is_reported_on_its_own_line(self):
        source = "def test_a():\n    assert True\n\ndef test_b():\n    pass\n"
        self.assertEqual(self.flagged(source), [4])

    def test_every_empty_test_is_reported_not_just_the_first(self):
        source = "def test_a():\n    pass\n\ndef test_b():\n    pass\n"
        self.assertEqual(self.flagged(source), [1, 4])

    def test_underscore_test_names_are_candidates(self):
        self.assertEqual(self.flagged("def _test_helper():\n    pass\n"), [1])

    def test_non_test_functions_are_ignored(self):
        self.assertEqual(self.flagged("def helper():\n    pass\n"), [])

    def test_setup_method_is_not_mistaken_for_a_test(self):
        self.assertEqual(self.flagged("def setUp(self):\n    pass\n"), [])

    def test_syntax_error_is_survivable(self):
        # Unparseable files are the complexity analyzer's problem to report;
        # this one must not raise on them.
        self.assertEqual(self.flagged("def test_broken(:\n"), [])

    def test_empty_file_is_handled(self):
        self.assertEqual(self.flagged(""), [])

    def test_comment_only_body_is_a_syntax_error_not_an_empty_test(self):
        # Python rejects this outright, so it never reaches detection; the
        # file is reported as unparseable instead.
        self.assertEqual(self.flagged("def test_x():\n    # nothing\n"), [])


class FrameworkDetectionTest(TempDirCase):
    """Finding the runner in use, including the one with no dependency.

    ``unittest`` ships with Python, so no manifest can ever declare it. A
    dependency-only scan therefore reports "no test framework detected" for
    every stdlib-only project, which is a false positive rather than a gap in
    testing.
    """

    def test_stdlib_unittest_is_detected_without_any_dependency(self):
        self.write("app.py", "def run():\n    return 1\n")
        self.write("tests/test_app.py", (
            "import unittest\n"
            "\n"
            "from app import run\n"
            "\n"
            "\n"
            "class T(unittest.TestCase):\n"
            "    def test_run(self):\n"
            "        self.assertEqual(run(), 1)\n"
        ))
        report = self.audit()
        self.assertNotIn("tests.no_framework", self.rule_ids(report))
        self.assertIn("unittest", report.metrics["tests"]["frameworks"])

    def test_from_unittest_import_is_detected(self):
        self.write("app.py", "def run():\n    return 1\n")
        self.write("tests/test_app.py", (
            "from unittest import TestCase\n"
            "\n"
            "from app import run\n"
            "\n"
            "\n"
            "class T(TestCase):\n"
            "    def test_run(self):\n"
            "        self.assertEqual(run(), 1)\n"
        ))
        report = self.audit()
        self.assertNotIn("tests.no_framework", self.rule_ids(report))

    def test_declared_pytest_is_still_detected(self):
        self.write("app.py", "def run():\n    return 1\n")
        self.write("requirements-dev.txt", "pytest==7.4.4\n")
        self.write("tests/test_app.py", (
            "from app import run\n"
            "\n"
            "\n"
            "def test_run():\n"
            "    assert run() == 1\n"
        ))
        report = self.audit()
        self.assertIn("pytest", report.metrics["tests"]["frameworks"])

    def test_a_test_file_with_no_runner_still_reports_one(self):
        # Assertions without any recognised runner are worth flagging: nothing
        # here will actually execute them.
        self.write("app.py", "def run():\n    return 1\n")
        self.write("tests/test_app.py", (
            "from app import run\n"
            "\n"
            "\n"
            "def check():\n"
            "    assert run() == 1\n"
        ))
        report = self.audit()
        self.assertIn("tests.no_framework", self.rule_ids(report))

    def test_no_tests_means_no_framework_finding(self):
        self.write("app.py", "def run():\n    return 1\n")
        report = self.audit()
        self.assertNotIn("tests.no_framework", self.rule_ids(report))


class EmptyTestFindingTest(TempDirCase):
    """The rule surfaces through the report, not just the helper."""

    def test_docstring_then_assertion_does_not_produce_a_finding(self):
        self.write("app.py", "def run():\n    return 1\n")
        self.write("tests/test_app.py", (
            "from app import run\n"
            "\n"
            "\n"
            "def test_run():\n"
            '    """It runs."""\n'
            "    assert run() == 1\n"
        ))
        report = self.audit()
        self.assertNotIn("tests.empty_test", self.rule_ids(report))

    def test_real_empty_test_does_produce_a_finding(self):
        self.write("app.py", "def run():\n    return 1\n")
        self.write("tests/test_app.py", (
            "def test_nothing():\n"
            '    """TODO."""\n'
        ))
        report = self.audit()
        self.assertIn("tests.empty_test", self.rule_ids(report))
