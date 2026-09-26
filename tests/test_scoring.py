"""Scoring, grading, capping, and baseline comparison."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt import config as cfg  # noqa: E402
from repodebt.model import (  # noqa: E402
    Confidence, Dimension, Finding, Location, Severity, grade_for,
)
from repodebt.render import json_out  # noqa: E402
from repodebt.scoring import cap_by_rule, fold_occurrences, score_findings  # noqa: E402

from fixtures import TempDirCase  # noqa: E402


def finding(rule_id: str = "complexity.large_file", severity=None, confidence=None,
            occurrences: int = 1, rel: str = "a.py") -> Finding:
    from repodebt.rules import RULES

    rule = RULES[rule_id]
    return Finding(
        id=rule.id,
        dimension=rule.dimension,
        severity=severity or rule.severity,
        confidence=confidence or Confidence.HIGH,
        title=rule.title,
        locations=[Location(rel)],
        occurrences=occurrences,
    )


class PenaltyTest(unittest.TestCase):
    def test_penalty_scales_with_severity(self):
        low = finding(severity=Severity.LOW)
        high = finding(severity=Severity.HIGH)
        critical = finding(severity=Severity.CRITICAL)
        self.assertLess(low.penalty, high.penalty)
        self.assertLess(high.penalty, critical.penalty)

    def test_confidence_discounts(self):
        exact = finding(severity=Severity.HIGH, confidence=Confidence.HIGH)
        heuristic = finding(severity=Severity.HIGH, confidence=Confidence.HEURISTIC)
        self.assertLess(heuristic.penalty, exact.penalty)


class GradeTest(unittest.TestCase):
    def setUp(self):
        self.bands = [(90.0, "A"), (80.0, "B"), (70.0, "C"), (60.0, "D"), (0.0, "F")]

    def test_boundaries(self):
        self.assertEqual(grade_for(100, self.bands), "A")
        self.assertEqual(grade_for(90, self.bands), "A")
        self.assertEqual(grade_for(89.9, self.bands), "B")
        self.assertEqual(grade_for(80, self.bands), "B")
        self.assertEqual(grade_for(70, self.bands), "C")
        self.assertEqual(grade_for(60, self.bands), "D")
        self.assertEqual(grade_for(59.9, self.bands), "F")
        self.assertEqual(grade_for(0, self.bands), "F")

    def test_none_score(self):
        self.assertIsNone(grade_for(None, self.bands))


class ScoreTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.config = cfg.load(self.tmp)

    def test_no_findings_scores_full(self):
        card = score_findings([], self.config, set(Dimension))
        self.assertEqual(card.overall, 100.0)
        self.assertEqual(card.grade, "A")

    def test_single_critical_drops_score(self):
        card = score_findings(
            [finding("deps.vulnerability")], self.config, set(Dimension)
        )
        self.assertLess(card.overall, 90.0)
        self.assertGreater(card.overall, 60.0)

    def test_score_saturates_at_zero(self):
        # 20 criticals in one dimension: 20 * 10 penalty against a budget of
        # 40 drives that dimension to the floor rather than going negative.
        many = [finding(severity=Severity.CRITICAL) for _ in range(20)]
        card = score_findings(many, self.config, set(Dimension))
        self.assertGreaterEqual(card.overall, 0.0)
        complexity = card.get(Dimension.COMPLEXITY)
        self.assertEqual(complexity.score, 0.0)
        self.assertEqual(complexity.penalty, 200.0)

    def test_unavailable_dimension_is_none_not_perfect(self):
        card = score_findings(
            [finding("complexity.large_file")],
            self.config,
            available={Dimension.COMPLEXITY},
            notes={Dimension.DEPS: "requires --online"},
        )
        deps = card.get(Dimension.DEPS)
        self.assertIsNone(deps.score)
        self.assertEqual(deps.note, "requires --online")

    def test_unavailable_dimensions_excluded_from_average(self):
        card = score_findings(
            [finding("complexity.large_file")],
            self.config,
            available={Dimension.COMPLEXITY},
        )
        # Only complexity contributes, so the overall equals that dimension.
        self.assertAlmostEqual(card.overall, card.get(Dimension.COMPLEXITY).score, places=6)

    def test_missing_dimensions_note_weight_coverage(self):
        card = score_findings([], self.config, available={Dimension.COMPLEXITY})
        note = card.get(Dimension.COMPLEXITY).note
        self.assertIn("weighted over", note)

    def test_heuristic_findings_penalised_less(self):
        # A heuristic finding is discounted (0.5x), so it drags the score down
        # less than an exact measurement of the same rule would.
        exact = score_findings(
            [finding(confidence=Confidence.HIGH)], self.config, set(Dimension)
        )
        guess = score_findings(
            [finding(confidence=Confidence.HEURISTIC)], self.config, set(Dimension)
        )
        self.assertLess(exact.overall, guess.overall)

    def test_weights_change_the_overall(self):
        heavy = cfg.load(self.tmp)
        heavy.data["weights"] = {"complexity": 1.0, "tests": 0.0, "hotspots": 0.0,
                                 "deps": 0.0, "hygiene": 0.0}
        card = score_findings([finding("complexity.large_file")], heavy, set(Dimension))
        self.assertAlmostEqual(card.overall, card.get(Dimension.COMPLEXITY).score, places=6)


class CriticalCapTest(TempDirCase):
    """A single critical must not average away into a top grade."""

    def _heavy_deps(self):
        """Config where deps is the only dimension carrying a penalty."""
        config = cfg.load(self.tmp)
        config.data["critical_grade_cap"] = 89.0
        return config

    def test_caps_otherwise_perfect_repo_below_a(self):
        # One critical in deps: deps alone drops to 66.7, but the weighted
        # average across five dimensions lands above the A band.
        config = self._heavy_deps()
        card = score_findings(
            [finding("deps.vulnerability", severity=Severity.CRITICAL)],
            config,
            set(Dimension),
        )
        self.assertEqual(card.overall, 89.0)
        self.assertEqual(card.grade, "B")
        self.assertIn("capped", card.note)

    def test_cap_explains_itself_on_the_owning_dimension(self):
        card = score_findings(
            [finding("deps.vulnerability", severity=Severity.CRITICAL)],
            self._heavy_deps(),
            set(Dimension),
        )
        deps = card.get(Dimension.DEPS)
        self.assertIn("deps.vulnerability", deps.note)
        # The cap is a reporting override only: the dimension still computed
        # its real score from its real penalty.
        self.assertAlmostEqual(deps.score, 66.7, places=1)

    def test_does_not_apply_without_criticals(self):
        card = score_findings(
            [finding("deps.vulnerability", severity=Severity.MEDIUM)],
            self._heavy_deps(),
            set(Dimension),
        )
        self.assertGreater(card.overall, 89.0)
        self.assertEqual(card.note, "")

    def test_does_not_raise_a_low_score(self):
        # Ten criticals genuinely tank complexity; the cap must not rescue it.
        config = self._heavy_deps()
        card = score_findings(
            [finding(severity=Severity.CRITICAL) for _ in range(10)],
            config,
            set(Dimension),
        )
        self.assertLess(card.overall, 89.0)
        self.assertEqual(card.note, "")

    def test_cap_can_be_disabled(self):
        config = cfg.load(self.tmp)
        config.data["critical_grade_cap"] = None
        card = score_findings(
            [finding("deps.vulnerability", severity=Severity.CRITICAL)],
            config,
            set(Dimension),
        )
        self.assertGreater(card.overall, 89.0)

    def test_no_score_no_cap(self):
        card = score_findings(
            [finding("deps.vulnerability", severity=Severity.CRITICAL)],
            self._heavy_deps(),
            available=set(),
        )
        self.assertIsNone(card.overall)
        self.assertIsNone(card.grade)


class CappingTest(unittest.TestCase):
    def test_caps_per_rule(self):
        findings = [finding() for _ in range(20)]
        kept, dropped = cap_by_rule(findings, cap=12)
        self.assertEqual(len(kept), 12)
        self.assertEqual(dropped["complexity.large_file"], 8)

    def test_cap_not_applied_across_rules(self):
        findings = [finding("complexity.large_file") for _ in range(5)]
        findings += [finding("complexity.long_function") for _ in range(5)]
        kept, dropped = cap_by_rule(findings, cap=3)
        self.assertEqual(len(kept), 6)
        self.assertEqual(sum(dropped.values()), 4)

    def test_occurrences_folded_into_last_finding_only(self):
        findings = [finding() for _ in range(20)]
        kept, dropped = cap_by_rule(findings, cap=12)
        fold_occurrences(kept, dropped)
        self.assertEqual(sum(f.occurrences for f in kept), 20)
        # Exactly one finding carries the folded remainder.
        self.assertEqual(sum(1 for f in kept if f.occurrences > 1), 1)

    def test_cap_of_zero_disables_capping(self):
        findings = [finding() for _ in range(20)]
        kept, dropped = cap_by_rule(findings, cap=0)
        self.assertEqual(len(kept), 20)
        self.assertEqual(dropped, {})

    def test_no_dropped_is_a_noop(self):
        findings = [finding()]
        self.assertEqual(fold_occurrences(findings, {}), findings)
        self.assertEqual(findings[0].occurrences, 1)


class BaselineTest(TempDirCase):
    def _report(self, findings, overall=80.0, digest="abc123"):
        from repodebt.model import Report, RepoInfo, Scorecard, DimensionScore

        dimensions = [
            DimensionScore(dimension=d, score=overall, penalty=0.0, finding_count=0, weight=0.2)
            for d in Dimension
        ]
        return Report(
            repo=RepoInfo(root=str(self.tmp), name="x", is_git_repo=False),
            scorecard=Scorecard(overall=overall, grade="B", dimensions=dimensions),
            metrics={},
            findings=findings,
            config_digest=digest,
        )

    def test_identical_reports_have_no_regressions(self):
        report = self._report([finding()])
        baseline = report.to_dict()
        diff = json_out.compare(report, baseline)
        self.assertEqual(diff["regressions"], [])
        self.assertEqual(diff["summary"]["new"], 0)

    def test_new_finding_is_a_regression(self):
        report = self._report([finding()])
        baseline = self._report([]).to_dict()
        diff = json_out.compare(report, baseline)
        self.assertEqual(diff["summary"]["new"], 1)
        self.assertEqual(diff["regressions"][0]["kind"], "new")

    def test_resolved_finding_is_an_improvement_not_a_regression(self):
        report = self._report([])
        baseline = self._report([finding()]).to_dict()
        diff = json_out.compare(report, baseline)
        self.assertEqual(diff["regressions"], [])
        self.assertEqual(diff["summary"]["fixed"], 1)

    def test_increased_occurrences_is_a_regression(self):
        report = self._report([finding(occurrences=5)])
        baseline = self._report([finding(occurrences=2)]).to_dict()
        diff = json_out.compare(report, baseline)
        self.assertEqual(diff["summary"]["increased"], 1)

    def test_decreased_occurrences_is_an_improvement(self):
        report = self._report([finding(occurrences=1)])
        baseline = self._report([finding(occurrences=5)]).to_dict()
        diff = json_out.compare(report, baseline)
        self.assertEqual(diff["regressions"], [])
        self.assertTrue(any(i["kind"] == "decreased" for i in diff["improvements"]))

    def test_score_drop_beyond_threshold(self):
        report = self._report([], overall=70.0)
        baseline = self._report([], overall=90.0).to_dict()
        diff = json_out.compare(report, baseline, threshold=5.0)
        self.assertTrue(any(r["kind"] == "score_drop" for r in diff["regressions"]))
        self.assertEqual(diff["score"]["delta"], -20.0)

    def test_score_drop_within_threshold_is_ignored(self):
        report = self._report([], overall=88.0)
        baseline = self._report([], overall=90.0).to_dict()
        diff = json_out.compare(report, baseline, threshold=5.0)
        self.assertEqual(diff["regressions"], [])

    def test_config_change_is_flagged(self):
        report = self._report([], digest="new")
        baseline = self._report([], digest="old").to_dict()
        diff = json_out.compare(report, baseline)
        self.assertTrue(diff["config_changed"])

    def test_dimension_deltas_reported(self):
        report = self._report([], overall=75.0)
        baseline = self._report([], overall=90.0).to_dict()
        diff = json_out.compare(report, baseline)
        self.assertTrue(diff["dimensions"])
        for change in diff["dimensions"].values():
            self.assertEqual(change["delta"], -15.0)


if __name__ == "__main__":
    unittest.main()
