"""Complexity, size, and duplication analysis.

Python is measured exactly via ``ast``. Every other language falls back to a
tokenizer heuristic, and those findings are emitted with ``HEURISTIC``
confidence so the scorer discounts them instead of pretending they are exact.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..model import Confidence, Dimension, Location
from ..walker import FileInfo
from .base import Analyzer, Context, Result, make_finding, pct

# --------------------------------------------------------------------------
# Python: exact metrics via ast
# --------------------------------------------------------------------------

_CONTROL_FLOW = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
                 ast.Try, ast.ExceptHandler, ast.Match)


@dataclass
class FuncMetric:
    name: str
    lineno: int
    end_lineno: int
    length: int
    args: int
    complexity: int
    max_depth: int

    @property
    def qualified(self) -> str:
        return self.name


_NESTED_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _complexity_of(node: ast.AST) -> int:
    """McCabe cyclomatic complexity: 1 + number of decision points.

    ``with`` and ``assert`` are not decisions, so they are excluded. ``elif``
    is a nested ``If`` in the AST and therefore counts, as it should.
    """
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp)):
            score += 1
        elif isinstance(child, ast.ExceptHandler):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
        elif isinstance(child, ast.Match):
            score += max(0, len(child.cases) - 1)
    return score


def _control_depth(node: ast.AST, depth: int = 0) -> int:
    """Deepest chain of nested control-flow statements.

    Nested functions, lambdas, and classes are their own scopes and are
    measured separately, so this does not descend into them.
    """
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPE):
            continue
        child_depth = depth + 1 if isinstance(child, _CONTROL_FLOW) else depth
        best = max(best, _control_depth(child, child_depth))
    return best


def _count_args(node) -> int:
    args = node.args
    total = len(getattr(args, "posonlyargs", []) or []) + len(args.args)
    if args.vararg:
        total += 1
    total += len(args.kwonlyargs)
    if args.kwarg:
        total += 1
    return total


def _collect_functions(node, class_depth: int = 0) -> list[FuncMetric]:
    """Metrics for ``node`` plus every function nested inside it.

    Nested functions are hoisted into the same flat list so each is measured
    and reported on its own rather than hidden inside its parent's numbers.
    """
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    out = [FuncMetric(
        name=node.name,
        lineno=node.lineno,
        end_lineno=end,
        length=end - node.lineno + 1,
        args=_count_args(node),
        complexity=_complexity_of(node),
        max_depth=_control_depth(node) + class_depth,
    )]
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_collect_functions(child, class_depth))
        elif isinstance(child, ast.ClassDef):
            out.extend(_collect_class(child))
    return out


def _collect_class(node, depth: int = 0) -> list[FuncMetric]:
    out: list[FuncMetric] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_collect_functions(child, depth + 1))
        elif isinstance(child, ast.ClassDef):
            out.extend(_collect_class(child, depth + 1))
    return out


def _module_functions(tree: ast.Module) -> list[FuncMetric]:
    out: list[FuncMetric] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_collect_functions(node))
        elif isinstance(node, ast.ClassDef):
            out.extend(_collect_class(node))
    return out


def _module_depth(tree: ast.Module) -> int:
    return max((_control_depth(node) for node in tree.body), default=0)


# --------------------------------------------------------------------------
# Heuristic metrics for non-Python languages
# --------------------------------------------------------------------------

#: Regex fragments that each contribute one unit of branch complexity.
_BRANCH_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "braces": (
        re.compile(r"\b(if|else\s+if|elif)\b"),
        re.compile(r"\b(for|while|foreach|do)\b"),
        re.compile(r"\b(case|catch|except|rescue|match\s+.*\s*=>)\b"),
        re.compile(r"&&|\|\|"),
        re.compile(r"\?\s*[^:]{1,80}:"),
        re.compile(r"\bwhen\b"),
    ),
    "hash": (
        re.compile(r"\b(if|elsif|unless|when|case|rescue|catch)\b"),
        re.compile(r"\b(while|until|loop|for)\b"),
        re.compile(r"\b(and|or|not)\b"),
        re.compile(r"&{1,2}|\|{1,2}"),
    ),
    "pythonish": (
        re.compile(r"\b(if|elif|else)\b"),
        re.compile(r"\b(for|while)\b"),
        re.compile(r"\b(except|finally)\b"),
        re.compile(r"\band\b|\bor\b"),
    ),
}

_FUNC_START = re.compile(
    r"^\s*(?:(?:export|public|private|protected|static|final|async|inline|extern)\s+)*"
    r"(?:[\w<>\[\]:,*&.]+\s+)?"
    r"(function|func|def|fn|sub|method|class|interface|impl|struct|type|enum|trait)\b"
    r"[\s\*&:]*([A-Za-z_$][\w$]*)"
)
_ANON_FUNC = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|async|export)\s+)*[\w<>\[\]]+\s+"
    r"([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:const\s*)?\{"
)


def _branches_for(language_name: str) -> tuple[re.Pattern[str], ...]:
    if language_name in ("Go", "Rust", "C", "C++", "Java", "C#", "Kotlin", "Scala",
                         "Swift", "Zig", "Dart", "PHP"):
        return _BRANCH_PATTERNS["braces"]
    if language_name in ("Ruby", "Shell", "Elixir", "PowerShell", "R", "SQL"):
        return _BRANCH_PATTERNS["hash"]
    return _BRANCH_PATTERNS["pythonish"]


def _strip_comments_and_strings(text: str, line_comments: tuple[str, ...],
                                block_comments: tuple[tuple[str, str], ...],
                                doc_delims: tuple[str, ...]) -> list[str]:
    """Return lines with comments and string bodies blanked out."""
    out: list[str] = []
    in_block: str | None = None
    for raw in text.splitlines():
        line = raw
        if in_block:
            end = line.find(in_block)
            if end == -1:
                out.append("")
                continue
            line = line[end + len(in_block):]
            in_block = None
        for opener, closer in block_comments:
            while True:
                start = line.find(opener)
                if start == -1:
                    break
                end = line.find(closer, start + len(opener))
                if end == -1:
                    line = line[:start]
                    in_block = closer
                    break
                line = line[:start] + " " + line[end + len(closer):]
        for prefix in line_comments:
            index = line.find(prefix)
            if index != -1:
                line = line[:index]
        for delim in doc_delims:
            line = _blank_quoted(line, delim)
        out.append(line)
    return out


def _blank_quoted(line: str, delim: str) -> str:
    out = []
    index = 0
    while index < len(line):
        if line.startswith(delim, index):
            end = line.find(delim, index + len(delim))
            if end == -1:
                # Unterminated: blank the remainder of the line.
                out.append(" " * (len(line) - index))
                break
            out.append(" " * (end + len(delim) - index))
            index = end + len(delim)
            continue
        out.append(line[index])
        index += 1
    return "".join(out)


def _brace_depth_delta(line: str) -> int:
    depth = 0
    in_string = False
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                in_string = False
        elif char in "\"'`":
            in_string = True
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return depth


def _indent_delta(line: str) -> int:
    if not line.strip():
        return 0
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "*", "--")):
        return 0
    return len(line) - len(stripped)


# --------------------------------------------------------------------------
# Line statistics
# --------------------------------------------------------------------------

@dataclass
class FileStats:
    rel: str
    language: str
    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    long_lines: int = 0
    max_line_length: int = 0
    functions: list[FuncMetric] = field(default_factory=list)
    max_depth: int = 0
    complexity: int = 0
    parse_error: str = ""
    is_test: bool = False

    @property
    def exact(self) -> bool:
        return self.language == "Python" and not self.parse_error


def _line_stats(text: str, language) -> tuple[dict[str, int], list[str]]:
    raw_lines = text.splitlines()
    stripped = _strip_comments_and_strings(
        text, language.line_comments, language.block_comments, language.doc_string_delims
    )
    total = len(raw_lines)
    code = comment = blank = long_lines = 0
    max_len = 0
    for original, clean in zip(raw_lines, stripped):
        stripped_original = original.strip()
        if len(original) > max_len:
            max_len = len(original)
        if len(original) > 120:
            long_lines += 1
        if not stripped_original:
            blank += 1
            continue
        if not clean.strip():
            comment += 1
            continue
        code += 1
    return (
        {
            "total_lines": total,
            "code_lines": code,
            "comment_lines": comment,
            "blank_lines": blank,
            "long_lines": long_lines,
            "max_line_length": max_len,
        },
        stripped,
    )


def _analyze_python(text: str) -> tuple[list[FuncMetric], int, int, str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [], 0, 0, f"{exc.msg} (line {exc.lineno})"
    except (ValueError, RecursionError) as exc:  # e.g. null bytes
        return [], 0, 0, str(exc)
    functions = _module_functions(tree)
    total_complexity = sum(f.complexity - 1 for f in functions)
    max_depth = max(
        [_module_depth(tree)] + [f.max_depth for f in functions],
        default=0,
    )
    return functions, total_complexity, max_depth, ""


def _analyze_heuristic(text: str, language_name: str) -> tuple[list[FuncMetric], int, int]:
    """Approximate function metrics without a real parser.

    Function extents are recovered by tracking brace depth (C-family) or
    indentation (hash-family) from the declaration line until the body ends.
    """
    lines = text.splitlines()
    branch_patterns = _branches_for(language_name)
    brace_languages = language_name in (
        "TypeScript", "JavaScript", "Go", "Rust", "Java", "C", "C++", "C#", "Kotlin",
        "Scala", "Swift", "Dart", "Zig", "PHP", "Vue", "Svelte",
    )
    functions: list[FuncMetric] = []
    depth = 0
    max_depth = 0
    complexity = 0
    # Open functions, innermost last, with the depth/indent at declaration.
    open_funcs: list[FuncMetric] = []
    open_meta: list[tuple[int, int]] = []

    for index, line in enumerate(lines, start=1):
        indent = len(line) - len(line.lstrip()) if line.strip() else None

        # Closing runs on every line, including blanks, so that a body ending
        # on a line followed by a blank is attributed the correct extent.
        if brace_languages:
            # A function declared at depth D ends once depth falls back below D.
            while open_meta and depth < open_meta[-1][0]:
                metric = open_funcs.pop()
                open_meta.pop()
                metric.end_lineno = max(metric.lineno, index - 1)
                metric.length = metric.end_lineno - metric.lineno + 1
                functions.append(metric)
            depth += _brace_depth_delta(line)
            max_depth = max(max_depth, depth)
        elif indent is not None:
            # A function declared at indent I ends at the next line at indent <= I.
            while open_meta and indent <= open_meta[-1][1]:
                metric = open_funcs.pop()
                open_meta.pop()
                metric.end_lineno = max(metric.lineno, index - 1)
                metric.length = metric.end_lineno - metric.lineno + 1
                functions.append(metric)
            depth = min(indent, 24) // 2
            max_depth = max(max_depth, depth)

        if indent is None:
            continue

        complexity += sum(len(p.findall(line)) for p in branch_patterns)

        match = _FUNC_START.match(line)
        if match and not line.rstrip().endswith(";"):
            name, args = match.group(2), _count_args_heuristic(line)
        else:
            anon = _ANON_FUNC.match(line)
            if anon and not line.rstrip().endswith(";"):
                name, args = anon.group(1), _count_args_heuristic(line)
            else:
                name, args = None, 0

        if name:
            open_funcs.append(FuncMetric(
                name=name,
                lineno=index,
                end_lineno=len(lines),
                length=0,
                args=args,
                complexity=0,
                max_depth=max_depth,
            ))
            open_meta.append((depth, indent))

    # Anything still open runs to the end of the file.
    for metric in open_funcs:
        metric.end_lineno = len(lines)
        metric.length = len(lines) - metric.lineno + 1
        functions.append(metric)

    for metric in functions:
        metric.max_depth = max_depth
    return functions, complexity, max_depth


def _count_args_heuristic(line: str) -> int:
    start = line.find("(")
    if start == -1:
        return 0
    depth = 0
    args = []
    current = []
    for char in line[start:]:
        if char == "(":
            depth += 1
            if depth == 1:
                continue
        elif char == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(current))
                break
        elif char == "," and depth == 1:
            args.append("".join(current))
            current = []
            continue
        if depth >= 1:
            current.append(char)
    args = [a.strip() for a in args if a.strip()]
    return len(args)


# --------------------------------------------------------------------------
# Duplication
# --------------------------------------------------------------------------

@dataclass
class Clone:
    lines: int
    first_file: str
    first_line: int
    second_file: str
    second_line: int


def _normalized_lines(stats: FileStats, text: str) -> list[tuple[int, str]]:
    """Lines worth hashing, with literals masked but identifiers kept.

    Masking numbers and string literals is what makes a copied block match
    after a value changed. Identifiers are deliberately preserved: renaming
    every identifier makes unrelated-but-similar lines collide, which produces
    far more noise than signal.
    """
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "//", "*", "--", "/*")):
            continue
        if len(line) < 12:
            continue
        collapsed = re.sub(r"\s+", " ", line)
        collapsed = re.sub(r'"[^"]*"|\'[^\']*\'|`[^`]*`', "STR", collapsed)
        collapsed = re.sub(r"\b\d+\b", "NUM", collapsed)
        out.append((number, collapsed))
    return out


def find_duplication(
    files: list[FileInfo],
    window: int,
    min_lines: int,
    max_windows: int,
) -> tuple[list[Clone], bool]:
    """Detect repeated blocks with normalized-window hashing.

    Every window of ``window`` significant lines is hashed into a global
    ``(file, line) -> digest`` map. A digest seen in two places marks the
    start of a duplicated block, which is then extended for as long as
    consecutive windows in both files keep hashing equal. A run of ``R``
    matching windows corresponds to ``R + window - 1`` identical lines.
    """
    lookup: dict[tuple[str, int], str] = {}
    budget = max_windows
    truncated = False

    for info in files:
        if info.size > 512 * 1024:
            continue
        lines = _normalized_lines(info, info.text())
        if len(lines) < min_lines:
            continue
        if len(lines) - window + 1 > budget:
            truncated = True
            lines = lines[: budget + window]
        for offset in range(len(lines) - window + 1):
            chunk = "\n".join(line for _, line in lines[offset:offset + window])
            digest = hashlib.blake2b(chunk.encode("utf-8"), digest_size=12).hexdigest()
            lookup[(info.rel, lines[offset][0])] = digest
            budget -= 1
        if budget <= 0:
            truncated = True
            break

    by_digest: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for key, digest in lookup.items():
        by_digest[digest].append(key)

    # A run must be long enough that the resulting block reaches min_lines.
    needed = max(1, min_lines - window + 1)
    clones: list[Clone] = []

    for positions in by_digest.values():
        if len(positions) < 2:
            continue
        by_file: dict[str, list[int]] = defaultdict(list)
        for path, line in positions:
            by_file[path].append(line)
        for entry in by_file.values():
            entry.sort()
        names = sorted(by_file)
        consumed: set[tuple[str, int]] = set()
        for a in range(len(names)):
            for b in range(a, len(names)):
                clones.extend(_merge_runs(
                    names[a], names[b] if b > a else names[a],
                    by_file[names[a]],
                    by_file[names[b]] if b > a else by_file[names[a]],
                    needed, consumed, lookup, window,
                ))

    clones.sort(key=lambda c: -c.lines)
    return clones[:40], truncated


def _merge_runs(
    first: str,
    second: str,
    left: list[int],
    right: list[int],
    needed: int,
    consumed: set[tuple[str, int]],
    lookup: dict[tuple[str, int], str],
    window: int,
) -> list[Clone]:
    """Walk two sorted line lists together, extending runs of equal windows."""
    out: list[Clone] = []
    i = j = 0
    same_file = first == second
    while i < len(left) and j < len(right):
        la, lb = left[i], right[j]
        if (first, la) in consumed or (second, lb) in consumed:
            if (first, la) in consumed:
                i += 1
            else:
                j += 1
            continue
        if la == lb and not (same_file and la == lb):
            run = 0
            while (
                (first, la + run) not in consumed
                and (second, lb + run) not in consumed
                and lookup.get((first, la + run)) is not None
                and lookup.get((first, la + run)) == lookup.get((second, lb + run))
            ):
                run += 1
            if run >= needed:
                out.append(Clone(
                    lines=run + window - 1,
                    first_file=first,
                    first_line=la,
                    second_file=second,
                    second_line=lb,
                ))
                for step in range(run):
                    consumed.add((first, la + step))
                    consumed.add((second, lb + step))
                i += run
                j += run
                continue
        if la <= lb:
            i += 1
        else:
            j += 1
    return out


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------

class ComplexityAnalyzer(Analyzer):
    dimension = Dimension.COMPLEXITY
    name = "complexity"

    def available(self, ctx: Context) -> bool:
        return bool(ctx.source_files(include_tests=True))

    def analyze(self, ctx: Context) -> Result:
        config = ctx.config
        findings: list = []
        stats: list[FileStats] = []

        large_file_lines = int(config.get("large_file_lines", 500))
        long_function_lines = int(config.get("long_function_lines", 60))
        complex_score = int(config.get("complex_function_score", 12))
        max_nesting = int(config.get("max_nesting_depth", 4))
        max_params = int(config.get("max_parameters", 6))
        long_line_chars = int(config.get("long_line_chars", 120))
        max_long_ratio = float(config.get("max_long_line_ratio", 0.15))
        large_test_lines = int(config.get("test_file_lines", 800))

        candidates = [f for f in ctx.source_files(include_tests=True) if f.size > 0]
        for info in candidates:
            stat = _stat_for(info)
            stats.append(stat)

        source_stats = [s for s in stats if not s.is_test]
        test_stats = [s for s in stats if s.is_test]

        total_code = sum(s.code_lines for s in stats)
        total_source_code = sum(s.code_lines for s in source_stats)
        total_comment = sum(s.comment_lines for s in stats)
        total_blank = sum(s.blank_lines for s in stats)
        exact_files = sum(1 for s in stats if s.exact)
        heuristic_files = len(stats) - exact_files

        # -- per-file findings ------------------------------------------------
        for stat in stats:
            confidence = Confidence.HIGH if stat.exact else Confidence.HEURISTIC
            if stat.parse_error:
                findings.append(make_finding(
                    "complexity.parse_error",
                    detail=f"{stat.rel} could not be parsed: {stat.parse_error}",
                    confidence=Confidence.HIGH,
                    evidence=[f"Language: {stat.language}"],
                    locations=[Location(stat.rel)],
                    occurrences=1,
                ))
                continue

            if stat.total_lines == 0 and stat.code_lines == 0:
                findings.append(make_finding(
                    "complexity.empty_file",
                    detail=f"{stat.rel} contains no executable lines.",
                    locations=[Location(stat.rel)],
                ))
                continue

            if stat.is_test:
                if stat.total_lines > large_test_lines:
                    findings.append(make_finding(
                        "complexity.large_file",
                        detail=(f"{stat.rel} is {stat.total_lines} lines "
                                f"(test file, threshold {large_test_lines})."),
                        confidence=confidence,
                        evidence=[f"{stat.code_lines} code lines"],
                        locations=[Location(stat.rel)],
                    ))
                continue

            if stat.total_lines > large_file_lines:
                findings.append(make_finding(
                    "complexity.large_file",
                    detail=(f"{stat.rel} is {stat.total_lines} lines, over the "
                            f"{large_file_lines}-line threshold."),
                    confidence=confidence,
                    evidence=[
                        f"{stat.code_lines} code, {stat.comment_lines} comment, "
                        f"{stat.blank_lines} blank",
                    ],
                    locations=[Location(stat.rel)],
                ))

            ratio = stat.long_lines / stat.code_lines if stat.code_lines else 0.0
            if stat.long_lines and ratio > max_long_ratio and stat.code_lines >= 20:
                findings.append(make_finding(
                    "complexity.long_lines",
                    detail=(f"{stat.rel}: {stat.long_lines} of {stat.code_lines} code "
                            f"lines exceed {long_line_chars} characters."),
                    confidence=confidence,
                    evidence=[f"{ratio:.0%} of lines are over-long"],
                    locations=[Location(stat.rel)],
                ))

            if stat.max_depth > max_nesting:
                findings.append(make_finding(
                    "complexity.deep_nesting",
                    detail=(f"{stat.rel} nests {stat.max_depth} levels deep "
                            f"(threshold {max_nesting})."),
                    confidence=confidence,
                    locations=[Location(stat.rel, 1)],
                ))

            for func in stat.functions:
                if func.length > long_function_lines:
                    findings.append(make_finding(
                        "complexity.long_function",
                        detail=(f"{func.name}() in {stat.rel} is {func.length} lines "
                                f"(threshold {long_function_lines})."),
                        confidence=confidence,
                        locations=[Location(stat.rel, func.lineno)],
                    ))
                if func.complexity > complex_score:
                    findings.append(make_finding(
                        "complexity.complex_function",
                        detail=(f"{func.name}() in {stat.rel} has a branch complexity "
                                f"of ~{func.complexity} (threshold {complex_score})."),
                        confidence=confidence,
                        locations=[Location(stat.rel, func.lineno)],
                    ))
                if func.args > max_params:
                    findings.append(make_finding(
                        "complexity.many_parameters",
                        detail=(f"{func.name}() in {stat.rel} takes {func.args} "
                                f"parameters (threshold {max_params})."),
                        confidence=confidence,
                        locations=[Location(stat.rel, func.lineno)],
                    ))

        # -- duplication ------------------------------------------------------
        clones, dup_truncated = find_duplication(
            candidates,
            window=int(config.get("duplication_window", 10)),
            min_lines=int(config.get("duplication_min_lines", 20)),
            max_windows=int(config.get("duplication_max_windows", 1500000)),
        )
        for clone in clones:
            findings.append(make_finding(
                "complexity.duplication",
                detail=(f"{clone.lines} duplicated lines between "
                        f"{clone.first_file}:{clone.first_line} and "
                        f"{clone.second_file}:{clone.second_line}."),
                evidence=[f"{clone.lines} repeated lines"],
                locations=[
                    Location(clone.first_file, clone.first_line),
                    Location(clone.second_file, clone.second_line),
                ],
            ))

        top_files = sorted(source_stats, key=lambda s: -s.total_lines)[:10]
        top_functions = sorted(
            ((f, s.rel) for s in source_stats for f in s.functions),
            key=lambda pair: (-pair[0].length, pair[1]),
        )[:10]

        comment_ratio = pct(total_comment, max(1, total_code + total_comment))

        metrics: dict[str, Any] = {
            "total_lines": sum(s.total_lines for s in stats),
            "code_lines": total_code,
            "source_code_lines": total_source_code,
            "test_code_lines": sum(s.code_lines for s in test_stats),
            "comment_lines": total_comment,
            "blank_lines": total_blank,
            "comment_ratio": round(comment_ratio, 1),
            "files_measured": len(stats),
            "files_exact": exact_files,
            "files_heuristic": heuristic_files,
            "languages": _language_breakdown(stats),
            "largest_files": [
                {"file": s.rel, "lines": s.total_lines, "code": s.code_lines}
                for s in top_files
            ],
            "longest_functions": [
                {"file": func_file, "function": m.name, "lines": m.length,
                 "complexity": m.complexity, "line": m.lineno}
                for m, func_file in top_functions
            ],
            "max_nesting_depth": max((s.max_depth for s in stats), default=0),
            "duplication_blocks": len(clones),
            "duplication_truncated": dup_truncated,
            "parse_failures": [s.rel for s in stats if s.parse_error][:20],
        }

        notes: list[str] = []
        if dup_truncated:
            notes.append("duplication scan hit its window budget; results are partial")
        if heuristic_files:
            notes.append(
                f"{heuristic_files} of {len(stats)} files measured with heuristics "
                f"(exact for Python only)"
            )

        result = Result(findings=findings, metrics=metrics)
        if notes:
            result.metrics["notes"] = notes
        ctx.scratch["complexity_by_file"] = {s.rel: s for s in stats}
        return result


def _stat_for(info: FileInfo) -> FileStats:
    text = info.text()
    language = info.language
    stat = FileStats(
        rel=info.rel,
        language=language.name if language else "Unknown",
        is_test=info.is_test,
    )
    if language is None:
        stat.total_lines = len(text.splitlines())
        return stat

    counts, stripped = _line_stats(text, language)
    for key, value in counts.items():
        setattr(stat, key, value)

    if language.name == "Python":
        functions, complexity, depth, error = _analyze_python(text)
        stat.functions = functions
        stat.complexity = complexity
        stat.parse_error = error
        stat.max_depth = depth
    else:
        functions, complexity, depth = _analyze_heuristic(text, language.name)
        stat.functions = functions
        stat.complexity = complexity
        stat.max_depth = depth
    return stat


def _language_breakdown(stats: list[FileStats]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for stat in stats:
        bucket = out.setdefault(stat.language, {"files": 0, "code_lines": 0})
        bucket["files"] += 1
        bucket["code_lines"] += stat.code_lines
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["code_lines"]))
