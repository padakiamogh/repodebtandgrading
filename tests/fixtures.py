"""Deterministic fixture repositories for tests.

Git history is created with pinned author/committer dates so churn, bus
factor, and staleness assertions do not depend on when the test runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

DAY = 86400
#: Fixed base timestamp (2023-06-01T00:00:00Z), used only when a test pins it.
#: The default fixture clock is "now": analyzers measure *ages* ("changed 40
#: days ago") against ``time.time()``, so stamping commits against a literal
#: date would quietly push every fixture out of the history window as the
#: clock advances, and the affected tests would stop testing anything.
BASE = 1_685_558_400


def git(root: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace')}"
        )
    return result.stdout.decode("utf-8", "replace")


class TempDirCase(unittest.TestCase):
    """Base class providing a scratch directory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="repodebt-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, rel: str, content: str) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def audit(self, overrides: dict | None = None, **kwargs):
        """Run the full audit pipeline over the scratch directory.

        Going through ``run_audit`` rather than calling analyzers directly is
        deliberate: hotspots reads complexity stats off the shared scratch pad,
        so a hand-built Context would silently report zero hotspots.
        """
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from repodebt import config as cfg
        from repodebt.audit import run_audit

        config = cfg.load(self.tmp)
        config.data.update(overrides or {})
        return run_audit(self.tmp, config, **kwargs)

    @staticmethod
    def rule_ids(report) -> set[str]:
        return {f.id for f in report.findings}

    @staticmethod
    def findings_for(report, rule_id: str) -> list:
        return [f for f in report.findings if f.id == rule_id]


class RepoCase(TempDirCase):
    """A scratch directory initialised as a git repository."""

    #: Three authors, because the bus-factor signal needs at least three
    #: contributors to distinguish "one person wrote this" from "a few people
    #: share it". Two authors cannot produce an even split.
    authors = (
        ("Ada Lovelace", "ada@example.com"),
        ("Grace Hopper", "grace@example.com"),
        ("Alan Turing", "alan@example.com"),
    )
    #: Pin a literal epoch timestamp instead of tracking the wall clock.
    pinned_now: float | None = None

    def init_repo(self, branch: str = "main") -> Path:
        git(self.tmp, "init", "-q", "-b", branch)
        git(self.tmp, "config", "user.name", self.authors[0][0])
        git(self.tmp, "config", "user.email", self.authors[0][1])
        git(self.tmp, "config", "commit.gpgsign", "false")
        return self.tmp

    def commit(
        self,
        message: str,
        files: dict[str, str] | None = None,
        author: tuple[str, str] | None = None,
        days_ago: float = 0,
        allow_empty: bool = False,
    ) -> str:
        name, email = author or self.authors[0]
        for rel, content in (files or {}).items():
            self.write(rel, content)
        if files:
            git(self.tmp, "add", "-A")
        stamp = self._stamp(days_ago)
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            # Only needed when there is nothing staged; with real changes in
            # the tree git refuses anyway, so ask for it either way and let
            # git decide.
            args.insert(2, "--allow-empty")
        git(self.tmp, *args, env=self._env(name, email, days_ago, stamp))
        return message

    def _stamp(self, days_ago: float) -> str:
        base = self.pinned_now if self.pinned_now is not None else time.time()
        return f"{int(base - days_ago * DAY)} +0000"

    def _env(self, name: str, email: str, days_ago: float, stamp: str | None = None) -> dict:
        when = stamp or self._stamp(days_ago)
        return {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        }

    def branch_off(self, name: str, days_ago: float = 0) -> None:
        """Create a branch with one commit on it, then return to the base."""
        git(self.tmp, "checkout", "-q", "-b", name)
        self.commit(f"chore: start {name}", allow_empty=True, days_ago=days_ago)
        git(self.tmp, "checkout", "-q", "-")

    def merge_branch(
        self,
        name: str,
        days_ago: float = 0,
        files: dict[str, str] | None = None,
        author: tuple[str, str] | None = None,
    ) -> None:
        """Branch off ``name``, commit, and merge it back with --no-ff.

        ``--no-ff`` is what produces a real merge commit; without it a fast
        forward leaves no merge in the log and merge-ratio tests measure
        nothing.
        """
        base = git(self.tmp, "rev-parse", "--abbrev-ref", "HEAD").strip()
        git(self.tmp, "checkout", "-q", "-b", name)
        self.commit(f"feat: work on {name}", files=files, author=author,
                    days_ago=days_ago, allow_empty=True)
        git(self.tmp, "checkout", "-q", base)
        who = author or self.authors[0]
        git(self.tmp, "merge", "-q", "--no-ff", "--no-edit", name,
            env=self._env(who[0], who[1], days_ago, self._stamp(days_ago)))


def python_file(funcs: int = 1, nested: int = 0, lines_per_func: int = 5) -> str:
    """Generate a Python file with a predictable number of functions."""
    out = ['"""Generated fixture module."""', "", "VALUE = 1", ""]
    for index in range(funcs):
        out.append(f"def function_{index}(a, b):")
        out.append('    """Do a thing."""')
        for step in range(lines_per_func):
            out.append(f"    result_{step} = a + b + {step}")
        if nested:
            out.append("")
            out.append("    def helper(value):")
            for _ in range(nested):
                out.append("        if value:")
                out.append("            value += 1")
        out.append(f"    return function_{index}")
        out.append("")
    return "\n".join(out)


def branchy_function(name: str = "branchy", branches: int = 15) -> str:
    """A function with a known cyclomatic complexity of ``branches + 1``."""
    out = [f"def {name}(value):", '    """Complex decision logic."""', "    result = 0"]
    for index in range(branches):
        out.append(f"    if value == {index}:")
        out.append(f"        result += {index}")
    out.append("    return result")
    out.append("")
    return "\n".join(out)


def nested_function(levels: int = 6, name: str = "deep") -> str:
    """A function nested ``levels`` control-flow levels deep."""
    out = [f"def {name}(value):", '    """Deeply nested."""', "    result = 0"]
    indent = "    "
    for level in range(levels):
        out.append(f"{indent}if value > {level}:")
        indent += "    "
    out.append(f"{indent}result = value")
    out.append("    return result")
    out.append("")
    return "\n".join(out)
