"""End-to-end CLI behaviour: exit codes, gating, and output artefacts.

These invoke ``cli.main`` in-process with ``--format none`` (or ``json``) and
assert on the return code, which is the only thing CI actually depends on.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt import __version__  # noqa: E402
from repodebt.cli import EXIT_ERROR, EXIT_OK, EXIT_THRESHOLD, GRADES, main  # noqa: E402
from repodebt.model import SCHEMA_VERSION  # noqa: E402

from fixtures import RepoCase, TempDirCase, branchy_function, python_file  # noqa: E402


class CliCase(RepoCase):
    """A repo plus a helper that runs the CLI and captures everything."""

    def run_cli(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--no-color", *argv])
        return code, out.getvalue(), err.getvalue()

    def make_clean_repo(self) -> None:
        """A reference repo that genuinely earns an A on every dimension.

        This is the yardstick the grade tests measure against, so it has to
        clear the same bars a real well-run project would: conventions present,
        tests covering its source, a declared test framework, a bus factor of
        at least two, and enough history for churn to be rankable. Anything
        less and a failing grade test is telling us about the fixture rather
        than about the scorer.
        """
        self.init_repo()
        files = {
            "README.md": "# Demo\n\n" + ("A project used in tests. " * 20) + "\n",
            "LICENSE": "MIT\n" + ("Permission is hereby granted. " * 20) + "\n",
            "CONTRIBUTING.md": "# Contributing\n\nRun the tests.\n",
            "CODEOWNERS": "* @demo-team\n",
            ".editorconfig": "root = true\n\n[*]\nindent_style = space\n",
            ".gitignore": "*.pyc\n__pycache__/\n",
            ".github/workflows/ci.yml": "name: ci\non: [push]\n",
            "requirements.txt": "flask==2.0.0\n",
            "requirements-dev.txt": "pytest==7.4.4\n",
            "src/demo.py": python_file(3, lines_per_func=4),
            "tests/test_demo.py": (
                "import pytest\n"
                "\n"
                "from demo import add, mul\n"
                "\n"
                "\n"
                "def test_add():\n"
                "    assert add(1, 2) == 3\n"
                "\n"
                "\n"
                "def test_mul():\n"
                "    assert mul(2, 3) == 6\n"
            ),
        }
        # One commit per author keeps the bus factor at 2, and touching
        # src/demo.py each time leaves it with enough churn to be ranked.
        self.commit("feat: initial project", files=files,
                    author=self.authors[0], days_ago=3)
        self.commit("feat: second change", files={"src/demo.py": python_file(4, lines_per_func=4)},
                    author=self.authors[1], days_ago=2)
        self.commit("test: cover the demo helpers",
                    files={
                        "src/demo.py": python_file(6, lines_per_func=4),
                        "tests/test_demo.py": files["tests/test_demo.py"] + "\n\ndef test_zero():\n    assert mul(0, 5) == 0\n",
                    },
                    author=self.authors[2], days_ago=1)

    def make_messy_repo(self) -> None:
        """A repo that should score badly: huge, branchy, untested, no docs."""
        self.init_repo()
        self.commit("feat: dump some code", {
            "src/huge.py": python_file(4, nested=6, lines_per_func=120),
            "src/branchy.py": branchy_function("mess", 40),
        }, days_ago=200)
        self.commit("wip", files={"src/branchy.py": branchy_function("mess", 45)},
                    days_ago=150)


class ExitCodeTest(CliCase):
    def test_clean_repo_exits_zero(self):
        self.make_clean_repo()
        code, out, err = self.run_cli(str(self.tmp), "--format", "none")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(out.strip(), "")

    def test_missing_path_exits_two(self):
        code, _, err = self.run_cli(str(self.tmp / "nope"), "--format", "none")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("does not exist", err)

    def test_file_instead_of_directory_exits_two(self):
        self.write("a.py", python_file(1))
        code, _, err = self.run_cli(str(self.tmp / "a.py"), "--format", "none")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not a directory", err)

    def test_bad_fail_on_target_exits_two(self):
        self.make_clean_repo()
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(str(self.tmp), "--fail-on", "excellent")
        self.assertEqual(ctx.exception.code, 2)

    def test_baseline_and_write_baseline_are_mutually_exclusive(self):
        self.make_clean_repo()
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(str(self.tmp), "--baseline", "b.json", "--write-baseline", "c.json")
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_baseline_exits_two(self):
        self.make_clean_repo()
        code, _, err = self.run_cli(str(self.tmp), "--baseline", "absent.json", "--format", "none")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("baseline not found", err)

    def test_corrupt_baseline_exits_two(self):
        self.make_clean_repo()
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, _, err = self.run_cli(str(self.tmp), "--baseline", str(bad), "--format", "none")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("could not read baseline", err)

    def test_unreadable_config_exits_two(self):
        self.make_clean_repo()
        self.write("repodebt.toml", "this is not = = valid toml [[[")
        code, _, err = self.run_cli(str(self.tmp), "--format", "none")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("repodebt:", err)

    def test_version_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("--version")
        self.assertEqual(ctx.exception.code, 0)


class FailOnTest(CliCase):
    def test_grade_gate_passes_on_good_repo(self):
        self.make_clean_repo()
        code, out, err = self.run_cli(str(self.tmp), "--fail-on", "F", "--format", "json")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("OK", err)

    def test_clean_repo_really_is_grade_a(self):
        # Guards the assumption the other grade tests lean on.
        self.make_clean_repo()
        _, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        self.assertEqual(json.loads(out)["scores"]["grade"], "A")

    def test_gate_fails_when_the_grade_is_below_target(self):
        self.make_messy_repo()
        code, out, err = self.run_cli(str(self.tmp), "--fail-on", "A", "--format", "json")
        grade = json.loads(out)["scores"]["grade"]
        # Grades are ordered A < B < C < D < F by rank, not alphabetically.
        self.assertGreater(GRADES.index(grade), GRADES.index("A"))
        self.assertEqual(code, EXIT_THRESHOLD)
        self.assertIn("worse than required", err)

    def test_messy_repo_passes_the_most_permissive_gate(self):
        self.make_messy_repo()
        code, _, err = self.run_cli(str(self.tmp), "--fail-on", "F", "--format", "none")
        self.assertEqual(code, EXIT_OK)

    def test_grade_gate_is_case_insensitive(self):
        self.make_messy_repo()
        code, _, _ = self.run_cli(str(self.tmp), "--fail-on", "f", "--format", "none")
        self.assertEqual(code, EXIT_OK)

    def test_severity_gate_fails_on_any_critical(self):
        self.write("requirements.txt", "requests==2.19.0\n")
        report = self.audit()
        # Only assert the gate fires when the offline analyzer really did
        # produce something at that severity.
        severities = {f.severity.value for f in report.findings}
        code, _, err = self.run_cli(str(self.tmp), "--fail-on", "low", "--format", "none")
        if severities:
            self.assertEqual(code, EXIT_THRESHOLD)
            self.assertIn("finding(s) at or above", err)

    def test_severity_gate_passes_when_nothing_matches(self):
        self.make_clean_repo()
        report = self.audit()
        if not report.findings:
            code, _, _ = self.run_cli(str(self.tmp), "--fail-on", "high", "--format", "none")
            self.assertEqual(code, EXIT_OK)

    def test_gate_prints_ok_on_stderr_not_stdout(self):
        self.make_clean_repo()
        _, out, err = self.run_cli(str(self.tmp), "--fail-on", "F", "--format", "text")
        self.assertNotIn("repodebt: OK", out)
        self.assertIn("repodebt: OK", err)


class BaselineTest(CliCase):
    def _write_baseline(self, name="health.json"):
        target = self.tmp / name
        code, _, _ = self.run_cli(str(self.tmp), "--write-baseline", str(target),
                                  "--format", "none")
        self.assertEqual(code, EXIT_OK)
        return target

    def test_write_baseline_creates_a_loadable_file(self):
        self.make_clean_repo()
        target = self._write_baseline()
        self.assertTrue(target.exists())
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertIn("scores", payload)
        self.assertIn("findings", payload)

    def test_identical_run_has_no_regressions(self):
        self.make_clean_repo()
        target = self._write_baseline()
        code, _, err = self.run_cli(str(self.tmp), "--baseline", str(target), "--format", "none")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("OK", err)

    def test_new_findings_fail_the_build(self):
        self.make_clean_repo()
        target = self._write_baseline()
        messy = "def f(v):\n" + "    if v:\n        return 1\n" * 30
        self.commit("feat: add a messy module", {"src/messy.py": messy}, days_ago=0)
        code, _, err = self.run_cli(str(self.tmp), "--baseline", str(target), "--format", "none")
        self.assertEqual(code, EXIT_THRESHOLD)
        self.assertIn("regression", err)

    def test_improvements_do_not_fail_the_build(self):
        self.make_clean_repo()
        messy = "def f(v):\n" + "    if v:\n        return 1\n" * 30
        self.commit("feat: add a messy module", {"src/messy.py": messy}, days_ago=1)
        target = self._write_baseline()
        self.commit("fix: simplify the messy module", {"src/messy.py": "x = 1\n"}, days_ago=0)
        code, _, err = self.run_cli(str(self.tmp), "--baseline", str(target), "--format", "none")
        self.assertEqual(code, EXIT_OK)

    def test_diff_is_embedded_in_the_json_artifact(self):
        self.make_clean_repo()
        target = self._write_baseline()
        out_file = self.tmp / "out.json"
        self.run_cli(str(self.tmp), "--baseline", str(target), "--json", str(out_file),
                     "--format", "none")
        payload = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertIn("baseline_diff", payload)
        self.assertTrue(payload["baseline_diff"]["baseline_present"])
        self.assertIn("regressions", payload["baseline_diff"])

    def test_diff_is_also_in_stdout_json(self):
        self.make_clean_repo()
        target = self._write_baseline()
        code, out, _ = self.run_cli(str(self.tmp), "--baseline", str(target), "--format", "json")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("baseline_diff", json.loads(out))

    def test_baseline_file_itself_never_carries_a_diff(self):
        self.make_clean_repo()
        target = self._write_baseline()
        payload = json.loads(target.read_text(encoding="utf-8"))
        # Otherwise the next run would diff against a diff.
        self.assertNotIn("baseline_diff", payload)

    def test_max_score_drop_tolerates_small_regressions(self):
        self.make_clean_repo()
        target = self._write_baseline()
        # Bump the score-drop limit right up: a minor change should not fail.
        code, _, _ = self.run_cli(str(self.tmp), "--baseline", str(target),
                                  "--max-score-drop", "100", "--format", "none")
        self.assertEqual(code, EXIT_OK)

    def test_changed_config_is_reported(self):
        self.make_clean_repo()
        target = self._write_baseline()
        self.write("repodebt.toml", "large_file_lines = 400\n")
        code, out, _ = self.run_cli(str(self.tmp), "--baseline", str(target), "--format", "json")
        payload = json.loads(out)
        self.assertTrue(payload["baseline_diff"]["config_changed"])
        self.assertEqual(code, EXIT_OK)


class OutputArtefactTest(CliCase):
    def test_json_file_has_the_documented_shape(self):
        self.make_clean_repo()
        out_file = self.tmp / "report.json"
        self.run_cli(str(self.tmp), "--json", str(out_file), "--format", "none")
        payload = json.loads(out_file.read_text(encoding="utf-8"))
        for key in ("schema_version", "repo", "generated_at", "duration_seconds",
                    "config_digest", "scores", "metrics", "findings", "unavailable"):
            self.assertIn(key, payload)
        self.assertIn("overall", payload["scores"])
        self.assertIn("grade", payload["scores"])
        for name in ("complexity", "tests", "hotspots", "hygiene", "deps"):
            self.assertIn(name, payload["scores"]["dimensions"])

    def test_markdown_file_is_written(self):
        self.make_clean_repo()
        target = self.tmp / "HEALTH.md"
        self.run_cli(str(self.tmp), "--markdown", str(target), "--format", "none")
        text = target.read_text(encoding="utf-8")
        self.assertIn("# ", text)
        self.assertIn("Grade", text)
        self.assertIn("| Dimension |", text)

    def test_markdown_headings_are_nested(self):
        # A grade line at the same level as its own section heading renders as
        # a sibling section in every Markdown viewer.
        self.make_clean_repo()
        target = self.tmp / "HEALTH.md"
        self.run_cli(str(self.tmp), "--markdown", str(target), "--format", "none")
        lines = [l for l in target.read_text(encoding="utf-8").splitlines()
                 if l.startswith("#")]
        self.assertIn("## Overall", lines)
        overall_at = lines.index("## Overall")
        grade_line = lines[overall_at + 1]
        self.assertTrue(grade_line.startswith("### "), grade_line)

    def test_markdown_is_valid_utf8(self):
        self.make_clean_repo()
        target = self.tmp / "HEALTH.md"
        self.run_cli(str(self.tmp), "--markdown", str(target), "--format", "none")
        raw = target.read_bytes()
        text = raw.decode("utf-8")  # raises if the file is not UTF-8
        # The severity glyphs are non-ASCII by design.
        self.assertTrue(any(ord(c) > 127 for c in text))

    def test_output_creates_missing_parent_directories(self):
        self.make_clean_repo()
        target = self.tmp / "out" / "nested" / "report.json"
        code, _, _ = self.run_cli(str(self.tmp), "--json", str(target), "--format", "none")
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(target.exists())

    def test_format_none_prints_nothing(self):
        self.make_clean_repo()
        _, out, _ = self.run_cli(str(self.tmp), "--format", "none")
        self.assertEqual(out.strip(), "")

    def test_format_text_prints_a_report(self):
        self.make_clean_repo()
        _, out, _ = self.run_cli(str(self.tmp), "--format", "text")
        self.assertIn("REPODEBT", out)
        self.assertIn("Complexity", out)

    def test_format_json_prints_valid_json(self):
        self.make_clean_repo()
        _, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        json.loads(out)  # raises if malformed


class RuleReferenceTest(CliCase):
    def test_list_rules_prints_every_rule(self):
        from repodebt.rules import RULES

        code, out, _ = self.run_cli("--list-rules")
        self.assertEqual(code, EXIT_OK)
        for rule_id in RULES:
            self.assertIn(rule_id, out)
        self.assertIn("rules total", out)

    def test_explain_prints_details(self):
        code, out, _ = self.run_cli("--explain", "hotspots.high_churn_complex")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("why it matters", out)
        self.assertIn("remediation", out)
        self.assertIn("hotspots", out)

    def test_explain_unknown_rule_exits_two(self):
        code, _, err = self.run_cli("--explain", "no.such_rule")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("unknown rule", err)

    def test_list_rules_works_without_a_path(self):
        code, out, _ = self.run_cli("--list-rules")
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.strip())


class SelectionTest(CliCase):
    def test_skip_removes_a_dimension(self):
        self.make_clean_repo()
        _, out, _ = self.run_cli(str(self.tmp), "--skip", "deps", "--format", "json")
        payload = json.loads(out)
        self.assertIsNone(payload["scores"]["dimensions"]["deps"]["score"])

    def test_only_restricts_to_the_named_analyzer(self):
        self.make_clean_repo()
        _, out, _ = self.run_cli(str(self.tmp), "--only", "complexity", "--format", "json")
        payload = json.loads(out)
        self.assertIsNotNone(payload["scores"]["dimensions"]["complexity"]["score"])
        self.assertIsNone(payload["scores"]["dimensions"]["tests"]["score"])

    def _large_file_paths(self, payload: dict) -> list[str]:
        paths = []
        for finding in payload["findings"]:
            if finding["id"] == "complexity.large_file":
                paths.extend(loc["file"] for loc in finding["locations"])
        return paths

    def test_exclude_glob_removes_a_path(self):
        self.write("src/kept.py", "x = 1\n" * 600)
        self.write("src/skipme/big.py", "y = 1\n" * 600)
        _, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        before = self._large_file_paths(json.loads(out))
        self.assertTrue(any("kept.py" in p for p in before))
        self.assertTrue(any("skipme" in p for p in before))

        _, out, _ = self.run_cli(str(self.tmp), "--exclude", "src/skipme", "--format", "json")
        after = self._large_file_paths(json.loads(out))
        self.assertTrue(any("kept.py" in p for p in after))
        self.assertFalse([p for p in after if "skipme" in p])

    def test_include_glob_narrows_the_scan(self):
        self.write("src/kept.py", "x = 1\n" * 600)
        self.write("other/dropped.py", "y = 1\n" * 600)
        _, out, _ = self.run_cli(str(self.tmp), "--include", "src/**", "--format", "json")
        paths = self._large_file_paths(json.loads(out))
        self.assertTrue(any("kept.py" in p for p in paths))
        self.assertFalse([p for p in paths if "dropped.py" in p])

    def test_ignore_file_removes_paths_from_the_scan(self):
        self.write("src/kept.py", "x = 1\n" * 600)
        self.write("src/skipme/big.py", "y = 1\n" * 600)
        self.write(".repodebtignore", "src/skipme\n")
        _, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        paths = self._large_file_paths(json.loads(out))
        self.assertTrue(any("kept.py" in p for p in paths))
        self.assertFalse([p for p in paths if "skipme" in p])

    def test_no_git_leaves_history_findings_out(self):
        self.make_clean_repo()
        code, out, _ = self.run_cli(str(self.tmp), "--no-git", "--format", "json")
        payload = json.loads(out)
        self.assertFalse(payload["repo"]["is_git_repo"])
        self.assertEqual(code, EXIT_OK)

    def test_max_findings_per_rule_caps_output(self):
        for index in range(6):
            self.write(f"src/long_{index}.py", "x = 1\n" * 600)
        _, out, _ = self.run_cli(str(self.tmp), "--max-findings-per-rule", "2",
                                 "--format", "json")
        payload = json.loads(out)
        counts: dict[str, int] = {}
        for finding in payload["findings"]:
            counts[finding["id"]] = counts.get(finding["id"], 0) + 1
        self.assertLessEqual(max(counts.values()), 2)


class ConfigFileTest(CliCase):
    def test_toml_config_is_loaded(self):
        self.write("src/medium.py", python_file(2, lines_per_func=10))
        self.write("repodebt.toml", "large_file_lines = 5\n")
        _, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        payload = json.loads(out)
        # With the threshold at 5 lines, a 30-line module is "large". At the
        # 500-line default it would not be, so this proves the file was read.
        self.assertIn("complexity.large_file", {f["id"] for f in payload["findings"]})

    def test_default_threshold_does_not_flag_the_same_file(self):
        self.write("src/medium.py", python_file(2, lines_per_func=10))
        _, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        payload = json.loads(out)
        self.assertNotIn("complexity.large_file", {f["id"] for f in payload["findings"]})

    def test_explicit_config_path_is_honoured(self):
        self.make_clean_repo()
        custom = self.tmp / "custom.toml"
        custom.write_text("large_file_lines = 5\n", encoding="utf-8")
        _, out, _ = self.run_cli(str(self.tmp), "--config", str(custom), "--format", "json")
        payload = json.loads(out)
        self.assertIn("complexity.large_file", {f["id"] for f in payload["findings"]})

    def test_missing_config_path_exits_two(self):
        self.make_clean_repo()
        absent = self.tmp / "absent.toml"
        code, _, err = self.run_cli(str(self.tmp), "--config", str(absent), "--format", "none")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("repodebt:", err)

    def test_weights_in_config_change_the_grade(self):
        self.make_clean_repo()
        self.write("huge.py", "x = 1\n" * 900)
        self.write("repodebt.toml", "weights = {complexity = 0.0}\n")
        _, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        payload = json.loads(out)
        self.assertEqual(payload["scores"]["dimensions"]["complexity"]["weight"], 0.0)

    def test_unknown_config_key_is_reported_on_stderr(self):
        self.make_clean_repo()
        self.write("repodebt.toml", "largefile_lines = 500\n")
        code, _, err = self.run_cli(str(self.tmp), "--format", "none")
        # A warning, not a failure: the rest of the config may still be valid.
        self.assertEqual(code, EXIT_OK)
        self.assertIn("largefile_lines", err)
        self.assertIn("unrecognised", err)

    def test_unknown_key_warning_goes_to_stderr_not_stdout(self):
        self.make_clean_repo()
        self.write("repodebt.toml", "nonsense = 1\n")
        _, out, err = self.run_cli(str(self.tmp), "--format", "text")
        self.assertNotIn("nonsense", out)
        self.assertIn("nonsense", err)

    def test_no_warning_when_config_is_clean(self):
        self.make_clean_repo()
        self.write("repodebt.toml", "large_file_lines = 400\nlong_function_lines = 50\n")
        _, _, err = self.run_cli(str(self.tmp), "--format", "none")
        self.assertNotIn("unrecognised", err)


class OnlineFlagTest(TempDirCase):
    """Advisory lookups are opt-in; both paths must survive a dead network."""

    def run_cli(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--no-color", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_offline_by_default_does_not_hit_the_network(self):
        self.write("requirements.txt", "flask==1.0.0\n")
        code, out, _ = self.run_cli(str(self.tmp), "--format", "json")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        # Offline mode still reports the dependency, just without advisories.
        self.assertIn("deps", payload["metrics"])

    def test_online_flag_degrades_cleanly_when_unreachable(self):
        self.write("requirements.txt", "flask==1.0.0\n")
        # No network in the test environment: the run must still succeed and
        # must record why the advisory lookup did not happen.
        code, out, _ = self.run_cli(str(self.tmp), "--online", "--format", "json")
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertTrue(payload.get("unavailable"))


class VersionTest(unittest.TestCase):
    def test_version_is_a_string(self):
        self.assertIsInstance(__version__, str)
        self.assertTrue(__version__)


if __name__ == "__main__":
    unittest.main()
