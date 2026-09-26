"""File discovery, language detection, and vendor/generated-code exclusion.

Discovery prefers ``git ls-files`` because it is both fast and already
consistent with the project's own ignore rules. A manual walk is the
fallback for archives and non-repository directories.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg


@dataclass(frozen=True)
class Language:
    name: str
    extensions: tuple[str, ...]
    line_comments: tuple[str, ...] = ("//",)
    block_comments: tuple[tuple[str, str], ...] = (("/*", "*/"),)
    doc_string_delims: tuple[str, ...] = ('"""', "'''")
    is_source: bool = True


LANGUAGES: tuple[Language, ...] = (
    Language("Python", (".py", ".pyi"), ("#",), ()),
    Language("TypeScript", (".ts", ".tsx", ".mts", ".cts")),
    Language("JavaScript", (".js", ".jsx", ".mjs", ".cjs")),
    Language("Go", (".go",), ("//",), (("/*", "*/"),)),
    Language("Rust", (".rs",), ("//",), (("/*", "*/"),)),
    Language("Java", (".java",)),
    Language("Kotlin", (".kt", ".kts")),
    Language("C#", (".cs",)),
    Language("C", (".c", ".h"), ("//",), (("/*", "*/"),)),
    Language("C++", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), ("//",), (("/*", "*/"),)),
    Language("Ruby", (".rb",), ("#",), (("=begin", "=end"),)),
    Language("PHP", (".php",), ("//", "#"), (("/*", "*/"),)),
    Language("Swift", (".swift",)),
    Language("Scala", (".scala",)),
    Language("Shell", (".sh", ".bash", ".zsh"), ("#",), ()),
    Language("PowerShell", (".ps1", ".psm1"), ("#",), (("<#", "#>"),)),
    Language("SQL", (".sql",), ("--",), (("/*", "*/"),)),
    Language("Elixir", (".ex", ".exs"), ("#",), ()),
    Language("Erlang", (".erl",), ("%",), ()),
    Language("Haskell", (".hs",), ("--",), (("{-", "-}"),)),
    Language("Lua", (".lua",), ("--",), (("--[[", "]]"),)),
    Language("R", (".r",), ("#",), ()),
    Language("Dart", (".dart",)),
    Language("Zig", (".zig",)),
    Language("Vue", (".vue",)),
    Language("Svelte", (".svelte",)),
    Language("Markdown", (".md", ".markdown"), ("#",), (), is_source=False),
    Language("YAML", (".yaml", ".yml"), ("#",), (), is_source=False),
    Language("JSON", (".json",), ("//",), (), is_source=False),
    Language("TOML", (".toml",), ("#",), (), is_source=False),
    Language("Dockerfile", (".dockerfile",), ("#",), (), is_source=False),
    Language("Make", (".mk",), ("#",), ()),
    Language("CMake", (".cmake",), ("#",), ()),
    Language("Terraform", (".tf", ".tfvars"), ("#",), (("/*", "*/"),)),
    Language("Text", (".txt", ".rst", ".adoc"), ("#",), (), is_source=False),
    Language("CSV", (".csv",), is_source=False),
    Language("Lockfile", (".lock",), is_source=False),
)

BY_EXTENSION: dict[str, Language] = {}
for _lang in LANGUAGES:
    for _ext in _lang.extensions:
        BY_EXTENSION.setdefault(_ext, _lang)

SPECIAL_FILENAMES: dict[str, Language] = {
    "Makefile": BY_EXTENSION[".mk"],
    "Dockerfile": BY_EXTENSION[".dockerfile"],
    "Jenkinsfile": BY_EXTENSION[".scala"],
    "Rakefile": BY_EXTENSION[".rb"],
    "Gemfile": BY_EXTENSION[".rb"],
    "Gemfile.lock": BY_EXTENSION[".lock"],
    "Pipfile": BY_EXTENSION[".toml"],
    "Pipfile.lock": BY_EXTENSION[".json"],
    "go.mod": BY_EXTENSION[".toml"],
    "go.sum": BY_EXTENSION[".txt"],
    "Cargo.toml": BY_EXTENSION[".toml"],
    "Cargo.lock": BY_EXTENSION[".toml"],
    "CMakeLists.txt": BY_EXTENSION[".cmake"],
    ".gitignore": BY_EXTENSION[".txt"],
    ".dockerignore": BY_EXTENSION[".txt"],
    ".editorconfig": BY_EXTENSION[".ini"] if ".ini" in BY_EXTENSION else BY_EXTENSION[".txt"],
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".avif",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
    ".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".o", ".obj", ".class", ".pyc",
    ".pyo", ".pyd", ".wasm", ".node", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".avi",
    ".mov", ".mkv", ".webm", ".psd", ".ai", ".sketch", ".ttc", ".pack", ".idx",
}

#: Files that declare dependencies. Checked by presence, not extension.
MANIFEST_FILENAMES = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Pipfile", "poetry.lock", "uv.lock", "Pipfile.lock", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
    "mix.exs", "pubspec.yaml", "Package.swift", "packages.config",
)

LOCKFILE_LANGUAGES = {
    "package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "yarn.lock": "npm",
    "pnpm-lock.yaml": "npm", "bun.lockb": "npm",
    "poetry.lock": "python", "uv.lock": "python", "Pipfile.lock": "python",
    "requirements.lock": "python", "pylock.toml": "python", "pylock.json": "python",
    "Cargo.lock": "cargo", "go.sum": "gomod", "Gemfile.lock": "bundler",
    "composer.lock": "composer", "pubspec.lock": "pub", "gradle.lockfile": "gradle",
    "mix.lock": "hex",
}

#: Manifest -> ecosystem, used for lockfile pairing.
MANIFEST_ECOSYSTEM = {
    "package.json": "npm",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "go.mod": "gomod",
    "Cargo.toml": "cargo",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "Gemfile": "bundler",
    "composer.json": "composer",
    "mix.exs": "hex",
    "pubspec.yaml": "pub",
    "Package.swift": "spm",
    "packages.config": "nuget",
}

#: Filename prefixes/suffixes that mark a test file, per ecosystem.
TEST_PATTERNS = (
    "test_*.py", "*_test.py", "conftest.py",
    "*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx",
    "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx",
    "*_test.go", "*_test.py",
    "*Test.java", "*Tests.java", "*Spec.java",
    "*_spec.rb", "spec_*.rb",
    "*_test.dart", "*_test.exs", "*_test.ex",
    "*Tests.cs", "*Test.cs", "*.spec.js",
)


@dataclass
class FileInfo:
    path: Path
    rel: str
    language: Language | None
    size: int
    is_test: bool = False
    excluded: bool = False
    exclude_reason: str = ""
    is_manifest: bool = False
    _text: str | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def is_source(self) -> bool:
        return self.language is not None and self.language.is_source

    def text(self, max_bytes: int | None = None) -> str:
        """Decoded file contents, cached. Undecodable bytes become U+FFFD."""
        if self._text is not None:
            return self._text
        try:
            raw = self.path.read_bytes()
        except OSError:
            self._text = ""
            return self._text
        if max_bytes is not None and len(raw) > max_bytes:
            raw = raw[:max_bytes]
        self._text = raw.decode("utf-8", errors="replace")
        return self._text

    def invalidate(self) -> None:
        self._text = None


def detect_language(rel: str) -> Language | None:
    name = rel.rsplit("/", 1)[-1]
    if name in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[name]
    for candidate in ("Dockerfile", "Makefile"):
        if name.startswith(candidate):
            return BY_EXTENSION.get(".dockerfile" if candidate == "Dockerfile" else ".mk")
    for lock in LOCKFILE_LANGUAGES:
        if name == lock:
            return BY_EXTENSION[".lock"]
    _, ext = _split_ext(name)
    if not ext:
        return None
    return BY_EXTENSION.get(ext.lower())


def _split_ext(name: str) -> tuple[str, str]:
    index = name.rfind(".")
    if index <= 0:
        return name, ""
    return name[:index], name[index:].lower()


def looks_like_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    parts = rel.lower().split("/")
    in_test_dir = any(
        p in ("test", "tests", "spec", "specs", "__tests__", "e2e") for p in parts[:-1]
    )
    for pattern in TEST_PATTERNS:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    if in_test_dir:
        # A file inside a test directory is a test even if oddly named.
        language = detect_language(rel)
        return language is not None and language.is_source
    return False


def looks_binary(path: Path, ext: str, sniff_bytes: int = 8192) -> bool:
    if ext in BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as handle:
            chunk = handle.read(sniff_bytes)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    return False


@dataclass
class WalkResult:
    files: list[FileInfo] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False
    source: str = "git"


def _exclusion_reason(rel: str, patterns: list[cfg.Pattern]) -> str | None:
    return cfg.first_match(rel, patterns)


def _prepare(patterns: list[str]) -> list[cfg.Pattern]:
    return cfg.compile_patterns(patterns)


def discover(
    root: Path,
    config: cfg.Config,
    use_git: bool = True,
    max_file_bytes: int | None = None,
) -> WalkResult:
    result = WalkResult()
    if max_file_bytes is None:
        max_file_bytes = int(config.get("max_file_bytes", 2 * 1024 * 1024))

    patterns = _prepare(config.excludes())
    ignore_file = root / cfg.IGNORE_FILENAME
    if ignore_file.exists():
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            patterns.extend(_prepare([line]))

    include_patterns = _prepare(config.includes()) if config.includes() else []
    has_include_filter = bool(include_patterns)

    raw_paths: list[str] = []
    if use_git and (root / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                 "--exclude-standard"],
                capture_output=True, timeout=60, check=False,
            )
            if proc.returncode == 0:
                raw_paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
                result.source = "git"
        except (OSError, subprocess.SubprocessError):
            raw_paths = []

    if not raw_paths:
        result.source = "walk"
        for path in sorted(root.rglob("*")):
            if path.is_file():
                raw_paths.append(path.relative_to(root).as_posix())

    for rel in raw_paths:
        rel = rel.replace("\\", "/")
        if has_include_filter and not _included(rel, include_patterns):
            continue
        reason = _exclusion_reason(rel, patterns)
        path = root / rel
        try:
            stat = path.stat()
        except OSError:
            continue

        if reason:
            result.excluded.append((rel, reason))
            continue
        if stat.st_size > max_file_bytes:
            result.excluded.append((rel, "oversized"))
            continue

        language = detect_language(rel)
        name = rel.rsplit("/", 1)[-1]
        if looks_binary(path, _split_ext(name)[1]):
            result.excluded.append((rel, "binary"))
            continue

        is_manifest = name in MANIFEST_ECOSYSTEM or name.startswith("requirements")
        result.files.append(
            FileInfo(
                path=path,
                rel=rel,
                language=language,
                size=stat.st_size,
                is_test=looks_like_test(rel) if language else False,
                is_manifest=is_manifest,
            )
        )

    result.files.sort(key=lambda f: f.rel)
    return result


def _included(rel: str, patterns: list[cfg.Pattern]) -> bool:
    included = False
    for pattern in patterns:
        if pattern.matches(rel):
            included = not pattern.negated
    return included


def summarize(files: list[FileInfo], excluded: list[tuple[str, str]], truncated: bool) -> dict:
    languages: dict[str, int] = {}
    source_count = 0
    total_bytes = 0
    for info in files:
        total_bytes += info.size
        if info.is_source and not info.is_test:
            source_count += 1
        if info.language:
            languages[info.language.name] = languages.get(info.language.name, 0) + 1
    return {
        "file_count": len(files),
        "source_file_count": source_count,
        "excluded_file_count": len(excluded),
        "total_bytes": total_bytes,
        "languages": languages,
        "truncated": truncated,
    }
