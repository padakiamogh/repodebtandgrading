"""Command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import config as cfg
from .audit import run_audit
from .model import Report, Severity
from .render import console as console_render
from .render import json_out, markdown
from .rules import RULES

EXIT_OK = 0
EXIT_THRESHOLD = 1
EXIT_ERROR = 2

GRADES = ["A", "B", "C", "D", "F"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repodebt",
        description="Repository tech debt and health auditor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  repodebt                          audit the current directory
  repodebt ../myproject --markdown HEALTH.md --json health.json
  repodebt . --fail-on C            fail the build below grade C
  repodebt . --baseline health.json  fail only on regressions
  repodebt . --online               include OSV.dev advisories
  repodebt --list-rules             show every rule
  repodebt --explain hotspots.high_churn_complex
""",
    )
    parser.add_argument("path", nargs="?", default=".", help="repository to audit (default: .)")
    parser.add_argument("--version", action="version", version=f"repodebt {__version__}")

    output = parser.add_argument_group("output")
    output.add_argument("--json", metavar="FILE", help="write the machine-readable report")
    output.add_argument("--markdown", metavar="FILE", help="write a Markdown report")
    output.add_argument("--format", choices=["text", "json", "none"], default="text",
                        help="stdout format (default: text)")
    output.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    output.add_argument("-v", "--verbose", action="store_true",
                        help="show timings, metrics, and fix hints")

    gating = parser.add_argument_group("ci gating")
    gating.add_argument("--baseline", metavar="FILE",
                        help="compare against a previous report and fail only on regressions")
    gating.add_argument("--fail-on", metavar="TARGET",
                        help="exit 1 if the grade is worse than TARGET (A-F), or if any "
                             "finding at or above a severity (critical|high|medium|low) exists")
    gating.add_argument("--max-score-drop", type=float, default=0.0, metavar="N",
                        help="with --baseline, also fail if the score drops by more than N points")
    gating.add_argument("--write-baseline", metavar="FILE",
                        help="write the current report as a baseline and exit")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--online", action="store_true",
                           help="query OSV.dev for advisories and release data (cached)")
    behaviour.add_argument("--no-git", action="store_true",
                           help="skip all git-history analysis")
    behaviour.add_argument("--config", metavar="FILE", help="config file to use")
    behaviour.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                           help="additional exclude glob (repeatable)")
    behaviour.add_argument("--include", action="append", default=[], metavar="GLOB",
                           help="only analyse paths matching this glob (repeatable)")
    behaviour.add_argument("--only", action="append", default=[], metavar="ANALYZER",
                           help="run only these analyzers (repeatable)")
    behaviour.add_argument("--skip", action="append", default=[], metavar="ANALYZER",
                           help="skip these analyzers (repeatable)")
    behaviour.add_argument("--max-findings-per-rule", type=int, metavar="N",
                           help="cap findings shown per rule")

    info = parser.add_argument_group("rule reference")
    info.add_argument("--list-rules", action="store_true", help="list all rules and exit")
    info.add_argument("--explain", metavar="RULE_ID", help="explain one rule and exit")
    return parser


def _force_utf8() -> None:
    """Prefer UTF-8 on stdout/stderr so Unicode output is not mangled.

    Windows consoles often default to cp1252, which cannot encode the block
    and arrow characters used in the report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_rules:
        return _list_rules()
    if args.explain:
        return _explain(args.explain)
    if args.write_baseline and args.baseline:
        parser.error("--write-baseline and --baseline are mutually exclusive")
    if args.fail_on and args.fail_on.strip().upper() not in GRADES:
        if args.fail_on.strip().lower() not in {s.value for s in Severity}:
            parser.error(
                f"--fail-on must be a grade ({'/'.join(GRADES)}) or a severity "
                f"({'/'.join(s.value for s in Severity)})"
            )

    root = Path(args.path)
    if not root.exists():
        print(f"repodebt: path does not exist: {root}", file=sys.stderr)
        return EXIT_ERROR
    if not root.is_dir():
        print(f"repodebt: path is not a directory: {root}", file=sys.stderr)
        return EXIT_ERROR

    try:
        config = cfg.load(
            root,
            config_path=Path(args.config) if args.config else None,
            extra_excludes=args.exclude,
            extra_includes=args.include,
        )
    except cfg.ConfigError as exc:
        print(f"repodebt: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if config.unknown_keys:
        where = config.source_path or "config"
        print(f"repodebt: ignoring unrecognised key(s) in {where}: "
              f"{', '.join(config.unknown_keys)}", file=sys.stderr)

    if args.max_findings_per_rule is not None:
        config.data["max_findings_per_rule"] = args.max_findings_per_rule

    try:
        report = run_audit(
            root=root,
            config=config,
            use_git=not args.no_git,
            online=args.online,
            verbose=args.verbose,
            only=set(args.only) or None,
            skip=set(args.skip) or None,
        )
    except KeyboardInterrupt:
        print("\nrepodebt: interrupted", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        print(f"repodebt: audit failed: {exc}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return EXIT_ERROR

    # -- gating ------------------------------------------------------------
    failure: str | None = None
    diff: dict | None = None

    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"repodebt: baseline not found: {baseline_path}", file=sys.stderr)
            return EXIT_ERROR
        try:
            baseline = json_out.load_baseline(baseline_path)
        except (OSError, ValueError) as exc:
            print(f"repodebt: could not read baseline: {exc}", file=sys.stderr)
            return EXIT_ERROR
        diff = json_out.compare(report, baseline, args.max_score_drop)
        regressions = [r for r in diff["regressions"] if r["kind"] != "score_drop"]
        if regressions:
            failure = (f"{len(regressions)} regression(s) against baseline "
                       f"{baseline_path}")
        delta = (diff.get("score") or {}).get("delta")
        if delta is not None and delta < -abs(args.max_score_drop):
            if failure is None:
                failure = (f"score dropped {abs(delta):.1f} points "
                           f"(limit {args.max_score_drop})")

    # Build the JSON payload once so the diff can be attached before the file
    # is written, rather than reading the file back off disk and rewriting it.
    payload: str | None = None
    if args.json or args.format == "json" or args.write_baseline:
        payload = json_out.dumps(report, diff=diff)

    if args.json:
        json_out.write_text(payload, Path(args.json))
    if args.markdown:
        target = Path(args.markdown)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown.render(report), encoding="utf-8")

    if args.format == "json":
        print(payload)
    elif args.format == "text":
        print(console_render.render(report, color=False if args.no_color else None,
                                    verbose=args.verbose))
        if diff is not None:
            print(json_out.render_diff(diff, color=not args.no_color))

    if args.write_baseline:
        # A baseline is the report on its own; embedding the diff of an
        # earlier run would make the next comparison depend on this file.
        json_out.write_text(json_out.dumps(report), Path(args.write_baseline))
        print(f"baseline written to {args.write_baseline}", file=sys.stderr)

    if failure is None and args.fail_on:
        failure = _check_fail_on(report, args.fail_on)

    if failure:
        print(f"repodebt: FAIL — {failure}", file=sys.stderr)
        return EXIT_THRESHOLD
    if args.fail_on or args.baseline:
        print("repodebt: OK", file=sys.stderr)
    return EXIT_OK


def _check_fail_on(report: Report, target: str) -> str | None:
    value = target.strip()
    if value.upper() in GRADES:
        grade = value.upper()
        order = GRADES.index(grade)
        current = report.scorecard.grade
        if current is None:
            return None  # no score: nothing to gate on
        if GRADES.index(current) > order:
            return f"grade {current} is worse than required {grade}"
        return None

    try:
        threshold = Severity(value.lower())
    except ValueError:
        return None
    breaching = [f for f in report.findings if f.severity.rank >= threshold.rank]
    if breaching:
        worst = breaching[0]
        return (f"{len(breaching)} finding(s) at or above {threshold.value}; "
                f"worst: {worst.id} ({worst.title})")
    return None


def _list_rules() -> int:
    by_dimension: dict[str, list] = {}
    for rule in RULES.values():
        by_dimension.setdefault(rule.dimension.value, []).append(rule)
    for dimension in sorted(by_dimension, key=lambda d: -len(by_dimension[d])):
        print(f"\n{dimension.upper()}  ({len(by_dimension[dimension])} rules)")
        print("-" * 72)
        for rule in sorted(by_dimension[dimension], key=lambda r: r.id):
            print(f"  {rule.id:<38} {rule.severity.value:<8} {rule.title}")
    print(f"\n{len(RULES)} rules total. Use --explain <rule-id> for detail.\n")
    return EXIT_OK


def _explain(rule_id: str) -> int:
    rule = RULES.get(rule_id)
    if rule is None:
        print(f"repodebt: unknown rule: {rule_id}", file=sys.stderr)
        print("Run 'repodebt --list-rules' to see all rule ids.", file=sys.stderr)
        return EXIT_ERROR
    print(f"\n{rule.id}")
    print("=" * len(rule.id))
    print(f"  dimension   {rule.dimension.label}")
    print(f"  severity    {rule.severity.value}")
    print(f"  title       {rule.title}")
    print()
    print("  why it matters")
    print(f"    {rule.why}")
    print()
    print("  remediation")
    print(f"    {rule.remediation}")
    print()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
