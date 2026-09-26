"""Configuration loading and threshold defaults.

Precedence, lowest to highest: built-in defaults, ``repodebt.toml`` (or a
``[tool.repodebt]`` table in ``pyproject.toml``), CLI flags.

The effective config is hashed into ``config_digest`` so a baseline comparison
can tell you that a score moved because the thresholds moved, not the code.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on 3.10 and below
    tomllib = None  # type: ignore[assignment]

from .model import Dimension

CONFIG_FILENAMES = ("repodebt.toml", ".repodebt.toml")
IGNORE_FILENAME = ".repodebtignore"

#: Directories that are never source, regardless of ignore files.
DEFAULT_EXCLUDES = [
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    "third_party",
    "3rdparty",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".gradle",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "target",
    "out",
    "coverage",
    "htmlcov",
    ".next",
    ".nuxt",
    ".cache",
    ".terraform",
    "site-packages",
    ".eggs",
    "*.egg-info",
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.map",
    "*.pb.go",
    "*_pb2.py",
    "*.pb.cc",
    "*.pb.h",
    "*_generated.go",
    "*.g.dart",
    "*.designer.cs",
    "*.min.map",
]

DEFAULT_INCLUDES: list[str] = []

#: Severity -> penalty points. Confidence multiplies these at scoring time.
SEVERITY_PENALTY = {
    "critical": 10.0,
    "high": 6.0,
    "medium": 3.0,
    "low": 1.0,
    "info": 0.3,
}

DEFAULTS: dict[str, Any] = {
    "weights": {
        "complexity": 0.25,
        "tests": 0.25,
        "hotspots": 0.20,
        "deps": 0.15,
        "hygiene": 0.15,
    },
    "grade_bands": [
        [90.0, "A"],
        [80.0, "B"],
        [70.0, "C"],
        [60.0, "D"],
        [0.0, "F"],
    ],
    # Penalty budget per dimension. A dimension's score is
    # 100 * (1 - min(1, penalty / budget)), so the budget is the total
    # penalty that would drive that dimension to zero.
    "budget": {
        "complexity": 40.0,
        "hotspots": 30.0,
        "tests": 40.0,
        "hygiene": 30.0,
        "deps": 30.0,
    },
    "max_findings_per_rule": 12,
    # A single CRITICAL finding (a known-exploitable vulnerability, say) is
    # only 10 penalty points, which a large healthy repo can easily average
    # away into a perfect "A". That reads as a bug to anyone who sees the
    # finding list next to the grade, so critical findings clamp the overall
    # score to this ceiling. The default sits just under the A band: the best
    # achievable grade with an open critical is a B. Set to null to disable.
    "critical_grade_cap": 89.0,
    "history_window_days": 180,
    "dormant_days": 90,
    "large_file_lines": 500,
    "long_function_lines": 60,
    "complex_function_score": 12,
    "max_nesting_depth": 4,
    "max_parameters": 6,
    "long_line_chars": 120,
    "max_long_line_ratio": 0.15,
    "duplication_window": 10,
    "duplication_min_lines": 20,
    "duplication_max_windows": 1500000,
    "test_file_lines": 800,
    "test_min_ratio": 0.15,
    "test_min_assertions_per_100_lines": 2.0,
    "dormant_branch_days": 90,
    "bus_factor_min_authors": 2,
    "conventional_commit_min_ratio": 0.6,
    # A merge commit cannot exist without at least one non-merge commit
    # behind it on the merged side, so merge commits are always at most half
    # the history. A "max" above 0.5 could therefore never fire. 0.45 is
    # reachable only by a repo that merges every single branch with an
    # explicit --no-ff and never lands on a linear history.
    "max_merge_ratio": 0.45,
    "min_merge_ratio": 0.05,
    "large_blob_bytes": 500 * 1024,
    "min_readme_chars": 200,
    "online": False,
    "cache_ttl_hours": 24,
    # None means "use ~/.cache/repodebt"; see Config.cache_dir.
    "cache_dir": None,
    "max_file_bytes": 2 * 1024 * 1024,
    "exclude": list(DEFAULT_EXCLUDES),
    "include": list(DEFAULT_INCLUDES),
}


class ConfigError(Exception):
    pass


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:  # pragma: no cover
        raise ConfigError(
            "tomllib is unavailable; repodebt needs Python 3.11+ to read TOML config"
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        raise ConfigError(f"could not parse {path}: {exc}") from exc


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None
    extra_excludes: list[str] = field(default_factory=list)
    extra_includes: list[str] = field(default_factory=list)
    #: Unrecognised keys found in the config file. A config that parses but
    #: applies nothing is the worst failure mode for a tool whose whole
    #: promise is that the number is trustworthy, so these are surfaced.
    unknown_keys: list[str] = field(default_factory=list)

    # -- accessors ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def weight(self, dimension: Dimension) -> float:
        return float(self.data["weights"].get(dimension.value, 0.0))

    def budget(self, dimension: Dimension) -> float:
        return float(self.data["budget"].get(dimension.value, 30.0))

    @property
    def grade_bands(self) -> list[tuple[float, str]]:
        return [(float(x[0]), str(x[1])) for x in self.data["grade_bands"]]

    @property
    def max_findings_per_rule(self) -> int:
        return int(self.data["max_findings_per_rule"])

    @property
    def critical_grade_cap(self) -> float | None:
        cap = self.data.get("critical_grade_cap")
        if cap is None:
            return None
        return float(cap)

    @property
    def history_window_days(self) -> int:
        return int(self.data["history_window_days"])

    @property
    def dormant_days(self) -> int:
        return int(self.data["dormant_days"])

    @property
    def dormant_branch_days(self) -> int:
        return int(self.data["dormant_branch_days"])

    @property
    def bus_factor_min_authors(self) -> int:
        return int(self.data["bus_factor_min_authors"])

    @property
    def conventional_commit_min_ratio(self) -> float:
        return float(self.data["conventional_commit_min_ratio"])

    @property
    def max_merge_ratio(self) -> float:
        return float(self.data["max_merge_ratio"])

    @property
    def min_merge_ratio(self) -> float:
        return float(self.data["min_merge_ratio"])

    def excludes(self) -> list[str]:
        return list(self.data.get("exclude", [])) + list(self.extra_excludes)

    def includes(self) -> list[str]:
        return list(self.data.get("include", [])) + list(self.extra_includes)

    def cache_dir(self) -> Path:
        """Where online-advisory results are cached between runs.

        Honours an explicit ``cache_dir`` so the cache can be relocated (CI
        caches, a per-user scratch dir) and so tests never write into a
        developer's real ``~/.cache``. Falls back to the XDG-style default.
        """
        configured = self.data.get("cache_dir")
        base = Path(configured).expanduser() if configured else (
            Path.home() / ".cache" / "repodebt"
        )
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A read-only or missing home is not a reason to fail the audit;
            # the cache is an optimisation, not a requirement.
            pass
        return base

    def digest(self) -> str:
        payload = json.dumps(self.data, sort_keys=True, default=str).encode()
        return hashlib.sha256(payload).hexdigest()[:12]

    def score_config_changed(self, other_digest: str) -> bool:
        return other_digest and other_digest != self.digest()


def load(
    root: Path,
    config_path: Path | None = None,
    extra_excludes: list[str] | None = None,
    extra_includes: list[str] | None = None,
) -> Config:
    """Load effective config for ``root``."""
    data = json.loads(json.dumps(DEFAULTS))  # deep copy
    source: Path | None = None

    candidate: Path | None = None
    if config_path:
        candidate = Path(config_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            raise ConfigError(f"config file not found: {candidate}")
    else:
        for name in CONFIG_FILENAMES:
            if (root / name).exists():
                candidate = root / name
                break
        else:
            pyproject = root / "pyproject.toml"
            if pyproject.exists():
                parsed = _load_toml(pyproject)
                if "repodebt" in parsed.get("tool", {}):
                    candidate = pyproject

    unknown: list[str] = []
    if candidate is not None:
        parsed = _load_toml(candidate)
        if candidate.name == "pyproject.toml":
            parsed = parsed.get("tool", {}).get("repodebt", {})
        unknown = _unknown_keys(parsed)
        data = _deep_merge(data, parsed)
        source = candidate

    # .repodebtignore is additive on top of whatever the config file said, and
    # sits below explicit --exclude flags so a one-off CLI override can still
    # reach a path the ignore file hides.
    ignored = _read_ignore_file(root)
    configured = list(data.get("exclude", []))
    data["exclude"] = configured + [g for g in ignored if g not in configured]

    return Config(
        root=root,
        data=data,
        source_path=source,
        extra_excludes=list(extra_excludes or []),
        extra_includes=list(extra_includes or []),
        unknown_keys=unknown,
    )


#: Keys accepted inside each table-valued setting.
_NESTED_KEYS: dict[str, set[str]] = {
    "weights": {d.value for d in Dimension},
    "budget": {d.value for d in Dimension},
}


def _unknown_keys(parsed: dict[str, Any]) -> list[str]:
    """Report top-level keys repodebt does not recognise.

    This cannot catch every mistake -- a top-level key accidentally nested
    inside a ``[weights]`` table still looks like valid data -- but it turns
    the common typo (``largefile_lines = 500``) from a silent no-op into
    something the user is told about.
    """
    unknown: list[str] = []
    for key, value in parsed.items():
        if key not in DEFAULTS:
            unknown.append(key)
        elif key in _NESTED_KEYS and isinstance(value, dict):
            for sub in value:
                if sub not in _NESTED_KEYS[key]:
                    unknown.append(f"{key}.{sub}")
    return sorted(unknown)


def _read_ignore_file(root: Path) -> list[str]:
    """Parse ``.repodebtignore`` as newline-separated globs.

    Blank lines and ``#`` comments are skipped. This is deliberately not
    gitignore syntax: negation, anchoring, and per-directory scoping are
    implemented in ``walker`` to a different degree, and a half-supported
    gitignore dialect would be worse than an honest glob list.
    """
    path = root / IGNORE_FILENAME
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    globs: list[str] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        globs.append(entry)
    return globs


# --------------------------------------------------------------------------
# Glob matching
# --------------------------------------------------------------------------

_DIR_ONLY = re.compile(r"/$")


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = ["(?s:"]
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        if char == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(char))
                i += 1
                continue
            body = pattern[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = end + 1
            continue
        out.append(re.escape(char))
        i += 1
    out.append(")")
    return re.compile("^" + "".join(out) + "$")


@dataclass(frozen=True)
class Pattern:
    """One compiled gitignore-style glob."""

    text: str
    regex: re.Pattern[str]
    dir_only: bool
    negated: bool
    has_slash: bool

    def matches_dir_prefix(self, rel_path: str) -> bool:
        """True when any ancestor directory of ``rel_path`` matches."""
        return self._matches_ancestor(rel_path.split("/"))

    def matches(self, rel_path: str, is_dir: bool = False) -> bool:
        if self.negated:
            return False
        parts = rel_path.split("/")
        if not self.has_slash:
            if self.dir_only:
                # A directory-only name excludes the directory's contents but
                # not a file that happens to share the name.
                if is_dir:
                    return self.regex.match(parts[-1]) is not None
                return any(self.regex.match(part) for part in parts[:-1])
            return any(self.regex.match(part) for part in parts)
        if self.dir_only:
            if is_dir:
                return self.regex.match(rel_path) is not None
            return self._matches_ancestor(parts)
        return (self.regex.match(rel_path) is not None
                or self._matches_ancestor(parts))

    def _matches_ancestor(self, parts: list[str]) -> bool:
        """True when a containing directory matches, so its contents do too."""
        for index in range(1, len(parts)):
            if self.regex.match("/".join(parts[:index])):
                return True
        return False


def compile_patterns(patterns: list[str]) -> list[Pattern]:
    """Translate gitignore-ish globs into matchable patterns.

    Supports ``*``, ``**``, ``?``, character classes, directory-only patterns
    (trailing ``/``), and negation via ``!``.
    """
    compiled: list[Pattern] = []
    for raw in patterns:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        negated = text.startswith("!")
        body = text[1:] if negated else text
        dir_only = body.endswith("/") or negated
        body = body.rstrip("/")
        if not body:
            continue
        compiled.append(Pattern(
            text=text,
            regex=_glob_to_regex(body),
            dir_only=dir_only,
            negated=negated,
            has_slash="/" in body,
        ))
    return compiled


def match_any(rel_path: str, patterns: list[Pattern], is_dir: bool = False) -> bool:
    for pattern in patterns:
        if pattern.matches(rel_path, is_dir):
            return True
    return False


def first_match(rel_path: str, patterns: list[Pattern], is_dir: bool = False) -> str | None:
    for pattern in patterns:
        if pattern.matches(rel_path, is_dir):
            return pattern.text
    return None


def fnmatch_any(value: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)


__all__ = [
    "Config",
    "ConfigError",
    "DEFAULTS",
    "SEVERITY_PENALTY",
    "Pattern",
    "load",
    "compile_patterns",
    "match_any",
    "first_match",
    "fnmatch_any",
]
