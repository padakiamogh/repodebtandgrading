"""History-derived signals: hotspots, ownership, staleness, and bus factor.

These need a real git repository with real commit timestamps, so they build
one with the ``RepoCase`` fixture. Commit dates are stamped relative to the
current clock -- the analyzers compare commit ages against ``time.time()``,
so a fixture pinned to a literal date would silently fall out of the history
window and stop exercising the code under test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt import config as cfg  # noqa: E402
from repodebt.model import Dimension  # noqa: E402

from fixtures import RepoCase, TempDirCase, branchy_function, git, python_file  # noqa: E402


class HotspotTest(RepoCase):
    def test_churned_complex_file_becomes_a_hotspot(self):
        self.init_repo()
        # Five commits on one gnarly file beats five commits spread thin.
        for index in range(5):
            self.commit(
                f"feat: touch hotspot {index}",
                {"core.py": branchy_function("hot", 20) + f"\nSTEP_{index} = {index}\n"},
                days_ago=index * 2,
            )
        report = self.audit()
        hotspots = report.metrics["hotspots"]["hotspots"]
        self.assertTrue(hotspots)
        self.assertEqual(hotspots[0]["file"], "core.py")
        self.assertEqual(hotspots[0]["commits"], 5)
        self.assertIn("hotspots.high_churn_complex", self.rule_ids(report))

    def test_few_commits_below_threshold_are_ignored(self):
        self.init_repo()
        for index in range(2):  # MIN_COMMITS is 3
            self.commit(f"fix: minor {index}", {"core.py": python_file(1) + f"\nV{index} = 1\n"},
                        days_ago=index)
        report = self.audit()
        self.assertEqual(report.metrics["hotspots"]["hotspots"], [])
        self.assertNotIn("hotspots.high_churn_complex", self.rule_ids(report))

    def test_unchurned_file_outranks_churned_simple_file(self):
        self.init_repo()
        for index in range(6):
            self.commit(f"fix: simple churn {index}",
                        {"simple.py": python_file(1) + f"\nV{index} = {index}\n"},
                        days_ago=index)
        # The complex file is touched 4x: fewer commits, higher complexity.
        for index in range(4):
            self.commit(f"fix: complex churn {index}",
                        {"complex.py": branchy_function("cx", 25) + f"\nV{index} = {index}\n"},
                        days_ago=index)
        report = self.audit()
        top = report.metrics["hotspots"]["hotspots"][0]
        self.assertEqual(top["file"], "complex.py")
        self.assertLess(top["commits"], 6)
        self.assertGreater(top["complexity"], 1.0)

    def test_test_files_are_never_hotspots(self):
        self.init_repo()
        for index in range(6):
            self.commit(f"test: churn tests {index}",
                        {"tests/test_core.py": branchy_function("t", 20) + f"\nV{index} = {index}\n"},
                        days_ago=index)
        report = self.audit()
        ranked = [row["file"] for row in report.metrics["hotspots"]["hotspots"]]
        self.assertNotIn("tests/test_core.py", ranked)

    def test_sole_author_hotspot_is_flagged(self):
        self.init_repo()
        ada = self.authors[0]
        for index in range(5):
            self.commit(f"feat: solo work {index}",
                        {"core.py": branchy_function("solo", 20) + f"\nV{index} = {index}\n"},
                        author=ada, days_ago=index)
        report = self.audit()
        sole = self.findings_for(report, "hotspots.sole_owner")
        self.assertEqual(len(sole), 1)
        # The email must be masked, not printed in the clear.
        self.assertNotIn("ada@example.com", sole[0].evidence[0])

    def test_no_history_marks_hotspots_unavailable_not_healthy(self):
        self.init_repo()
        self.commit("feat: everything is ancient", {"core.py": python_file(2)},
                    days_ago=400)
        report = self.audit(overrides={"history_window_days": 30})
        # With nothing to rank, the dimension is left unscored rather than
        # counted as a clean 100.
        self.assertIn("hotspots_analysis", report.unavailable)
        self.assertIn("30", report.unavailable["hotspots_analysis"])
        self.assertIsNone(report.scorecard.get(Dimension.HOTSPOTS).score)

    def test_non_python_source_is_ranked_but_marked_less_certain(self):
        self.init_repo()
        for index in range(5):
            self.commit(f"feat: churn go {index}",
                        {"main.go": "package main\n\n" + "func work() {}\n" * (index + 1) * 20},
                        days_ago=index)
        report = self.audit()
        rows = report.metrics["hotspots"]["hotspots"]
        self.assertEqual(rows[0]["file"], "main.go")
        from repodebt.model import Confidence

        finding = self.findings_for(report, "hotspots.high_churn_complex")[0]
        self.assertIn(finding.confidence, (Confidence.MEDIUM, Confidence.HEURISTIC))

    def test_history_metrics_are_reported(self):
        self.init_repo()
        for index in range(4):
            self.commit(f"feat: add module {index}", {"mod.py": python_file(1, lines_per_func=index + 1)},
                        author=self.authors[index % 2], days_ago=index * 3)
        report = self.audit()
        metrics = report.metrics["hotspots"]
        self.assertEqual(metrics["commits_in_window"], 4)
        self.assertEqual(metrics["window_days"], 180)
        self.assertEqual(metrics["contributors_in_window"], 2)
        self.assertGreater(metrics["churn_total"], 0)


class OwnershipTest(RepoCase):
    def test_single_author_repo_is_flagged(self):
        self.init_repo()
        for index in range(5):
            self.commit(f"feat: solo {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        author=self.authors[0], days_ago=index)
        report = self.audit()
        self.assertIn("hygiene.single_author", self.rule_ids(report))
        self.assertEqual(report.metrics["hygiene"]["contributors"], 1)

    def test_two_author_repo_still_has_a_bus_factor_of_one(self):
        # Bus factor counts the authors covering *half* the commits, so a
        # perfectly even 2-person team scores 1: losing either person halves
        # the output. That is a real risk, so the rule fires.
        self.init_repo()
        for index in range(8):
            self.commit(f"feat: shared {index}",
                        {"a.py": python_file(1, lines_per_func=index + 1)},
                        author=self.authors[index % 2], days_ago=index)
        report = self.audit()
        self.assertNotIn("hygiene.single_author", self.rule_ids(report))
        self.assertIn("hygiene.bus_factor", self.rule_ids(report))
        self.assertEqual(report.metrics["hygiene"]["contributors"], 2)
        self.assertEqual(report.metrics["hygiene"]["bus_factor"], 1)
        self.assertEqual(report.metrics["hygiene"]["bus_factor_share"], 0.5)

    def test_wide_ownership_clears_the_bus_factor_rule(self):
        self.init_repo()
        extra = ("Alan Turing", "alan@example.com"), ("Katherine Johnson", "kj@example.com")
        for name, email in extra:
            git(self.tmp, "config", f"user.name.{email}", name)
        team = self.authors + extra
        for index in range(8):
            self.commit(f"feat: shared {index}",
                        {"a.py": python_file(1, lines_per_func=index + 1)},
                        author=team[index % 4], days_ago=index)
        report = self.audit()
        self.assertNotIn("hygiene.bus_factor", self.rule_ids(report))
        self.assertEqual(report.metrics["hygiene"]["contributors"], 4)
        # 2 of 4 authors cover the first half of the commits.
        self.assertEqual(report.metrics["hygiene"]["bus_factor"], 2)

    def test_dominant_author_triggers_bus_factor_finding(self):
        self.init_repo()
        for index in range(12):
            self.commit(f"feat: mostly mine {index}",
                        {"a.py": python_file(1, lines_per_func=index + 1)},
                        author=self.authors[0], days_ago=index)
        for index in range(2):
            self.commit(f"fix: rare touch {index}",
                        {"b.py": python_file(1, lines_per_func=index + 1)},
                        author=self.authors[1], days_ago=index)
        report = self.audit()
        self.assertIn("hygiene.bus_factor", self.rule_ids(report))
        metrics = report.metrics["hygiene"]
        # 12 of 14 commits: one author is well past the 80% share threshold.
        self.assertGreater(metrics["bus_factor_share"], 0.8)

    def test_bus_factor_metric_counts_authors_covering_half(self):
        self.init_repo()
        for index in range(6):
            self.commit(f"feat: split {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        author=self.authors[index % 2], days_ago=index)
        report = self.audit()
        self.assertEqual(report.metrics["hygiene"]["bus_factor"], 1)


class StalenessTest(RepoCase):
    def test_recent_commit_is_not_dormant(self):
        self.init_repo()
        self.commit("feat: just now", {"a.py": python_file(1)})
        report = self.audit()
        self.assertNotIn("hygiene.dormant", self.rule_ids(report))
        self.assertLess(report.metrics["hygiene"]["days_since_last_commit"], 1.0)

    def test_stale_commit_is_dormant_and_severe(self):
        # Dormancy is read off the whole history, not the 180-day window:
        # a repo whose newest commit predates the window is exactly the dead
        # repo this rule is for, and it would otherwise report nothing.
        self.init_repo()
        self.commit("feat: long ago", {"a.py": python_file(1)}, days_ago=400)
        report = self.audit()
        self.assertIn("hygiene.dormant", self.rule_ids(report))
        finding = self.findings_for(report, "hygiene.dormant")[0]
        # 400 days is more than 3x the 90-day dormancy threshold.
        self.assertEqual(finding.severity.value, "high")
        self.assertGreater(report.metrics["hygiene"]["days_since_last_commit"], 399)

    def test_long_gap_between_commits_is_a_dry_spell(self):
        self.init_repo()
        self.commit("feat: start", {"a.py": python_file(1)}, days_ago=200)
        self.commit("feat: after a long silence", {"a.py": python_file(2)}, days_ago=10)
        report = self.audit(overrides={"history_window_days": 365})
        self.assertIn("hygiene.long_dry_spell", self.rule_ids(report))
        self.assertGreater(report.metrics["hygiene"]["longest_dry_spell_days"], 150)

    def test_steady_commits_have_no_dry_spell(self):
        self.init_repo()
        for index in range(10):
            self.commit(f"feat: steady {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        days_ago=index * 5)
        report = self.audit()
        self.assertNotIn("hygiene.long_dry_spell", self.rule_ids(report))

    def test_stale_branch_is_reported(self):
        self.init_repo()
        self.commit("feat: baseline", {"a.py": python_file(1)})
        self.branch_off("feature/old", days_ago=200)
        self.commit("feat: current work", {"a.py": python_file(2)})
        report = self.audit(overrides={"dormant_branch_days": 90})
        branches = report.metrics["hygiene"]["long_lived_branches"]
        self.assertEqual([b["branch"] for b in branches], ["feature/old"])
        self.assertGreater(branches[0]["days"], 150)
        self.assertIn("hygiene.long_lived_branches", self.rule_ids(report))

    def test_fresh_branch_is_not_reported(self):
        self.init_repo()
        self.commit("feat: baseline", {"a.py": python_file(1)})
        self.branch_off("feature/new", days_ago=1)
        report = self.audit(overrides={"dormant_branch_days": 90})
        self.assertEqual(report.metrics["hygiene"]["long_lived_branches"], [])


class HistoryUnavailableTest(RepoCase):
    def test_no_git_marks_history_signals_unavailable(self):
        # A plain directory: no .git at all.
        self.write("a.py", python_file(1))
        report = self.audit(use_git=True)
        self.assertFalse(report.repo.is_git_repo)
        for key in ("commit_cadence", "bus_factor", "commit_convention"):
            self.assertIn(key, report.unavailable)
        self.assertIn("hygiene.history_unavailable", self.rule_ids(report))
        # Filesystem hygiene still works, so the dimension is scored.
        self.assertIsNotNone(report.scorecard.get(Dimension.HYGIENE).score)

    def test_no_git_leaves_hotspots_unscored(self):
        self.write("a.py", python_file(1))
        report = self.audit(use_git=True)
        self.assertIsNone(report.scorecard.get(Dimension.HOTSPOTS).score)

    def test_no_git_flag_is_honoured_in_a_real_repo(self):
        self.init_repo()
        self.commit("feat: ignored", {"a.py": python_file(1)})
        report = self.audit(use_git=False)
        self.assertFalse(report.repo.is_git_repo)
        self.assertIn("hygiene.history_unavailable", self.rule_ids(report))

    def test_empty_history_keeps_hygiene_available(self):
        self.init_repo()
        self.write("a.py", python_file(1))
        report = self.audit()
        self.assertTrue(report.repo.is_git_repo)
        # No commits at all: cadence is unknown, but that is not fatal.
        self.assertIn("commit_cadence", report.unavailable)
        self.assertIsNotNone(report.scorecard.get(Dimension.HYGIENE).score)


class ConventionTest(TempDirCase):
    """Missing project files: one finding per expectation, not per spelling."""

    def test_each_missing_convention_is_reported_once(self):
        self.write("a.py", python_file(1))
        report = self.audit()
        missing = report.metrics["hygiene"]["missing_conventions"]
        # LICENSE, LICENSE.md and LICENSE.txt are one expectation, not three.
        self.assertEqual(len(missing), len(set(missing)))
        self.assertEqual(missing.count("a LICENSE"), 1)
        self.assertEqual(missing.count("CODEOWNERS"), 1)

    def test_any_accepted_spelling_satisfies_the_check(self):
        self.write("a.py", python_file(1))
        self.write("LICENSE.md", "MIT\n")
        self.write("CODEOWNERS", "* @team\n")
        report = self.audit()
        missing = report.metrics["hygiene"]["missing_conventions"]
        self.assertNotIn("a LICENSE", missing)
        self.assertNotIn("CODEOWNERS", missing)

    def test_nested_codeowners_is_found(self):
        self.write("a.py", python_file(1))
        self.write(".github/CODEOWNERS", "* @team\n")
        report = self.audit()
        self.assertNotIn("CODEOWNERS", report.metrics["hygiene"]["missing_conventions"])

    def test_present_files_produce_no_missing_file_findings(self):
        self.write("a.py", python_file(1))
        self.write("README.md", "# Demo\n" + "text " * 100)
        self.write("LICENSE", "MIT\n")
        self.write("CONTRIBUTING.md", "# Contributing\n")
        self.write("CODEOWNERS", "* @team\n")
        self.write(".editorconfig", "root = true\n")
        self.write(".gitignore", "*.pyc\n")
        self.write(".github/workflows/ci.yml", "name: ci\n")
        report = self.audit()
        ids = self.rule_ids(report)
        self.assertNotIn("hygiene.missing_file", ids)
        self.assertNotIn("hygiene.no_readme", ids)
        self.assertNotIn("hygiene.no_ci", ids)
        self.assertEqual(report.metrics["hygiene"]["missing_conventions"], [])

    def test_stub_readme_is_flagged(self):
        self.write("a.py", python_file(1))
        self.write("README.md", "# Demo\n")
        report = self.audit()
        self.assertIn("hygiene.no_readme", self.rule_ids(report))

    def test_missing_file_detail_reads_as_a_sentence(self):
        self.write("a.py", python_file(1))
        report = self.audit()
        for finding in self.findings_for(report, "hygiene.missing_file"):
            self.assertTrue(finding.detail.startswith("Missing "), finding.detail)
            self.assertNotIn("No a ", finding.detail)


class CommitConventionTest(RepoCase):
    def test_conventional_history_passes(self):
        self.init_repo()
        for index in range(5):
            self.commit(f"feat(api): add thing {index} [ABC-{index + 1}]",
                        {"a.py": python_file(1, lines_per_func=index + 1)}, days_ago=index)
        report = self.audit()
        self.assertNotIn("hygiene.conventional_commits", self.rule_ids(report))
        self.assertEqual(report.metrics["hygiene"]["conventional_commit_ratio"], 100.0)
        self.assertEqual(report.metrics["hygiene"]["ticket_reference_ratio"], 100.0)

    def test_freeform_history_fails(self):
        self.init_repo()
        subjects = [
            f"updated some stuff number {index} and maybe a few other things"
            for index in range(5)
        ]
        for index, subject in enumerate(subjects):
            self.commit(subject, {"a.py": python_file(1, lines_per_func=index + 1)},
                        days_ago=index)
        report = self.audit()
        self.assertIn("hygiene.conventional_commits", self.rule_ids(report))
        self.assertEqual(report.metrics["hygiene"]["conventional_commit_ratio"], 0.0)
        self.assertEqual(report.metrics["hygiene"]["ticket_reference_ratio"], 0.0)
        expected = round(sum(len(s) for s in subjects) / len(subjects), 1)
        self.assertEqual(report.metrics["hygiene"]["avg_subject_length"], expected)

    def test_breaking_change_syntax_is_conventional(self):
        self.init_repo()
        for index in range(5):
            self.commit(f"feat(api)!: drop v1 {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        days_ago=index)
        report = self.audit()
        self.assertEqual(report.metrics["hygiene"]["conventional_commit_ratio"], 100.0)


class MergeRatioTest(RepoCase):
    # Every case needs at least 20 commits, because the rule is deliberately
    # silent on short histories where the ratio is just noise. Each merged
    # topic branch costs two commits (the topic commit plus the merge), so the
    # largest fixture here is deliberately kept small.
    MIN_COMMITS_FOR_RULE = 20

    def test_linear_history_flagged_only_with_enough_commits(self):
        self.init_repo()
        for index in range(5):  # below the 20-commit threshold
            self.commit(f"feat: linear {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        days_ago=index)
        report = self.audit()
        self.assertNotIn("hygiene.merge_ratio", self.rule_ids(report))
        self.assertEqual(report.metrics["hygiene"]["merge_ratio"], 0.0)

    def test_fully_linear_history_over_threshold_is_flagged(self):
        self.init_repo()
        for index in range(22):
            self.commit(f"feat: linear {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        days_ago=index)
        report = self.audit()
        self.assertIn("hygiene.merge_ratio", self.rule_ids(report))
        finding = self.findings_for(report, "hygiene.merge_ratio")[0]
        # A merge ratio is a weak proxy; it should say so.
        self.assertEqual(finding.confidence.value, "heuristic")

    def test_balanced_merge_ratio_is_not_flagged(self):
        self.init_repo()
        for index in range(18):
            self.commit(f"feat: base {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        days_ago=index)
        for index in range(2):  # 2 merges of 22 commits = 9%, inside the band
            self.merge_branch(f"topic/{index}", days_ago=index + 1,
                              files={f"m{index}.py": python_file(1)})
        report = self.audit()
        self.assertNotIn("hygiene.merge_ratio", self.rule_ids(report))
        self.assertGreater(report.metrics["hygiene"]["merge_ratio"], 0.05)
        self.assertLess(report.metrics["hygiene"]["merge_ratio"], 0.45)

    def test_merge_heavy_history_is_flagged(self):
        # A merge commit always implies at least one non-merge commit on the
        # merged side, so 0.5 is the hard ceiling on this ratio. Only a repo
        # that --no-ff merges literally every branch gets near it.
        self.init_repo()
        for index in range(2):
            self.commit(f"feat: base {index}", {"a.py": python_file(1, lines_per_func=index + 1)},
                        days_ago=index)
        for index in range(10):  # 10 merges of 22 commits = 45.5%, over the cap
            self.merge_branch(f"topic/{index}", days_ago=index + 1,
                              files={f"m{index}.py": python_file(1)})
        report = self.audit()
        self.assertAlmostEqual(report.metrics["hygiene"]["merge_ratio"], 0.455, places=3)
        self.assertIn("hygiene.merge_ratio", self.rule_ids(report))
        finding = self.findings_for(report, "hygiene.merge_ratio")[0]
        # A merge ratio is a weak proxy for review workflow shape; it must
        # not be presented as a confident measurement.
        self.assertEqual(finding.confidence.value, "heuristic")

    def test_merge_ratio_ceiling_is_not_above_one_half(self):
        # Guards the reasoning behind the default max_merge_ratio: if this
        # ever changes, the threshold needs to change with it.
        self.assertLessEqual(cfg.DEFAULTS["max_merge_ratio"], 0.5)


if __name__ == "__main__":
    unittest.main()
