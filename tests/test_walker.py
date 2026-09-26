"""File discovery, language detection, and exclusion."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt import config as cfg  # noqa: E402
from repodebt import walker  # noqa: E402

from fixtures import RepoCase, TempDirCase  # noqa: E402


class LanguageDetectionTest(unittest.TestCase):
    def test_by_extension(self):
        self.assertEqual(walker.detect_language("a/b.py").name, "Python")
        self.assertEqual(walker.detect_language("src/x.ts").name, "TypeScript")
        self.assertEqual(walker.detect_language("main.go").name, "Go")
        self.assertEqual(walker.detect_language("lib.rs").name, "Rust")

    def test_by_filename(self):
        self.assertEqual(walker.detect_language("Dockerfile").name, "Dockerfile")
        self.assertEqual(walker.detect_language("Makefile").name, "Make")
        self.assertEqual(walker.detect_language("go.mod").name, "TOML")
        self.assertEqual(walker.detect_language("yarn.lock").name, "Lockfile")

    def test_unknown(self):
        self.assertIsNone(walker.detect_language("data.qqq"))
        self.assertIsNone(walker.detect_language("LICENSE"))

    def test_case_insensitive_extension(self):
        self.assertEqual(walker.detect_language("SCRIPT.PY").name, "Python")


class TestDetectionTest(unittest.TestCase):
    def test_python_conventions(self):
        for rel in ("tests/test_a.py", "a/test_b.py", "test_c.py", "src/x_test.py"):
            self.assertTrue(walker.looks_like_test(rel), rel)

    def test_javascript_conventions(self):
        for rel in ("src/a.test.ts", "src/a.spec.js", "src/__tests__/a.js"):
            self.assertTrue(walker.looks_like_test(rel), rel)

    def test_source_files_are_not_tests(self):
        for rel in ("src/latest.py", "src/contest.py", "src/protest.py"):
            self.assertFalse(walker.looks_like_test(rel), rel)

    def test_go_and_java_conventions(self):
        self.assertTrue(walker.looks_like_test("pkg/handler_test.go"))
        self.assertTrue(walker.looks_like_test("src/FooTest.java"))


class DiscoveryTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.config = cfg.load(self.tmp)
        self.write("src/app.py", "x = 1\n")
        self.write("src/util.ts", "export const x = 1;\n")
        self.write("README.md", "# hi\n")
        self.write("package.json", "{}\n")
        self.write("node_modules/left-pad/index.js", "module.exports = 1\n")
        self.write("dist/bundle.js", "var a=1\n")
        self.write("static/app.min.js", "var a=1\n")
        self.write("assets/logo.png", "\x89PNG\r\n\x1a\n binary \x00 data")

    def _rels(self, result: walker.WalkResult) -> set[str]:
        return {f.rel for f in result.files}

    def test_excludes_vendor_and_generated(self):
        result = walker.discover(self.tmp, self.config, use_git=False)
        rels = self._rels(result)
        self.assertIn("src/app.py", rels)
        self.assertIn("src/util.ts", rels)
        self.assertNotIn("node_modules/left-pad/index.js", rels)
        self.assertNotIn("dist/bundle.js", rels)
        self.assertNotIn("static/app.min.js", rels)

    def test_excludes_binaries(self):
        result = walker.discover(self.tmp, self.config, use_git=False)
        self.assertNotIn("assets/logo.png", self._rels(result))
        self.assertIn(("assets/logo.png", "binary"), result.excluded)

    def test_flags_manifests(self):
        result = walker.discover(self.tmp, self.config, use_git=False)
        manifests = {f.rel for f in result.files if f.is_manifest}
        self.assertIn("package.json", manifests)

    def test_uses_manual_walk_without_git(self):
        result = walker.discover(self.tmp, self.config, use_git=False)
        self.assertEqual(result.source, "walk")

    def test_repodebt_ignore_file(self):
        self.write(".repodebtignore", "src/util.ts\n")
        config = cfg.load(self.tmp)
        result = walker.discover(self.tmp, config, use_git=False)
        self.assertNotIn("src/util.ts", self._rels(result))

    def test_include_filter_restricts(self):
        config = cfg.load(self.tmp, extra_includes=["src/*.py"])
        result = walker.discover(self.tmp, config, use_git=False)
        rels = self._rels(result)
        self.assertIn("src/app.py", rels)
        self.assertNotIn("src/util.ts", rels)

    def test_extra_exclude_flag(self):
        config = cfg.load(self.tmp, extra_excludes=["README.md"])
        result = walker.discover(self.tmp, config, use_git=False)
        self.assertNotIn("README.md", self._rels(result))

    def test_oversized_files_excluded(self):
        self.write("src/huge.py", "# padding\n" * 5000)
        config = cfg.load(self.tmp)
        config.data["max_file_bytes"] = 100
        result = walker.discover(self.tmp, config, use_git=False)
        self.assertIn(("src/huge.py", "oversized"), result.excluded)

    def test_summary_counts(self):
        result = walker.discover(self.tmp, self.config, use_git=False)
        summary = walker.summarize(result.files, result.excluded, result.truncated)
        self.assertEqual(summary["source_file_count"], 2)  # app.py + util.ts
        self.assertIn("Python", summary["languages"])


class GitDiscoveryTest(RepoCase):
    def test_uses_git_ls_files(self):
        self.init_repo()
        self.write("src/tracked.py", "x = 1\n")
        self.write("src/untracked.py", "y = 2\n")
        self.gitignore_untracked()
        self.commit("feat: add", {"src/tracked.py": "x = 1\n"})
        config = cfg.load(self.tmp)
        result = walker.discover(self.tmp, config, use_git=True)
        rels = {f.rel for f in result.files}
        self.assertEqual(result.source, "git")
        self.assertIn("src/tracked.py", rels)
        self.assertNotIn("src/untracked.py", rels)

    def gitignore_untracked(self):
        from fixtures import git

        self.write(".gitignore", "src/untracked.py\n")
        git(self.tmp, "add", ".gitignore")


if __name__ == "__main__":
    unittest.main()
