"""Complexity metrics: Python exactness, heuristics, and duplication."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt.analyzers import complexity as cx  # noqa: E402
from repodebt.walker import FileInfo  # noqa: E402

from fixtures import branchy_function, nested_function, python_file  # noqa: E402


def make_file(tmp: Path, rel: str, content: str, language: str = "Python") -> FileInfo:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    from repodebt.walker import detect_language

    detected = detect_language(rel)
    return FileInfo(path=path, rel=rel, language=detected or detected_fallback(language),
                     size=len(content.encode()))


def detected_fallback(name: str):
    from repodebt.walker import LANGUAGES

    for lang in LANGUAGES:
        if lang.name == name:
            return lang
    raise AssertionError(f"unknown language {name}")


class PythonMetricsTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())

    def analyze(self, content: str, rel: str = "mod.py"):
        info = make_file(self.tmp, rel, content)
        return cx._stat_for(info)

    def test_counts_code_comment_and_blank_lines(self):
        # The module docstring counts as a comment, not as executable code.
        stat = self.analyze('"""Doc."""\n\n# a comment\nx = 1\n\n')
        self.assertEqual(stat.code_lines, 1)
        self.assertEqual(stat.comment_lines, 2)
        self.assertEqual(stat.blank_lines, 2)

    def test_docstrings_count_as_comments(self):
        stat = self.analyze('def f():\n    """Docs."""\n    return 1\n')
        self.assertEqual(stat.comment_lines, 1)
        self.assertEqual(stat.code_lines, 2)

    def test_syntax_error_is_reported_not_raised(self):
        stat = self.analyze("def broken(:\n")
        self.assertTrue(stat.parse_error)
        self.assertFalse(stat.exact)

    def test_function_length_and_location(self):
        # python_file() emits: docstring, blank, VALUE, blank, then defs.
        stat = self.analyze(python_file(funcs=1, lines_per_func=5))
        self.assertEqual(len(stat.functions), 1)
        func = stat.functions[0]
        self.assertEqual(func.name, "function_0")
        self.assertGreater(func.length, 5)
        self.assertEqual(func.lineno, 5)

    def test_complexity_matches_mccabe(self):
        # 15 `if` statements plus the implicit 1.
        stat = self.analyze(branchy_function(branches=15))
        self.assertEqual(stat.functions[0].complexity, 16)

    def test_low_complexity_function(self):
        stat = self.analyze("def f(a):\n    return a + 1\n")
        self.assertEqual(stat.functions[0].complexity, 1)

    def test_nesting_depth(self):
        stat = self.analyze(nested_function(levels=6))
        self.assertEqual(stat.functions[0].max_depth, 6)

    def test_flat_function_has_no_depth(self):
        stat = self.analyze("def f(a):\n    return a\n")
        self.assertEqual(stat.functions[0].max_depth, 0)

    def test_nested_functions_are_measured_separately(self):
        source = (
            "def outer(a):\n"
            "    def inner(b):\n"
            "        return b\n"
            "    return inner(a)\n"
        )
        stat = self.analyze(source)
        names = {f.name for f in stat.functions}
        self.assertEqual(names, {"outer", "inner"})

    def test_boolean_operators_count_as_decisions(self):
        stat = self.analyze("def f(a, b, c):\n    return a and b or c\n")
        # 1 + (a and b -> +1) + (or -> +1) = 3
        self.assertEqual(stat.functions[0].complexity, 3)

    def test_try_except_counts_handlers(self):
        stat = self.analyze(
            "def f():\n"
            "    try:\n        pass\n"
            "    except A:\n        pass\n"
            "    except B:\n        pass\n"
        )
        self.assertEqual(stat.functions[0].complexity, 3)

    def test_parameter_count(self):
        stat = self.analyze("def f(a, b, c, d, e, f, g, h):\n    return a\n")
        self.assertEqual(stat.functions[0].args, 8)

    def test_keyword_only_and_varargs_counted(self):
        stat = self.analyze("def f(a, *args, b, **kwargs):\n    return a\n")
        self.assertEqual(stat.functions[0].args, 4)

    def test_class_methods_measured(self):
        stat = self.analyze(
            "class C:\n"
            "    def method(self, a):\n"
            "        return a\n"
        )
        self.assertEqual([f.name for f in stat.functions], ["method"])


class LineStatsTest(unittest.TestCase):
    def test_python_hash_comments(self):
        from repodebt.walker import BY_EXTENSION

        lang = BY_EXTENSION[".py"]
        counts, _ = cx._line_stats("# comment\nx = 1  # trailing\n", lang)
        self.assertEqual(counts["code_lines"], 1)
        self.assertEqual(counts["comment_lines"], 1)

    def test_clike_block_comments(self):
        from repodebt.walker import BY_EXTENSION

        lang = BY_EXTENSION[".ts"]
        text = "/* header\n   more */\nconst x = 1;\n"
        counts, _ = cx._line_stats(text, lang)
        self.assertEqual(counts["code_lines"], 1)
        self.assertEqual(counts["comment_lines"], 2)

    def test_long_lines_counted(self):
        from repodebt.walker import BY_EXTENSION

        lang = BY_EXTENSION[".py"]
        counts, _ = cx._line_stats("x = " + "1" * 200 + "\n", lang)
        self.assertEqual(counts["long_lines"], 1)
        self.assertGreater(counts["max_line_length"], 120)

    def test_brace_depth_ignores_braces_in_strings(self):
        self.assertEqual(cx._brace_depth_delta('const a = "{";'), 0)
        self.assertEqual(cx._brace_depth_delta("function f() {"), 1)
        self.assertEqual(cx._brace_depth_delta("}"), -1)


class HeuristicTest(unittest.TestCase):
    def test_finds_javascript_function_extent(self):
        source = (
            "function alpha(a, b) {\n"
            "  const x = a + b;\n"
            "  return x;\n"
            "}\n"
            "\n"
            "function beta() {\n"
            "  return 1;\n"
            "}\n"
        )
        functions, _, _ = cx._analyze_heuristic(source, "JavaScript")
        by_name = {f.name: f for f in functions}
        self.assertIn("alpha", by_name)
        self.assertIn("beta", by_name)
        self.assertEqual(by_name["alpha"].length, 4)
        self.assertEqual(by_name["beta"].length, 3)
        self.assertEqual(by_name["alpha"].args, 2)

    def test_finds_ruby_method_extent(self):
        source = (
            "class Foo\n"
            "  def bar(a)\n"
            "    a + 1\n"
            "  end\n"
            "end\n"
        )
        functions, _, _ = cx._analyze_heuristic(source, "Ruby")
        self.assertIn("bar", [f.name for f in functions])

    def test_counts_branches_in_typescript(self):
        source = "function f(a) {\n  if (a) { return 1; }\n  return 0;\n}\n"
        _, complexity, _ = cx._analyze_heuristic(source, "TypeScript")
        self.assertGreaterEqual(complexity, 1)

    def test_open_function_runs_to_end_of_file(self):
        source = "function f() {\n  return 1;\n"
        functions, _, _ = cx._analyze_heuristic(source, "JavaScript")
        self.assertEqual(len(functions), 1)
        self.assertEqual(functions[0].end_lineno, 2)
        self.assertEqual(functions[0].length, 2)


class DuplicationTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())

    def test_detects_duplicate_block_across_files(self):
        block = "\n".join(
            f"value_{i} = compute_something({i}, 'constant')" for i in range(25)
        )
        a = make_file(self.tmp, "a.py", block + "\n")
        b = make_file(self.tmp, "b.py", block + "\n")
        clones, truncated = cx.find_duplication([a, b], window=10, min_lines=20,
                                                max_windows=10000)
        self.assertFalse(truncated)
        self.assertTrue(clones, "expected a duplicate block to be detected")
        self.assertEqual({clones[0].first_file, clones[0].second_file}, {"a.py", "b.py"})

    def test_distinct_files_not_flagged(self):
        a = make_file(self.tmp, "a.py", "\n".join(f"a_{i} = alpha_{i}()" for i in range(30)))
        b = make_file(self.tmp, "b.py", "\n".join(f"b_{i} = beta_{i}()" for i in range(30)))
        clones, _ = cx.find_duplication([a, b], window=10, min_lines=20, max_windows=10000)
        self.assertEqual(clones, [])

    def test_short_files_skipped(self):
        a = make_file(self.tmp, "a.py", "x = 1\ny = 2\n")
        b = make_file(self.tmp, "b.py", "x = 1\ny = 2\n")
        clones, _ = cx.find_duplication([a, b], window=10, min_lines=20, max_windows=10000)
        self.assertEqual(clones, [])

    def test_window_budget_sets_truncation_flag(self):
        a = make_file(self.tmp, "a.py", "\n".join(f"x{i} = compute_value_{i}()" for i in range(200)))
        b = make_file(self.tmp, "b.py", "\n".join(f"y{i} = other_value_{i}()" for i in range(200)))
        _clones, truncated = cx.find_duplication([a, b], window=10, min_lines=20,
                                                 max_windows=5)
        self.assertTrue(truncated)

    def test_identifiers_are_preserved(self):
        # Same shape, different names: not a clone, because renaming a whole
        # block is not what copy-paste detection should flag.
        a = make_file(self.tmp, "a.py",
                      "\n".join(f"result_{i} = compute(alpha, {i})" for i in range(25)))
        b = make_file(self.tmp, "b.py",
                      "\n".join(f"other_{i} = derive(beta, {i + 100})" for i in range(25)))
        clones, _ = cx.find_duplication([a, b], window=10, min_lines=20, max_windows=10000)
        self.assertEqual(clones, [])

    def test_literals_masked_but_identifiers_matching(self):
        # A copied block whose constants were updated: still a clone.
        a = make_file(self.tmp, "a.py",
                      "\n".join(f"total = compute(alpha, {i})" for i in range(25)))
        b = make_file(self.tmp, "b.py",
                      "\n".join(f"total = compute(alpha, {i * 7})" for i in range(25)))
        clones, _ = cx.find_duplication([a, b], window=10, min_lines=20, max_windows=10000)
        self.assertTrue(clones)


if __name__ == "__main__":
    unittest.main()
