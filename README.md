# repodebt

A repository tech-debt and health auditor. One command, no install, no
network, no service to configure. It reads a source tree, measures what it
can actually measure, prints a report you can read in a terminal, and emits
JSON you can gate a build on.

```console
$ repodebt
================================== REPODEBT ==================================
 myproject  ·  412 files  ·  318 source  ·  1.4s
 main @ 9f2c1ab
────────────────────────────────────────────────────────────────────────────

  ████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
   71.4 / 100    grade C

  Complexity         ████████████████████░░░░░░░░░░  62.3  C    47 findings
  Hotspots           ██████████████████████████░░░░░░  88.1  B    12 findings
  Test Health        ██████████████░░░░░░░░░░░░░░░░░  55.0  F    23 findings
  Repo Hygiene       ██████████████████████████████░░  96.2  A     3 findings
  Dependency Risk    ████████████████████████████████ 100.0  A     0 findings
```

## Install

Requires Python 3.11+ (for `tomllib`) and `git` on `PATH` for the
history-based signals. **No third-party packages** — everything is stdlib.

```console
$ pip install -e .          # or just put src/ on PYTHONPATH
$ repodebt
```

Without installing:

```console
$ PYTHONPATH=src python -m repodebt
```

## What it measures

Five dimensions, each scored 0–100 and then combined into a weighted average.

| Dimension | Weight | Source | What it looks at |
| --- | ---: | --- | --- |
| Complexity | 25% | files | branch complexity, function length, nesting depth, parameter count, file size, long-line ratio, clone detection |
| Test health | 25% | files + git | test-to-source ratio, assertions per 100 lines, empty and skipped test files, files with no tests, stale tests |
| Hotspots | 20% | git | commits × complexity, sole-owned files, commit cadence |
| Repo hygiene | 15% | files + git | README/LICENSE/CI/CODEOWNERS, large blobs, dormancy, dry spells, bus factor, merge ratio, commit message conventions, long-lived branches |
| Dependency risk | 15% | manifests | unpinned and floating versions, missing lockfiles, known advisories (online only) |

Run `repodebt --list-rules` for all 44 rules, or `repodebt --explain <id>` for
the reasoning and remediation behind any single one.

### Python gets exact measurements

For `.py` files, complexity comes from the `ast` module: real McCabe
complexity, real control-flow nesting depth, real parameter counts. For
everything else it is a **heuristic** — brace/keyword counting over a
tokenised view of the file. Heuristic findings are tagged
`"confidence": "heuristic"` and their penalty is multiplied by **0.5**, so
they nudge the score rather than dominating it. The distinction is always
visible in the JSON.

## Output

Three renderings of the same report:

```console
$ repodebt                                    # terminal report
$ repodebt --markdown HEALTH.md               # for a PR comment or wiki
$ repodebt --json health.json                 # for CI
$ repodebt --format json                      # JSON on stdout
$ repodebt --format none                      # silence; exit code only
```

The JSON document is the stable contract:

```json
{
  "schema_version": "1.0",
  "generated_at": 1750000000.0,
  "duration_seconds": 1.42,
  "config_digest": "a1b2c3",
  "repo": { "name": "myproject", "is_git_repo": true, "branch": "main", ... },
  "scores": {
    "overall": 71.4,
    "grade": "C",
    "note": "",
    "dimensions": { "complexity": { "score": 62.3, "grade": "C", ... } }
  },
  "metrics": { "complexity": { ... }, "tests": { ... } },
  "findings": [ ... ],
  "unavailable": { "bus_factor": "not a git repository" }
}
```

`unavailable` is the important part. Anything repodebt could not measure is
listed there with a reason, its dimension scores `null` instead of 100, and
it is dropped from the weighted average. **A signal that could not be
computed is never reported as a signal that passed.** A repo audited without
git shows you exactly which conclusions you are not entitled to draw.

This holds in both directions. An analyzer that runs but cannot produce a
measurement marks itself unscored rather than returning an empty finding
list, because *no findings* and *nothing to find* are different claims. A
repository with two commits in the window has no churn to rank, so hotspots
is reported as unmeasured — not as a clean sheet. Hotspots carries a fifth
of the overall weight, and scoring an unrankable dimension as perfect would
inflate the grade on the strength of a measurement that never happened.

## Scoring

The model is deliberately simple enough to argue with:

```
penalty = Σ (severity_penalty × confidence_multiplier)   per dimension
score   = 100 × (1 − min(1, penalty / budget[dimension]))
overall = Σ (score × weight) / Σ weight                   over available dimensions
```

Severity penalties: critical 10, high 6, medium 3, low 1, info 0.3.
Confidence multipliers: high 1.0, medium 0.8, heuristic 0.5.
Grade bands: A ≥ 90, B ≥ 80, C ≥ 70, D ≥ 60, F < 60.

Penalty budgets (the total penalty that would drive a dimension to zero):
complexity 40, tests 40, hotspots 30, hygiene 30, deps 30.

### One deliberate override

A single `CRITICAL` finding is worth 10 penalty points. In a large healthy
repo that is diluted by the weighted average into a perfect **A** — which
reads as a bug next to a finding list containing a known-exploitable
dependency. So an open critical finding **clamps the overall score to 89.0**,
making a B the best achievable grade. The clamp only ever lowers a score,
and it says so:

```
   89.0 / 100    grade B
  overall capped at 89.0 despite a weighted average of 94.2: 1 open
  critical finding (deps.vulnerability) is diluted by the other dimensions
```

Set `critical_grade_cap = null` in config to switch it off.

## CI gating

Two independent mechanisms.

**Grade or severity thresholds:**

```console
$ repodebt . --fail-on C              # fail if the grade is worse than C
$ repodebt . --fail-on high           # fail if any finding is high or critical
```

**Baseline comparison** — the more useful one, because it fails on *change*
rather than on absolute level. New findings and score drops fail; fixed
findings and improvements are reported but never break a build.

```console
# once, on a healthy tree
$ repodebt . --write-baseline health.json

# in CI, on every build
$ repodebt . --baseline health.json --max-score-drop 2
```

`--baseline` never fails on an improvement. It reports new findings, findings
that got more common, and score drops, with per-dimension deltas. If the
config changed since the baseline was written, the diff says so — a threshold
move is not a regression.

Exit codes: **0** pass, **1** gating failure, **2** bad invocation or an
unreadable config/baseline.

## Configuration

Drop a `repodebt.toml` in the repo root, or a `[tool.repodebt]` table in
`pyproject.toml`, or pass `--config path.toml`. Precedence, lowest to
highest: built-in defaults → `repodebt.toml` / `[tool.repodebt]` →
`.repodebtignore` → `--include` / `--exclude` flags.

```toml
# repodebt.toml
[weights]
complexity = 0.30
tests      = 0.30
hotspots   = 0.15
deps       = 0.10
hygiene    = 0.15

[grade_bands]
# [[90.0, "A"], [80.0, "B"], ...] -- lower is stricter, higher is stricter

# Anything below these counts as a finding.
large_file_lines    = 400
long_function_lines = 50
max_nesting_depth   = 3
duplication_min_lines = 15

# Git window.
history_window_days = 180
dormant_days        = 90

# How many findings to show per rule before folding the rest into one.
max_findings_per_rule = 12
```

Unrecognised keys are reported on stderr rather than ignored. A config that
parses but applies nothing is the worst failure mode for a tool whose promise
is that the number is trustworthy:

```
$ repodebt .
repodebt: ignoring unrecognised key(s) in repodebt.toml: largefile_lines
```

This also catches the most common way to write a config that silently does
nothing — a top-level key placed *after* a `[table]` header, where TOML
quietly nests it (`weights.large_file_lines`).

`.repodebtignore` is a plain list of globs, one per line, `#` for comments:

```
# generated, not ours to fix
api/schema/generated/
*.pb.go
third_party/
```

## Dependency advisories

Offline by default. `--online` opts in to querying OSV.dev for known
advisories and release data. Results are cached under `~/.cache/repodebt/`
with a TTL (`cache_ttl_hours`, default 24); set `cache_dir` to relocate it,
which is what you want for a CI cache. The `deps.online` metric reports
`queried`, `cache_hits` and `fetched` separately, so a warm run is visibly
warm rather than merely fast.

In `repodebt.toml`, quote a Windows path with single quotes — TOML treats
backslashes inside double-quoted strings as escapes:

```toml
cache_dir = 'C:/ci/repodebt-cache'
cache_ttl_hours = 24
```

Severity comes from the advisory's own CVSS base score, which is computed from
the vector string (CVSS v3.0/v3.1) per the published specification. An
advisory carrying a CVE alias but no rating is reported as `high`; one with
neither is `medium`. Ratings are never invented to look worse than the evidence
supports, and a failed lookup never produces a finding.

Maven and Gradle coordinates are queried with their full `group:artifact`
name, and Packagist packages with their `vendor/package` name, because those
are the identifiers OSV indexes. Trimming either to its last component would
silently miss every advisory for the package.

Requirements files are classified by filename, so `requirements-dev.txt`,
`requirements/tests.txt` and similar hold test and tooling pins rather than
shipped dependencies. Counting them as production overstates what a release
installs, and sends the advisory lookup after packages the project never
deploys.

The tool stays fully functional offline. If the network is unavailable,
`--online` degrades to offline mode and records the reason in
`unavailable{}` rather than failing the run or inventing results.

## Full flag list

```
repodebt [PATH]

  --json FILE              write the machine-readable report
  --markdown FILE          write a Markdown report
  --format text|json|none  stdout format (default: text)
  --no-color               disable ANSI colour
  -v, --verbose            show timings and extra detail

  --baseline FILE          compare against a previous report, fail on regressions
  --fail-on TARGET         A-F grade, or critical|high|medium|low severity
  --max-score-drop N       with --baseline, tolerance in score points
  --write-baseline FILE    write the current report as a baseline and exit

  --online                 query OSV.dev (cached); off by default
  --no-git                 skip all history-based analysis
  --config FILE            config file to use
  --include GLOB           only analyse paths matching this glob (repeatable)
  --exclude GLOB           additional exclude glob (repeatable)
  --only ANALYZER          run only these analyzers (repeatable)
  --skip ANALYZER          skip these analyzers (repeatable)
  --max-findings-per-rule N

  --list-rules             list all rules and exit
  --explain RULE_ID        explain one rule and exit
```

Analyzers: `complexity`, `tests`, `hotspots`, `hygiene`, `deps`.

## Degrading gracefully

repodebt treats missing inputs as unknown, never as healthy:

- **No git** → hotspots unscored, history-based hygiene signals listed in
  `unavailable{}`, filesystem hygiene still scored.
- **No `git` binary** → same, with the reason recorded.
- **Too little history to rank churn** → hotspots unscored. Churn ranking
  needs at least `hotspot_min_commits` commits touching a *single* file; a
  handful of commits spread thinly is not a ranking, and a real repository
  has more than two commits.
- **No dependency manifest** → dependency risk unscored rather than perfect.
- **A file that will not parse** → `complexity.parse_error`, and the rest of
  the file is skipped instead of the run dying.
- **No online access** → offline mode, reason recorded.

## Development

```console
$ python -m unittest discover -s tests -t tests
```

319 tests, stdlib `unittest` only. The suite builds real git repositories in
temp directories with commit dates pinned relative to the current clock, so
churn, bus-factor, and staleness assertions stay deterministic — and stay
inside the history window as time passes. The git-dependent tests take most
of the runtime (roughly four minutes, dominated by process spawns on Windows).

Layout:

```
src/repodebt/
  cli.py          argument parsing, output routing, gating
  audit.py        orchestration: walk, analyze, score, assemble
  walker.py       file discovery, language table
  gitlog.py       git subprocess wrapper
  config.py       defaults, config precedence, glob matching
  scoring.py      findings -> scores -> grade
  rules.py        the rule catalog (backs --list-rules / --explain)
  model.py        dataclasses and the JSON contract
  analyzers/      one module per dimension
  render/         console, markdown, json
```
