"""Thin subprocess wrapper around ``git``.

Every call is best-effort: a missing binary, a missing ``.git``, a corrupt
object store, or a timeout all degrade to "history unavailable" rather than
crashing the audit. Callers treat ``None`` as unknown, never as healthy.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field

GIT_TIMEOUT = 120


@dataclass
class Commit:
    sha: str
    author_name: str
    author_email: str
    timestamp: int
    subject: str
    parents: list[str]
    files: list[tuple[str, int, int]] = field(default_factory=list)  # (path, added, deleted)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass
class FileHistory:
    path: str
    commits: int = 0
    added: int = 0
    deleted: int = 0
    last_timestamp: int = 0
    authors: dict[str, int] = field(default_factory=dict)

    @property
    def churn(self) -> int:
        return self.added + self.deleted

    def dominant_author(self) -> str | None:
        if not self.authors:
            return None
        return max(self.authors.items(), key=lambda kv: (kv[1], kv[0]))[0]


@dataclass
class History:
    commits: list[Commit] = field(default_factory=list)
    files: dict[str, FileHistory] = field(default_factory=dict)
    authors: dict[str, int] = field(default_factory=dict)
    window_days: int = 0
    truncated: bool = False

    def author_commits_in_window(self, author: str) -> int:
        return self.authors.get(author, 0)

    def bus_factor(self) -> tuple[int, float]:
        """Number of authors covering 50% of commits, and that share."""
        if not self.authors:
            return 0, 0.0
        ordered = sorted(self.authors.values(), reverse=True)
        total = sum(ordered)
        running = 0
        for index, count in enumerate(ordered, start=1):
            running += count
            if running >= total / 2:
                return index, running / total
        return len(ordered), 1.0

    def longest_dry_spell_days(self, now: float) -> float:
        if not self.commits:
            return float("inf")
        stamps = sorted(c.timestamp for c in self.commits)
        longest = 0.0
        previous = stamps[0]
        for stamp in stamps[1:]:
            gap = stamp - previous
            if gap > longest:
                longest = gap
            previous = stamp
        # Also measure the gap from the last commit to now.
        tail = now - stamps[-1]
        return max(longest, tail) / 86400.0


class GitRepo:
    """Read-only git accessor."""

    def __init__(self, root, enabled: bool = True):
        self.root = str(root)
        self.available = False
        self.reason = ""
        self._binary = shutil.which("git")
        self._head: str | None = None
        self._branch: str | None = None
        self._log_cache: dict[int, History] = {}
        if not enabled:
            self.reason = "disabled via --no-git"
            return
        if not self._binary:
            self.reason = "git executable not found on PATH"
            return
        if self.run("rev-parse", "--git-dir") is None:
            self.reason = "not a git repository"
            return
        self.available = True

    # -- low level ---------------------------------------------------------
    def run(self, *args: str, timeout: int = GIT_TIMEOUT) -> str | None:
        if not self._binary:
            return None
        try:
            proc = subprocess.run(
                [self._binary, "-C", self.root, *args],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace")

    def run_ok(self, *args: str, timeout: int = GIT_TIMEOUT) -> bool:
        return self.run(*args, timeout=timeout) is not None

    # -- metadata ----------------------------------------------------------
    @property
    def head(self) -> str | None:
        if self._head is None:
            out = self.run("rev-parse", "HEAD")
            self._head = out.strip() if out else ""
        return self._head or None

    @property
    def branch(self) -> str | None:
        if self._branch is None:
            out = self.run("rev-parse", "--abbrev-ref", "HEAD")
            self._branch = out.strip() if out else ""
        return self._branch or None

    # -- history -----------------------------------------------------------
    def history(self, window_days: int) -> History:
        if not self.available:
            return History(window_days=window_days)
        if window_days in self._log_cache:
            return self._log_cache[window_days]

        # Two different separators, for two different jobs. ``-z`` makes git
        # emit each numstat record as its own NUL-terminated field, and that
        # NUL lives only in git's *output*. The joiner for the commit header
        # fields must be a character that survives a Windows command line:
        # CreateProcess rejects an argv string containing a NUL outright, so
        # building the format with a literal "\x00" raises ValueError before
        # git ever runs. US (0x1f) cannot appear in a commit subject.
        record_sep = "\x00"
        field_sep = "\x1f"
        fmt = field_sep.join(["%H", "%an", "%ae", "%at", "%P", "%s"])
        out = self.run(
            "log",
            f"--since={window_days}.days.ago",
            "--numstat",
            "-z",
            f"--pretty=format:{fmt}",
            "--no-color",
        )
        history = History(window_days=window_days)
        if out is None:
            self.reason = self.reason or "git log failed"
            return history

        commits = _parse_log(out, record_sep, field_sep)
        history.commits = commits
        for commit in commits:
            name = commit.author_email or commit.author_name
            history.authors[name] = history.authors.get(name, 0) + 1
            for path, added, deleted in commit.files:
                entry = history.files.get(path)
                if entry is None:
                    entry = FileHistory(path=path)
                    history.files[path] = entry
                entry.commits += 1
                entry.added += added
                entry.deleted += deleted
                entry.last_timestamp = max(entry.last_timestamp, commit.timestamp)
                entry.authors[name] = entry.authors.get(name, 0) + 1

        self._log_cache[window_days] = history
        return history

    def last_commit_timestamp(self) -> int:
        out = self.run("log", "-1", "--pretty=format:%at")
        try:
            return int((out or "").strip())
        except ValueError:
            return 0

    def branches_older_than(self, days: int, now: float) -> list[tuple[str, float]]:
        """Branches whose tip commit is older than ``days``."""
        out = self.run(
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)%09%(committerdate:unix)%09%(committerdate:short)",
            "refs/heads",
        )
        if not out:
            return []
        cutoff = now - days * 86400
        current = self.branch
        result: list[tuple[str, float]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name, raw_stamp = parts[0], parts[1]
            if name == current or name in ("master", "main") and current is None:
                continue
            try:
                stamp = int(raw_stamp)
            except ValueError:
                continue
            if stamp < cutoff:
                result.append((name, (now - stamp) / 86400.0))
        return result

    def tracked_paths(self) -> set[str]:
        out = self.run("ls-files")
        if not out:
            return set()
        return {line for line in out.splitlines() if line}


def _parse_log(out: str, record_sep: str, field_sep: str) -> list[Commit]:
    """Parse ``git log --numstat -z --pretty=format:<fields>``.

    With ``-z`` git NUL-terminates *every* record, and those record boundaries
    do not line up with commit boundaries. The commit header is coalesced with
    the first numstat line, and every further changed file arrives as its own
    bare numstat field carrying no header at all::

        <header><US>...<US><subject>\\n<added>\\t<deleted>\\t<path>\\0
        <added>\\t<deleted>\\t<path>\\0
        <added>\\t<deleted>\\t<path>\\0

    So a field introduces a new commit only if it carries the header
    separator; otherwise it is another file belonging to the commit already in
    hand. Reading every field as "header on line 0, numstat after" silently
    discarded every changed file after the first, so any commit touching more
    than one file undercounted its churn, its per-file commit count and its
    author spread -- and since real commits almost always touch several files,
    hotspot ranking and bus-factor analysis were both built on those numbers.
    """
    commits: list[Commit] = []
    current: Commit | None = None
    for field in out.split(record_sep):
        if not field.strip():
            continue
        lines = field.split("\n")
        if field_sep in lines[0]:
            header_parts = lines[0].split(field_sep)
            if len(header_parts) < 6:
                current = None
                continue
            sha, name, email, at, parents, subject = header_parts[:6]
            try:
                timestamp = int(at)
            except ValueError:
                current = None
                continue
            current = Commit(
                sha=sha.strip(),
                author_name=name,
                author_email=email,
                timestamp=timestamp,
                subject=subject,
                parents=[p for p in parents.split() if p],
            )
            commits.append(current)
            lines = lines[1:]
        if current is None:
            # A numstat record with no commit in hand cannot be attributed.
            continue
        for line in lines:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted = _numstat_cell(parts[0]), _numstat_cell(parts[1])
            if added is None or deleted is None:
                continue
            current.files.append((_unquote_path(parts[2]), added, deleted))
    return commits


def _numstat_cell(value: str) -> int | None:
    """Parse one numstat cell.

    Binary files report ``-`` for both counts. Those files are not analyzable
    for churn, but recording them with a zero delta keeps them present in the
    history instead of silently vanishing from it.
    """
    if value == "-":
        return 0
    if not value or not value.isdigit():
        return None
    return int(value)


def _unquote_path(path: str) -> str:
    """Undo git's C-style quoting of unusual paths."""
    if len(path) > 1 and path.startswith('"') and path.endswith('"'):
        body = path[1:-1]
        out = []
        index = 0
        while index < len(body):
            char = body[index]
            if char == "\\" and index + 1 < len(body):
                nxt = body[index + 1]
                mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
                out.append(mapping.get(nxt, nxt))
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)
    return path


def now() -> float:
    return time.time()
