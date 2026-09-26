"""Config loading, defaults, and glob matching."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt import config as cfg  # noqa: E402
from repodebt.model import Dimension  # noqa: E402

from fixtures import TempDirCase  # noqa: E402


class DefaultsTest(unittest.TestCase):
    def test_weights_sum_to_one(self):
        total = sum(cfg.DEFAULTS["weights"].values())
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_every_dimension_has_a_weight_and_budget(self):
        for dimension in Dimension:
            self.assertIn(dimension.value, cfg.DEFAULTS["weights"])
            self.assertIn(dimension.value, cfg.DEFAULTS["budget"])

    def test_grade_bands_are_descending(self):
        bands = cfg.DEFAULTS["grade_bands"]
        thresholds = [b[0] for b in bands]
        self.assertEqual(thresholds, sorted(thresholds, reverse=True))


class LoadTest(TempDirCase):
    def test_defaults_when_no_config(self):
        config = cfg.load(self.tmp)
        self.assertIsNone(config.source_path)
        self.assertEqual(config.get("large_file_lines"), 500)

    def test_repodebt_toml_overrides(self):
        self.write("repodebt.toml", 'large_file_lines = 42\nweights = { tests = 0.9 }\n')
        config = cfg.load(self.tmp)
        self.assertEqual(config.get("large_file_lines"), 42)
        self.assertEqual(config.weight(Dimension.TESTS), 0.9)
        # Unspecified weights keep their defaults.
        self.assertEqual(config.weight(Dimension.COMPLEXITY), 0.25)

    def test_pyproject_tool_table_is_read(self):
        self.write("pyproject.toml", "[project]\nname = 'x'\n\n[tool.repodebt]\nlarge_file_lines = 7\n")
        config = cfg.load(self.tmp)
        self.assertEqual(config.source_path.name, "pyproject.toml")
        self.assertEqual(config.get("large_file_lines"), 7)

    def test_explicit_config_path(self):
        self.write("custom.toml", "large_file_lines = 11\n")
        config = cfg.load(self.tmp, config_path=Path("custom.toml"))
        self.assertEqual(config.get("large_file_lines"), 11)

    def test_missing_config_path_raises(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.load(self.tmp, config_path=Path("nope.toml"))

    def test_invalid_toml_raises(self):
        self.write("repodebt.toml", "this is not = = toml\n")
        with self.assertRaises(cfg.ConfigError):
            cfg.load(self.tmp)

    def test_digest_is_stable_and_sensitive(self):
        first = cfg.load(self.tmp)
        self.assertEqual(first.digest(), cfg.load(self.tmp).digest())
        self.write("repodebt.toml", "large_file_lines = 123\n")
        self.assertNotEqual(first.digest(), cfg.load(self.tmp).digest())

    def test_extra_excludes_are_appended(self):
        config = cfg.load(self.tmp, extra_excludes=["fixtures/"])
        self.assertIn("fixtures/", config.excludes())


class UnknownKeyTest(TempDirCase):
    """A config that parses but applies nothing must not be silent."""

    def test_typo_is_reported(self):
        self.write("repodebt.toml", "largefile_lines = 500\n")
        self.assertEqual(cfg.load(self.tmp).unknown_keys, ["largefile_lines"])

    def test_valid_config_reports_nothing(self):
        self.write("repodebt.toml", "large_file_lines = 500\n")
        self.assertEqual(cfg.load(self.tmp).unknown_keys, [])

    def test_weights_keys_are_checked(self):
        self.write("repodebt.toml", '[weights]\ncomplexity = 0.5\ncomplexty = 0.1\n')
        self.assertEqual(cfg.load(self.tmp).unknown_keys, ["weights.complexty"])

    def test_budget_keys_are_checked(self):
        self.write("repodebt.toml", '[budget]\ndepz = 10\n')
        self.assertEqual(cfg.load(self.tmp).unknown_keys, ["budget.depz"])

    def test_multiple_unknown_keys_are_all_reported(self):
        self.write("repodebt.toml", "nonsense = 1\nalso_wrong = 2\nlarge_file_lines = 5\n")
        self.assertEqual(cfg.load(self.tmp).unknown_keys, ["also_wrong", "nonsense"])

    def test_pyproject_table_is_validated(self):
        self.write("pyproject.toml", "[tool.repodebt]\nbogus = 1\n")
        self.assertEqual(cfg.load(self.tmp).unknown_keys, ["bogus"])

    def test_grade_bands_is_not_flagged(self):
        self.write("repodebt.toml", 'grade_bands = [[90.0, "A"], [80.0, "B"]]\n')
        self.assertEqual(cfg.load(self.tmp).unknown_keys, [])

    def test_unknown_key_does_not_stop_the_run(self):
        self.write("repodebt.toml", "largefile_lines = 500\nlarge_file_lines = 42\n")
        config = cfg.load(self.tmp)
        self.assertEqual(config.unknown_keys, ["largefile_lines"])
        # The valid key alongside it still takes effect.
        self.assertEqual(config.get("large_file_lines"), 42)

    def test_a_key_stray_inside_a_table_is_caught(self):
        # The most common way to write a config that silently does nothing:
        # TOML scoping pulls a top-level key into the preceding table. It
        # parses fine, but "weights.large_file_lines" is not a thing.
        self.write("repodebt.toml", "[weights]\nlarge_file_lines = 700\n")
        config = cfg.load(self.tmp)
        self.assertEqual(config.unknown_keys, ["weights.large_file_lines"])
        self.assertNotEqual(config.get("large_file_lines"), 700)

    def test_cache_dir_is_a_recognised_key(self):
        # Documented in the README, so rejecting it here would be a bug.
        self.write("repodebt.toml", "cache_dir = 'C:/ci/rd-cache'\n")
        config = cfg.load(self.tmp)
        self.assertEqual(config.unknown_keys, [])
        self.assertEqual(config.cache_dir(), Path("C:/ci/rd-cache"))


class CacheDirTest(TempDirCase):
    def test_default_is_under_home(self):
        self.assertEqual(cfg.load(self.tmp).cache_dir(),
                         Path.home() / ".cache" / "repodebt")

    def test_configured_path_is_honoured_and_created(self):
        target = self.tmp / "nested" / "cache"
        self.write("repodebt.toml", f"cache_dir = '{target.as_posix()}'\n")
        cache = cfg.load(self.tmp).cache_dir()
        self.assertEqual(cache, target)
        self.assertTrue(cache.is_dir())

    def test_unwritable_location_does_not_raise(self):
        # The cache is an optimisation; a read-only home must not fail a run.
        data = dict(cfg.DEFAULTS)
        data["cache_dir"] = "NUL\\nonexistent\\cache"
        config = cfg.Config(root=self.tmp, data=data)
        self.assertIsInstance(config.cache_dir(), Path)


class IgnoreFileTest(TempDirCase):
    """.repodebtignore is a plain newline-separated glob list."""

    def test_absent_file_changes_nothing(self):
        self.assertNotIn("somewhere/", cfg.load(self.tmp).excludes())

    def test_globs_are_read(self):
        self.write(".repodebtignore", "vendored/\n*.generated.py\n")
        excludes = cfg.load(self.tmp).excludes()
        self.assertIn("vendored/", excludes)
        self.assertIn("*.generated.py", excludes)

    def test_comments_and_blanks_are_skipped(self):
        self.write(".repodebtignore", "# a comment\n\n   \nreal/\n")
        excludes = cfg.load(self.tmp).excludes()
        self.assertIn("real/", excludes)
        self.assertNotIn("# a comment", excludes)
        self.assertNotIn("", excludes)

    def test_ignore_file_is_additive_to_the_config(self):
        self.write("repodebt.toml", 'exclude = ["from_config"]\n')
        self.write(".repodebtignore", "from_ignore\n")
        excludes = cfg.load(self.tmp).excludes()
        self.assertIn("from_config", excludes)
        self.assertIn("from_ignore", excludes)

    def test_ignore_file_cannot_remove_a_config_exclude(self):
        self.write("repodebt.toml", 'exclude = ["keep_me"]\n')
        self.write(".repodebtignore", "something_else\n")
        self.assertIn("keep_me", cfg.load(self.tmp).excludes())

    def test_duplicate_globs_are_collapsed(self):
        self.write("repodebt.toml", 'exclude = ["dup"]\n')
        self.write(".repodebtignore", "dup\nother\n")
        excludes = cfg.load(self.tmp).excludes()
        self.assertEqual(excludes.count("dup"), 1)

    def test_ignore_file_changes_the_digest(self):
        before = cfg.load(self.tmp).digest()
        self.write(".repodebtignore", "newly_ignored\n")
        self.assertNotEqual(before, cfg.load(self.tmp).digest())

    def test_cli_flag_still_lands_in_the_exclude_list(self):
        # The flag is a different mechanism from the ignore list, so the two
        # must compose rather than one replacing the other.
        self.write(".repodebtignore", "from_ignore\n")
        config = cfg.load(self.tmp, extra_excludes=["from_flag"])
        excludes = config.excludes()
        self.assertIn("from_ignore", excludes)
        self.assertIn("from_flag", excludes)


class GlobTest(unittest.TestCase):
    def test_simple_name(self):
        patterns = cfg.compile_patterns(["node_modules"])
        self.assertTrue(cfg.match_any("node_modules/react/index.js", patterns, is_dir=True))
        self.assertFalse(cfg.match_any("src/app.js", patterns))

    def test_nested_pattern(self):
        patterns = cfg.compile_patterns(["src/generated"])
        self.assertTrue(cfg.match_any("src/generated/x.py", patterns, is_dir=True))
        self.assertFalse(cfg.match_any("lib/generated/x.py", patterns))

    def test_wildcard_extension(self):
        patterns = cfg.compile_patterns(["*.min.js"])
        self.assertTrue(cfg.match_any("static/app.min.js", patterns))

    def test_double_star_prefix(self):
        patterns = cfg.compile_patterns(["**/dist"])
        self.assertTrue(cfg.match_any("packages/a/dist", patterns, is_dir=True))
        self.assertTrue(cfg.match_any("dist", patterns, is_dir=True))

    def test_dir_only_pattern_matches_contents(self):
        patterns = cfg.compile_patterns(["build/"])
        self.assertTrue(cfg.match_any("build/out.js", patterns))
        self.assertFalse(cfg.match_any("build", patterns, is_dir=False))

    def test_question_mark(self):
        patterns = cfg.compile_patterns(["a?c.py"])
        self.assertTrue(cfg.match_any("abc.py", patterns))
        self.assertFalse(cfg.match_any("abbc.py", patterns))

    def test_character_class(self):
        patterns = cfg.compile_patterns(["v[0-9].txt"])
        self.assertTrue(cfg.match_any("v1.txt", patterns))
        self.assertFalse(cfg.match_any("vx.txt", patterns))

    def test_comments_and_blanks_ignored(self):
        patterns = cfg.compile_patterns(["# a comment", "", "   "])
        self.assertEqual(patterns, [])


if __name__ == "__main__":
    unittest.main()
