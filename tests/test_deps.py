"""Dependency manifest parsing, version handling, and OSV name mapping."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repodebt.analyzers import base  # noqa: E402
from repodebt.analyzers import deps as dp  # noqa: E402
from repodebt.analyzers import osv  # noqa: E402
from repodebt.model import Severity  # noqa: E402

from fixtures import TempDirCase  # noqa: E402


class VersionTest(unittest.TestCase):
    def test_extract_pypi(self):
        cases = {
            "==1.2.3": "1.2.3",
            ">=1.0,<2.0": "1.0",
            "~=2.1": "2.1",
            "^1.0.0": "1.0.0",
            "==1.0; python_version>'3.8'": "1.0",
            "1.2.3": "1.2.3",
        }
        for spec, expected in cases.items():
            self.assertEqual(dp.extract_version(spec, "python"), expected, spec)

    def test_extract_npm(self):
        self.assertEqual(dp.extract_version("^4.17.21", "npm"), "4.17.21")
        self.assertEqual(dp.extract_version("~1.2.3", "npm"), "1.2.3")
        self.assertEqual(dp.extract_version(">=2.0.0 <3.0.0", "npm"), "2.0.0")

    def test_extract_gomod(self):
        self.assertEqual(dp.extract_version("v1.21.0", "gomod"), "1.21.0")

    def test_extract_maven(self):
        self.assertEqual(dp.extract_version("${junit.version}", "maven"), "junit.version")

    def test_pinned_exact(self):
        self.assertTrue(dp.is_pinned_exact("1.2.3", "npm"))
        self.assertTrue(dp.is_pinned_exact("==1.2.3", "python"))
        self.assertTrue(dp.is_pinned_exact("===1.2.3", "python"))
        self.assertTrue(dp.is_pinned_exact("v1.2.3", "gomod"))
        self.assertFalse(dp.is_pinned_exact("^1.2.3", "npm"))
        self.assertFalse(dp.is_pinned_exact(">=1.2.3", "python"))
        self.assertFalse(dp.is_pinned_exact("~1.2.3", "npm"))
        self.assertFalse(dp.is_pinned_exact("*", "npm"))
        self.assertFalse(dp.is_pinned_exact("latest", "python"))

    def test_is_range(self):
        self.assertTrue(dp.is_range("^1.0.0", "npm"))
        self.assertFalse(dp.is_range("1.0.0", "npm"))
        self.assertFalse(dp.is_range("", "npm"))

    def test_version_tuple_ordering(self):
        self.assertLess(dp.version_tuple("1.2.3"), dp.version_tuple("1.10.0"))
        self.assertEqual(dp.version_tuple(None), ())


class PackageJsonTest(unittest.TestCase):
    def test_splits_dependency_groups(self):
        text = """{
  "dependencies": {"react": "18.2.0", "lodash": "^4.17.21"},
  "devDependencies": {"jest": "29.7.0", "typescript": "~5.3.0"},
  "optionalDependencies": {"fsevents": "2.3.3"},
  "peerDependencies": {"react-dom": ">=18.0.0"}
}"""
        manifest = dp.parse_package_json(text, "package.json")
        self.assertEqual(manifest.error, "")
        groups = {d.name: d.group for d in manifest.dependencies}
        self.assertEqual(groups["react"], dp.PROD)
        self.assertEqual(groups["jest"], dp.DEV)
        self.assertEqual(groups["fsevents"], dp.OPTIONAL)
        self.assertEqual(groups["react-dom"], dp.PEER)

    def test_pinned_flags(self):
        text = '{"dependencies": {"a": "1.0.0", "b": "^2.0.0"}}'
        manifest = dp.parse_package_json(text, "package.json")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertTrue(by_name["a"].pinned_exact)
        self.assertFalse(by_name["b"].pinned_exact)

    def test_invalid_json_reports_error(self):
        manifest = dp.parse_package_json("{ not json", "package.json")
        self.assertTrue(manifest.error)
        self.assertEqual(manifest.dependencies, [])


class PyprojectTest(unittest.TestCase):
    def test_standard_project_metadata(self):
        text = """
[project]
name = "x"
dependencies = ["requests>=2.0", "flask==3.0.0", "uvicorn[standard]>=0.20"]

[project.optional-dependencies]
test = ["pytest>=7"]

[build-system]
requires = ["setuptools>=68"]
"""
        manifest = dp.parse_pyproject(text, "pyproject.toml")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["requests"].group, dp.PROD)
        self.assertEqual(by_name["flask"].spec, "==3.0.0")
        self.assertTrue(by_name["flask"].pinned_exact)
        self.assertEqual(by_name["uvicorn"].spec, "[standard]>=0.20")
        self.assertEqual(by_name["pytest"].group, dp.OPTIONAL)
        self.assertEqual(by_name["setuptools"].group, dp.DEV)

    def test_dependency_groups(self):
        text = """
[dependency-groups]
dev = ["ruff>=0.1", "mypy>=1.8"]
"""
        manifest = dp.parse_pyproject(text, "pyproject.toml")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["ruff"].group, dp.DEV)
        self.assertEqual(by_name["mypy"].group, dp.DEV)

    def test_poetry_style(self):
        text = """
[tool.poetry.dependencies]
python = "^3.11"
requests = "^2.31.0"

[tool.poetry.dev-dependencies]
pytest = "^7.4"
"""
        manifest = dp.parse_pyproject(text, "pyproject.toml")
        names = {d.name for d in manifest.dependencies}
        self.assertNotIn("python", names)
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["requests"].group, dp.PROD)
        self.assertEqual(by_name["pytest"].group, dp.DEV)

    def test_invalid_toml_reports_error(self):
        manifest = dp.parse_pyproject("[[[bad", "pyproject.toml")
        self.assertTrue(manifest.error)


class RequirementsTest(unittest.TestCase):
    def test_parses_pins_comments_and_extras(self):
        text = (
            "# a comment\n"
            "requests==2.31.0  # pinned\n"
            "flask>=3.0\n"
            "uvicorn[standard]==0.27.0\n"
            "celery\n"
            "-r other.txt\n"
            "--index-url https://example.com\n"
            "pkg @ https://example.com/pkg.whl\n"
        )
        manifest = dp.parse_requirements(text, "requirements.txt")
        names = [d.name for d in manifest.dependencies]
        self.assertIn("requests", names)
        self.assertIn("flask", names)
        self.assertIn("uvicorn", names)
        self.assertIn("celery", names)
        self.assertNotIn("-r", names)
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["requests"].spec, "==2.31.0")
        # Extras belong to the name, not the version specifier.
        self.assertEqual(by_name["uvicorn"].spec, "==0.27.0")
        self.assertTrue(by_name["uvicorn"].pinned_exact)
        self.assertFalse(by_name["flask"].pinned_exact)

    def test_plain_requirements_file_is_production(self):
        manifest = dp.parse_requirements("flask==2.0.0\n", "requirements.txt")
        self.assertEqual([d.group for d in manifest.dependencies], [dp.PROD])

    def test_dev_flavoured_filenames_are_not_counted_as_production(self):
        """Test and tooling pins must not be reported as shipped dependencies.

        Otherwise the production dependency count overstates what a release
        installs, and an online advisory lookup queries packages the project
        never ships.
        """
        dev_names = [
            "requirements-dev.txt",
            "requirements_dev.txt",
            "dev-requirements.txt",
            "requirements-test.txt",
            "requirements/tests.txt",
            "requirements/dev.txt",
            "requirements/testing.txt",
        ]
        for path in dev_names:
            with self.subTest(path=path):
                manifest = dp.parse_requirements("pytest==7.4.4\n", path)
                self.assertEqual([d.group for d in manifest.dependencies], [dp.DEV], path)

    def test_unmarked_requirements_files_stay_production(self):
        for path in ("requirements.txt", "requirements/base.txt", "requirements-prod.txt"):
            with self.subTest(path=path):
                manifest = dp.parse_requirements("flask==2.0.0\n", path)
                self.assertEqual([d.group for d in manifest.dependencies], [dp.PROD], path)

    def test_explicit_group_argument_still_wins(self):
        manifest = dp.parse_requirements("flask==2.0.0\n", "requirements-dev.txt", dp.PROD)
        self.assertEqual([d.group for d in manifest.dependencies], [dp.PROD])


@dataclass
class FakeDep:
    """Minimal stand-in for ``deps.DeclaredDependency``."""

    name: str
    ecosystem: str
    version: str | None = "1.0.0"
    group: str = dp.PROD
    manifest: str = "requirements.txt"
    line: int | None = 1


class OsvEcosystemTest(unittest.TestCase):
    """The internal label and the OSV ecosystem string are different schemes.

    These tests exist because looking an internal label up in a map keyed by
    OSV names returns ``None`` for every ecosystem whose spelling differs --
    which is all of them except npm. That made ``--online`` silently query
    nothing and report "no queryable production dependencies".
    """

    def test_every_label_reps_an_osv_ecosystem(self):
        for label in ("python", "npm", "cargo", "gomod", "maven", "gradle",
                      "composer", "bundler", "pub"):
            self.assertIsNotNone(osv._osv_ecosystem(FakeDep("x", label)), label)

    def test_label_to_osv_ecosystem(self):
        expected = {
            "python": "PyPI",
            "npm": "npm",
            "cargo": "crates.io",
            "gomod": "Go",
            "maven": "Maven",
            "composer": "Packagist",
            "bundler": "RubyGems",
            "pub": "Pub",
        }
        for label, ecosystem in expected.items():
            self.assertEqual(osv._osv_ecosystem(FakeDep("x", label)), ecosystem)

    def test_gradle_coordinates_are_maven_coordinates(self):
        self.assertEqual(osv._osv_ecosystem(FakeDep("g:a", "gradle")), "Maven")

    def test_unknown_ecosystem_is_not_queryable(self):
        self.assertIsNone(osv._osv_ecosystem(FakeDep("x", "unknown")))

    def test_lookup_direction_is_internal_to_osv(self):
        # Guards against reintroducing the inverted map.
        for label, ecosystem in osv.OSV_ECOSYSTEM_BY_LABEL.items():
            self.assertEqual(osv._osv_ecosystem(FakeDep("x", label)), ecosystem)


class OsvPackageNameTest(unittest.TestCase):
    def test_python_is_pep503_normalised(self):
        self.assertEqual(osv._osv_package_name(FakeDep("Django_Foo", "python")), "django-foo")
        self.assertEqual(osv._osv_package_name(FakeDep("zope.interface", "python")),
                         "zope-interface")

    def test_python_extras_are_stripped(self):
        # "uvicorn[standard]" is not a distribution name; the extras are a
        # property of the requirement, not of the package.
        self.assertEqual(osv._osv_package_name(FakeDep("uvicorn[standard]", "python")),
                         "uvicorn")

    def test_npm_scope_is_stripped(self):
        self.assertEqual(osv._osv_package_name(FakeDep("@babel/core", "npm")), "core")
        self.assertEqual(osv._osv_package_name(FakeDep("lodash", "npm")), "lodash")

    def test_maven_keeps_the_group(self):
        # Reducing "group:artifact" to "artifact" would miss every advisory.
        self.assertEqual(
            osv._osv_package_name(FakeDep("com.google.guava:guava", "maven")),
            "com.google.guava:guava",
        )

    def test_packagist_keeps_the_vendor(self):
        self.assertEqual(osv._osv_package_name(FakeDep("monolog/monolog", "composer")),
                         "monolog/monolog")

    def test_go_keeps_the_module_path(self):
        self.assertEqual(osv._osv_package_name(FakeDep("github.com/spf13/cobra", "gomod")),
                         "github.com/spf13/cobra")

    def test_other_ecosystems_pass_through(self):
        self.assertEqual(osv._osv_package_name(FakeDep("serde", "cargo")), "serde")
        self.assertEqual(osv._osv_package_name(FakeDep("rails", "bundler")), "rails")
        self.assertEqual(osv._osv_package_name(FakeDep("http", "pub")), "http")


class OsvEnrichmentTest(unittest.TestCase):
    """``enrich_dependencies`` must degrade, never raise, and never fabricate.

    The network is mocked, so these tests give the same answer whether or not
    the machine running them can reach api.osv.dev.
    """

    class _Ctx:
        def __init__(self, config):
            self.config = config

    def setUp(self):
        from repodebt import config as cfg

        # Point the cache at a scratch dir so a real run cannot read or write
        # a developer's ~/.cache/repodebt.
        data = dict(cfg.DEFAULTS)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        data["cache_dir"] = scratch.name
        self.config = cfg.Config(root=Path("."), data=data)

    def _run(self, deps_list):
        findings = []
        notes = osv.enrich_dependencies(
            self._Ctx(self.config), deps_list, findings, base.make_finding
        )
        return notes, findings

    def _seed_cache(self, dep, ids, details=None):
        osv._save_cache(osv._cache_file(self._Ctx(self.config), "vulns"),
                        {osv._query_key(dep): ids})
        if details is not None:
            osv._save_cache(osv._cache_file(self._Ctx(self.config), "detail"), details)

    def test_no_queryable_deps_reports_a_reason(self):
        notes, findings = self._run([])
        self.assertEqual(notes["queried"], 0)
        self.assertIn("message", notes)
        self.assertEqual(findings, [])

    def test_unpinned_dev_and_unknown_deps_are_not_queried(self):
        deps_list = [
            FakeDep("noversion", "python", version=None),
            FakeDep("devonly", "python", group=dp.DEV),
            FakeDep("weird", "unknown"),
        ]
        notes, _ = self._run(deps_list)
        self.assertEqual(notes["queried"], 0)

    def test_pinned_prod_deps_are_counted(self):
        deps_list = [FakeDep("requests", "python", "2.19.0"),
                     FakeDep("flask", "python", "0.12.0")]
        with patch.object(osv, "_post", return_value=None) as post, \
                patch.object(osv, "_get", return_value=None):
            notes, _ = self._run(deps_list)
        self.assertEqual(notes["queried"], 2)
        self.assertNotIn("message", notes)
        self.assertEqual(post.call_count, 2)

    def test_network_failure_is_recorded_not_raised(self):
        with patch.object(osv, "_post", return_value=None), \
                patch.object(osv, "_get", return_value=None):
            notes, findings = self._run([FakeDep("requests", "python", "2.19.0")])
        self.assertTrue(notes["degraded"])
        self.assertEqual(notes["errors"], ["OSV request failed for requests"])
        # A failed lookup must not invent a vulnerability.
        self.assertEqual(findings, [])

    def test_unrated_advisory_defaults_to_medium(self):
        with patch.object(osv, "_post", return_value={
            "results": [{"vulns": [{"id": "GHSA-xxxx-yyyy-zzzz"}]}]
        }), patch.object(osv, "_get", return_value={}):
            notes, findings = self._run([FakeDep("requests", "python", "2.19.0")])
        self.assertEqual(notes["affected"], 1)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "deps.vulnerability")
        self.assertEqual(findings[0].severity, Severity.MEDIUM)
        self.assertEqual(findings[0].occurrences, 1)

    def test_fixed_version_and_summary_reach_the_evidence(self):
        advisory = {
            "summary": "Path traversal in the static file handler.",
            "severity": [{"type": "CVSS_V3",
                          "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
            "affected": [{"ranges": [{"events": [{"fixed": "2.20.0"}]}]}],
        }
        with patch.object(osv, "_post", return_value={
            "results": [{"vulns": [{"id": "PYSEC-2020-1"}]}]
        }), patch.object(osv, "_get", return_value=advisory):
            _notes, findings = self._run([FakeDep("requests", "python", "2.19.0")])

        finding = findings[0]
        self.assertTrue(any("fixed in 2.20.0" in line for line in finding.evidence),
                        finding.evidence)
        self.assertTrue(any("Path traversal" in line for line in finding.evidence),
                        finding.evidence)
        # The CVSS vector scores 9.8, not the MEDIUM it used to fall back to.
        self.assertEqual(finding.severity, Severity.CRITICAL)
        self.assertEqual([loc.file for loc in finding.locations], ["requirements.txt"])

    def test_clean_package_produces_no_finding(self):
        with patch.object(osv, "_post", return_value={"results": [{"vulns": []}]}), \
                patch.object(osv, "_get", return_value={}):
            notes, findings = self._run([FakeDep("requests", "python", "2.31.0")])
        self.assertEqual(findings, [])
        self.assertEqual(notes["affected"], 0)
        self.assertEqual(notes["advisories"], 0)

    def test_query_payload_uses_the_mapped_ecosystem_and_name(self):
        def _query_for(dep):
            with patch.object(osv, "_post", return_value={
                "results": [{"vulns": [{"id": "GHSA-1"}]}]
            }) as post, patch.object(osv, "_get", return_value={}):
                self._run([dep])
            return post.call_args[0][1]["queries"][0]

        # An npm scope is not part of the package OSV knows it by.
        self.assertEqual(
            _query_for(FakeDep("@babel/core", "npm", "7.0.0"))["package"],
            {"name": "core", "ecosystem": "npm"},
        )
        # Dropping the Maven group would miss every advisory.
        self.assertEqual(
            _query_for(FakeDep("com.google.guava:guava", "maven", "31.0"))["package"],
            {"name": "com.google.guava:guava", "ecosystem": "Maven"},
        )
        self.assertEqual(
            _query_for(FakeDep("uvicorn[standard]", "python", "0.27.0"))["package"],
            {"name": "uvicorn", "ecosystem": "PyPI"},
        )

    def test_cache_short_circuits_the_network(self):
        """A cached answer must not re-query, and must survive a dead network."""
        self._seed_cache(
            FakeDep("requests", "python", "2.19.0"), ["GHSA-cached"],
            {"GHSA-cached": {
                "severity": [{"type": "CVSS_V3",
                              "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
            }},
        )
        with patch.object(osv, "_post", side_effect=AssertionError("no post")), \
                patch.object(osv, "_get", side_effect=AssertionError("no get")):
            notes, findings = self._run([FakeDep("requests", "python", "2.19.0")])

        self.assertEqual(notes["affected"], 1)
        self.assertEqual(findings[0].severity, Severity.CRITICAL)

    def test_a_cached_error_is_not_reported_as_a_vulnerability(self):
        self._seed_cache(FakeDep("requests", "python", "2.19.0"), ["__error__"])
        with patch.object(osv, "_post", side_effect=AssertionError("no post")), \
                patch.object(osv, "_get", side_effect=AssertionError("no get")):
            notes, findings = self._run([FakeDep("requests", "python", "2.19.0")])
        self.assertEqual(notes["affected"], 0)
        self.assertEqual(findings, [])

    def test_online_notes_separate_hits_from_fetches(self):
        """A count of network round-trips must not be labelled "cached"."""
        self._seed_cache(
            FakeDep("requests", "python", "2.19.0"), ["GHSA-cached"],
            {"GHSA-cached": {}},
        )
        with patch.object(osv, "_post", side_effect=AssertionError("no post")), \
                patch.object(osv, "_get", side_effect=AssertionError("no get")):
            notes, _ = self._run([FakeDep("requests", "python", "2.19.0")])
        self.assertEqual(notes["cache_hits"], 1)
        self.assertEqual(notes["fetched"], 0)
        self.assertNotIn("cached", notes)

    def test_cold_cache_reports_hits_as_zero_and_counts_fetches(self):
        with patch.object(osv, "_post", return_value={
            "results": [{"vulns": [{"id": "GHSA-1"}]}]
        }), patch.object(osv, "_get", return_value={}):
            notes, _ = self._run([FakeDep("requests", "python", "2.19.0")])
        self.assertEqual(notes["cache_hits"], 0)
        self.assertEqual(notes["fetched"], 1)


class MissingLockMessageTest(TempDirCase):
    def test_dependency_count_is_pluralised(self):
        self.write("build.gradle", "dependencies {\n"
                                   "    implementation 'com.google.guava:guava:31.0'\n}\n")
        findings = self.findings_for(self.audit(), "deps.missing_lock")
        self.assertEqual(len(findings), 1)
        self.assertIn("(1 dependency declared)", findings[0].detail)
        self.assertNotIn("1 dependencies", findings[0].detail)

    def test_several_dependencies_stay_plural(self):
        self.write("build.gradle",
                   "dependencies {\n"
                   "    implementation 'com.google.guava:guava:31.0'\n"
                   "    implementation 'org.slf4j:slf4j-api:1.7.36'\n}\n")
        findings = self.findings_for(self.audit(), "deps.missing_lock")
        self.assertEqual(len(findings), 1)
        self.assertIn("(2 dependencies declared)", findings[0].detail)


class GradleTest(unittest.TestCase):
    def test_short_notation_yields_a_queryable_version(self):
        # extract_version used to require a ":" or quote delimiter that the
        # parser had already stripped, so no Gradle dependency was ever
        # queryable and the whole Maven OSV path stayed dark.
        m = dp.parse_build_gradle(
            "dependencies {\n"
            "    implementation 'com.google.guava:guava:28.0-jre'\n"
            "}\n", "build.gradle")
        self.assertEqual(len(m.dependencies), 1)
        dep = m.dependencies[0]
        self.assertEqual(dep.name, "com.google.guava:guava")
        self.assertEqual(dep.group, dp.PROD)
        self.assertEqual(dp.extract_version(dep.spec, "gradle"), "28.0-jre")

    def test_map_notation_is_parsed(self):
        m = dp.parse_build_gradle(
            "dependencies {\n"
            "    api group: 'io.grpc', name: 'grpc-core', version: '1.58.0'\n"
            "}\n", "build.gradle")
        self.assertEqual(len(m.dependencies), 1)
        dep = m.dependencies[0]
        self.assertEqual(dep.name, "io.grpc:grpc-core")
        self.assertEqual(dp.extract_version(dep.spec, "gradle"), "1.58.0")

    def test_map_notation_tolerates_key_order_and_a_missing_group(self):
        m = dp.parse_build_gradle(
            "dependencies {\n"
            "    implementation name: 'guava', version: '31.0'\n"
            "    implementation version: '1.0', name: 'other', group: 'g'\n"
            "}\n", "build.gradle")
        names = {d.name for d in m.dependencies}
        self.assertEqual(names, {"guava", "g:other"})

    def test_declaration_without_a_version_is_skipped_not_guessed(self):
        m = dp.parse_build_gradle(
            "dependencies {\n"
            "    implementation name: 'guava'\n"
            "}\n", "build.gradle")
        self.assertEqual(m.dependencies, [])

    def test_test_configurations_are_dev(self):
        m = dp.parse_build_gradle(
            "dependencies {\n"
            "    testImplementation 'junit:junit:4.13.2'\n"
            "    androidTestImplementation 'androidx.test:core:1.4.0'\n"
            "    implementation 'com.google.guava:guava:31.0'\n"
            "}\n", "build.gradle")
        groups = {d.name: d.group for d in m.dependencies}
        self.assertEqual(groups["junit:junit"], dp.DEV)
        self.assertEqual(groups["androidx.test:core"], dp.DEV)
        self.assertEqual(groups["com.google.guava:guava"], dp.PROD)

    def test_gradle_version_shapes(self):
        for spec, expected in [
            ("28.0-jre", "28.0-jre"),
            ("1.0-SNAPSHOT", "1.0-SNAPSHOT"),
            ("3.12.0", "3.12.0"),
            ("[1.0,2.0)", "1.0"),
            ("${guavaVersion}", None),
        ]:
            with self.subTest(spec=spec):
                self.assertEqual(dp.extract_version(spec, "gradle"), expected)

    def test_dynamic_versions_are_not_treated_as_releases(self):
        # "2.+" names no concrete release, so there is nothing for OSV to match.
        for spec in ["2.+", "1.2.*", "latest.release", "${guavaVersion}"]:
            with self.subTest(spec=spec):
                self.assertIsNone(dp.extract_version(spec, "gradle"))


class QueryKeyTest(unittest.TestCase):
    """The cache key must survive coordinates that contain the separator."""

    def test_key_round_trips_a_maven_coordinate(self):
        # "Maven:com.google.guava:guava:31.0" split on ":" gives the name
        # "com.google.guava" and the version "guava:31.0" -- so a colon-delimited
        # key silently mis-addresses every Maven advisory.
        key = osv._query_key(FakeDep("com.google.guava:guava", "maven", "31.0"))
        self.assertEqual(json.loads(key), ["Maven", "com.google.guava:guava", "31.0"])

    def test_key_round_trips_a_packagist_package(self):
        key = osv._query_key(FakeDep("monolog/monolog", "composer", "2.0.0"))
        self.assertEqual(json.loads(key), ["Packagist", "monolog/monolog", "2.0.0"])

    def test_distinct_dependencies_get_distinct_keys(self):
        deps_list = [
            FakeDep("com.google.guava:guava", "maven", "31.0"),
            FakeDep("com.google.guava:failureaccess", "maven", "31.0"),
            FakeDep("guava", "npm", "31.0"),
            FakeDep("com.google.guava:guava", "maven", "32.0"),
        ]
        self.assertEqual(len({osv._query_key(d) for d in deps_list}), len(deps_list))

    def test_key_is_stable_across_calls(self):
        dep = FakeDep("requests", "python", "2.19.0")
        self.assertEqual(osv._query_key(dep), osv._query_key(dep))


class CvssBaseScoreTest(unittest.TestCase):
    """OSV carries CVSS *vectors*, so the base score has to be computed.

    Taking the last "/"-segment and reading digits off it does not score a
    vector -- "A:H" is not a number -- so every rated advisory silently fell
    through to the unrated default. Expected values below are the published
    CVSS v3.1 base scores; the two spec worked examples are cited as such.
    """

    CASES = [
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", 7.5),
        ("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 8.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),   # spec 7.1.4
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:L", 7.3),   # spec 7.1.3
        ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.8),
        ("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:H/I:H/A:H", 6.3),
        ("CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L", 4.0),
        ("CVSS:3.1/AV:A/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N", 5.7),
        ("CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N", 0.0),
        ("CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),   # v3.0 too
    ]

    def test_published_base_scores(self):
        for vector, expected in self.CASES:
            with self.subTest(vector=vector):
                self.assertEqual(osv._cvss3_base_score(vector), expected)

    def test_unparseable_input_yields_none(self):
        # Returning a number here would be worse than returning nothing.
        for bad in [
            "AV:N/AC:L/Au:N/C:P/I:P/A:P",     # CVSS v2, different scheme
            "not a vector",
            "",
            "CVSS:4.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            "CVSS:3.1/AV:Q/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",   # unknown value
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H",      # missing metric
        ]:
            with self.subTest(bad=bad):
                self.assertIsNone(osv._cvss3_base_score(bad))

    def test_roundup_is_not_plain_rounding(self):
        # The spec's Roundup is a ceiling to one decimal, with an exactness
        # carve-out: a value that is already a clean tenth stays put, so 4.9
        # must not be bumped to 5.0 the way math.ceil would.
        self.assertEqual(osv._cvss3_roundup(4.0), 4.0)
        self.assertEqual(osv._cvss3_roundup(4.9), 4.9)
        self.assertEqual(osv._cvss3_roundup(0.0), 0.0)
        # Anything above a clean tenth rounds up to the next one.
        self.assertEqual(osv._cvss3_roundup(4.00001), 4.1)
        self.assertEqual(osv._cvss3_roundup(4.91), 5.0)
        self.assertEqual(osv._cvss3_roundup(7.48224), 7.5)


class SeverityForAdvisoryTest(unittest.TestCase):
    def _severity(self, advisory):
        return osv._severity_for(advisory)

    def test_unrated_advisory_defaults_to_medium(self):
        self.assertEqual(self._severity({}), Severity.MEDIUM)

    def test_alias_without_rating_is_high(self):
        # A CVE alias means the flaw is real, but says nothing about
        # exploitability, so it must not outrank a measured critical.
        self.assertEqual(self._severity({"aliases": ["CVE-2020-1"]}), Severity.HIGH)

    def test_database_specific_critical_wins(self):
        self.assertEqual(
            self._severity({
                "database_specific": {"severity": "CRITICAL"},
                "severity": [{"type": "CVSS_V3",
                              "score": "CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N"}],
            }),
            Severity.CRITICAL,
        )

    def test_vector_to_severity_bands(self):
        cases = [
            ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", Severity.CRITICAL),
            ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", Severity.HIGH),
            ("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:H/I:H/A:H", Severity.MEDIUM),
            ("CVSS:3.1/AV:L/AC:L/PR:H/UI:R/S:U/C:L/I:N/A:N", Severity.LOW),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(
                    self._severity({"severity": [{"type": "CVSS_V3", "score": score}]}),
                    expected,
                )

    def test_bare_number_scores_are_accepted(self):
        self.assertEqual(self._severity({"severity": [{"score": "7.5"}]}), Severity.HIGH)
        self.assertEqual(self._severity({"severity": [{"score": "9.1"}]}), Severity.CRITICAL)

    def test_v3_is_preferred_over_v2_regardless_of_order(self):
        advisory = {"severity": [
            {"type": "CVSS_V2", "score": "AV:N/AC:L/Au:N/C:P/I:P/A:P"},
            {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
        ]}
        self.assertEqual(self._severity(advisory), Severity.CRITICAL)

    def test_unusable_score_falls_through_to_the_alias_signal(self):
        advisory = {"aliases": ["CVE-2020-1"],
                    "severity": [{"type": "CVSS_V2", "score": "AV:N/AC:L/Au:N/C:P/I:P/A:P"}]}
        self.assertEqual(self._severity(advisory), Severity.HIGH)


class OtherEcosystemsTest(unittest.TestCase):
    def test_go_mod_block(self):
        text = """
module example.com/app

go 1.21

require (
    github.com/spf13/cobra v1.8.0
    github.com/stretchr/testify v1.8.4 // indirect
)

require github.com/single/one v0.1.0
"""
        manifest = dp.parse_go_mod(text, "go.mod")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(len(manifest.dependencies), 3)
        self.assertEqual(by_name["github.com/spf13/cobra"].spec, "v1.8.0")
        self.assertTrue(by_name["github.com/spf13/cobra"].pinned_exact)

    def test_cargo_toml(self):
        text = """
[package]
name = "x"

[dependencies]
serde = "1.0"
tokio = { version = "1.35", features = ["full"] }

[dev-dependencies]
criterion = "0.5"
"""
        manifest = dp.parse_cargo_toml(text, "Cargo.toml")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["serde"].group, dp.PROD)
        self.assertEqual(by_name["tokio"].spec, "1.35")
        self.assertEqual(by_name["criterion"].group, dp.DEV)

    def test_pom_xml(self):
        text = """<project>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.10.0</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>com.google.guava</groupId>
      <artifactId>guava</artifactId>
      <version>33.0.0-jre</version>
    </dependency>
  </dependencies>
</project>"""
        manifest = dp.parse_pom_xml(text, "pom.xml")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["com.google.guava:guava"].group, dp.PROD)
        self.assertEqual(by_name["org.junit.jupiter:junit-jupiter"].group, dp.DEV)
        self.assertEqual(manifest.error, "")

    def test_build_gradle(self):
        text = """
dependencies {
    implementation 'com.google.guava:guava:33.0.0-jre'
    testImplementation 'junit:junit:4.13.2'
}
"""
        manifest = dp.parse_build_gradle(text, "build.gradle")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["com.google.guava:guava"].group, dp.PROD)
        self.assertEqual(by_name["junit:junit"].group, dp.DEV)

    def test_gemfile(self):
        text = """
source 'https://rubygems.org'

gem 'rails', '~> 7.1.0'

group :test do
  gem 'rspec-rails', '6.1.0'
end
"""
        manifest = dp.parse_gemfile(text, "Gemfile")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["rails"].group, dp.PROD)
        self.assertEqual(by_name["rspec-rails"].group, dp.DEV)

    def test_composer(self):
        text = """{
  "require": {"php": "^8.2", "monolog/monolog": "^3.0"},
  "require-dev": {"phpunit/phpunit": "^10.0"}
}"""
        manifest = dp.parse_composer_json(text, "composer.json")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertNotIn("php", by_name)
        self.assertEqual(by_name["monolog/monolog"].group, dp.PROD)
        self.assertEqual(by_name["phpunit/phpunit"].group, dp.DEV)

    def test_pubspec(self):
        text = """
name: app
dependencies:
  http: ^1.1.0
  path: ^1.8.0
dev_dependencies:
  test: ^1.24.0
"""
        manifest = dp.parse_pubspec_yaml(text, "pubspec.yaml")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["http"].group, dp.PROD)
        self.assertEqual(by_name["test"].group, dp.DEV)

    def test_pipfile(self):
        text = """
[packages]
requests = "==2.31.0"
flask = "*"

[dev-packages]
pytest = ">=7"
"""
        manifest = dp.parse_pipfile(text, "Pipfile")
        by_name = {d.name: d for d in manifest.dependencies}
        self.assertEqual(by_name["requests"].group, dp.PROD)
        self.assertEqual(by_name["pytest"].group, dp.DEV)
        self.assertFalse(by_name["flask"].pinned_exact)


class HelpersTest(unittest.TestCase):
    def test_skew_key_normalises_names(self):
        prod = dp.DeclaredDependency("Django_Rest", "1.0", "python", "a.txt")
        self.assertEqual(dp._skew_key(prod), "django-rest")
        maven = dp.DeclaredDependency("org.x:guava", "1.0", "maven", "pom.xml")
        self.assertEqual(dp._skew_key(maven), "guava")
        npm = dp.DeclaredDependency("@scope/pkg", "1.0", "npm", "package.json")
        self.assertEqual(dp._skew_key(npm), "pkg")

    def test_test_only_detection(self):
        self.assertTrue(dp._looks_like_test_only("pytest"))
        self.assertTrue(dp._looks_like_test_only("jest"))
        self.assertTrue(dp._looks_like_test_only("ts-jest"))
        self.assertFalse(dp._looks_like_test_only("requests"))
        self.assertFalse(dp._looks_like_test_only("express"))

    def test_lockfile_pairing(self):
        files = {"package.json", "package-lock.json", "src/app.js"}
        self.assertEqual(dp._lockfile_for("npm", files), "package-lock.json")
        files = {"package.json", "yarn.lock"}
        self.assertEqual(dp._lockfile_for("npm", files), "yarn.lock")
        self.assertIsNone(dp._lockfile_for("cargo", files))

    def test_lockfile_found_in_subdirectory(self):
        files = {"frontend/package.json", "frontend/pnpm-lock.yaml"}
        self.assertEqual(dp._lockfile_for("npm", files), "frontend/pnpm-lock.yaml")


if __name__ == "__main__":
    unittest.main()
