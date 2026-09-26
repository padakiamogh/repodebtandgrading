"""Optional online dependency intelligence via OSV.dev.

OSV needs no API key. Results are cached on disk with a TTL so repeated CI
runs are cheap, and every network failure degrades to an ``unavailable``
metric rather than an exception.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..model import Location, Severity

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns"

#: Internal ecosystem label (as used by ``analyzers.deps``) -> the ecosystem
#: string OSV expects. The direction matters: these are two different naming
#: schemes, and looking one up in the other silently yields ``None`` for
#: everything that happens to differ -- which is every ecosystem except the
#: one whose label is spelled the same in both.
#:
#: Gradle coordinates are Maven coordinates, so both map to ``Maven``.
OSV_ECOSYSTEM_BY_LABEL: dict[str, str] = {
    "python": "PyPI",
    "npm": "npm",
    "cargo": "crates.io",
    "gomod": "Go",
    "maven": "Maven",
    "gradle": "Maven",
    "composer": "Packagist",
    "bundler": "RubyGems",
    "pub": "Pub",
}

TIMEOUT = 20
BATCH_SIZE = 100


def _cache_file(ctx, kind: str) -> Path:
    ttl = float(ctx.config.get("cache_ttl_hours", 24))
    base = ctx.config.cache_dir()
    return base / f"osv-{kind}-{int(ttl)}h.json"


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json",
                                 "User-Agent": "repodebt/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return None


def _get(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "repodebt/1.0"}), timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return None


# --- CVSS v3.x base score -------------------------------------------------
#
# OSV's ``severity`` array carries CVSS *vector* strings ("CVSS:3.1/AV:N/..."),
# not bare numbers. Taking the last "/"-segment and reading digits off it does
# not score a vector -- "A:H" is not a number -- so the whole array falls
# through and every advisory gets the default severity. The base score has to
# be computed. The formula below is the published CVSS v3.1 specification.

_CVSS3_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_CVSS3_AC = {"L": 0.77, "H": 0.44}
_CVSS3_PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_CVSS3_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.50}
_CVSS3_UI = {"N": 0.85, "R": 0.62}
_CVSS3_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def _cvss3_roundup(value: float) -> float:
    """The CVSS-specified Roundup, which is not plain rounding.

    The specification rounds to five decimals *before* applying the ceiling
    step. Truncating instead would reintroduce the binary-float artifact the
    Roundup exists to absorb: 4.00001 * 100000 lands just below 400001, and an
    ``int()`` truncation would call that a clean 4.0.
    """
    scaled = int(round(value * 100000))
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (scaled // 10000 + 1) / 10.0


def _cvss3_base_score(vector: str) -> float | None:
    """Base score for a CVSS v3.0/v3.1 vector string, or None if unparseable."""
    parts = dict(
        piece.split(":", 1)
        for piece in vector.strip().split("/")
        if ":" in piece
    )
    if not parts or not parts.get("CVSS", "").startswith("3."):
        return None
    try:
        scope_changed = parts["S"] == "C"
        av = _CVSS3_AV[parts["AV"]]
        ac = _CVSS3_AC[parts["AC"]]
        pr = (_CVSS3_PR_CHANGED if scope_changed else _CVSS3_PR_UNCHANGED)[parts["PR"]]
        ui = _CVSS3_UI[parts["UI"]]
        conf = _CVSS3_CIA[parts["C"]]
        integ = _CVSS3_CIA[parts["I"]]
        avail = _CVSS3_CIA[parts["A"]]
    except KeyError:
        return None

    iss = 1 - ((1 - conf) * (1 - integ) * (1 - avail))
    if scope_changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * pr * ui
    total = impact + exploitability
    if scope_changed:
        total *= 1.08
    return _cvss3_roundup(min(total, 10.0))


def _severity_for(advisory: dict[str, Any]) -> Severity:
    database = (advisory.get("database_specific") or {}).get("severity")
    if isinstance(database, str) and database.upper() == "CRITICAL":
        return Severity.CRITICAL

    entries = [e for e in (advisory.get("severity") or []) if isinstance(e, dict)]
    # Prefer v3: an advisory carrying both a v2 and a v3 score should be rated
    # on the current scale, and iterating in document order would otherwise
    # depend on how the upstream database happened to serialise it.
    entries.sort(key=lambda e: 0 if "3" in str(e.get("type", "")).lower()
                 or str(e.get("score", "")).upper().startswith("CVSS:3")
                 else 1)

    for entry in entries:
        raw = str(entry.get("score", "")).strip()
        if not raw:
            continue
        value: float | None = None
        if raw.upper().startswith("CVSS:"):
            value = _cvss3_base_score(raw)
        else:
            # Some feeds put a bare number in "severity".
            try:
                value = float(raw)
            except ValueError:
                value = None
        if value is None:
            continue
        if value >= 9.0:
            return Severity.CRITICAL
        if value >= 7.0:
            return Severity.HIGH
        if value >= 4.0:
            return Severity.MEDIUM
        return Severity.LOW

    # No usable score. An alias means a CVE exists for the same flaw, which is
    # a stronger signal than "unrated", but it is not evidence of exploitability.
    if advisory.get("aliases"):
        return Severity.HIGH
    return Severity.MEDIUM


def _fixed_versions(advisory: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for affected in advisory.get("affected", []) or []:
        for rng in affected.get("ranges", []) or []:
            for event in rng.get("events", []) or []:
                if "fixed" in event:
                    out.append(str(event["fixed"]))
    return out


def _osv_ecosystem(dep) -> str | None:
    """The OSV ecosystem string for a declared dependency, if OSV has one."""
    return OSV_ECOSYSTEM_BY_LABEL.get(dep.ecosystem)


def _osv_package_name(dep) -> str:
    """The package name OSV expects for this dependency.

    Normalisation is per-ecosystem, and the mistakes are not symmetric.
    Reducing a name to its last ``/`` or ``:`` component is right for npm
    scopes and wrong for Maven ``group:artifact`` and Packagist
    ``vendor/package``, where dropping the prefix makes the query miss.
    """
    name = dep.name
    if dep.ecosystem == "python":
        # PEP 503 normalisation, and the extras that requirement lines carry
        # in the name ("uvicorn[standard]") are not part of the distribution.
        name = re.sub(r"\[.*?\]", "", name).lower()
        name = re.sub(r"[-_.]+", "-", name)
    elif dep.ecosystem == "npm":
        name = name.rsplit("/", 1)[-1].lower()
    # Everything else is passed through as declared: crates.io, Go module
    # paths, Maven group:artifact, Packagist vendor/package, gems, and pub
    # packages are all addressed by their full name.
    return name


def _query_key(dep) -> str:
    """A cache key for one OSV query.

    Encoded as JSON rather than joined with a separator: Maven and Gradle
    coordinates contain colons, so a colon-delimited key cannot be split back
    apart -- "Maven:com.google.guava:guava:31.0" would yield the name
    "com.google.guava" and the version "guava:31.0", and every Maven advisory
    would then be looked up under garbage.
    """
    return json.dumps(
        [_osv_ecosystem(dep), _osv_package_name(dep), dep.version],
        separators=(",", ":"),
    )


def enrich_dependencies(ctx, deps: list, findings: list, make_finding) -> dict[str, Any]:
    """Query OSV for advisories and record findings. Never raises."""
    notes: dict[str, Any] = {
        "queried": 0, "cache_hits": 0, "fetched": 0, "errors": [],
    }

    queryable = [
        d for d in deps
        if d.group in ("prod", "optional") and _osv_ecosystem(d) and d.version
    ]
    notes["queried"] = len(queryable)
    if not queryable:
        notes["message"] = "no queryable production dependencies"
        return notes

    cache_path = _cache_file(ctx, "vulns")
    cache = _load_cache(cache_path)

    keyed = {id(dep): _query_key(dep) for dep in queryable}
    pending = sorted({key for key in keyed.values() if key not in cache})
    notes["cache_hits"] = len(queryable) - len(pending)

    for key in pending:
        ecosystem, name, version = json.loads(key)
        payload = _post(OSV_BATCH_URL, {
            "queries": [{
                "package": {"name": name, "ecosystem": ecosystem},
                "version": version,
            }]
        })
        if payload is None:
            notes["errors"].append(f"OSV request failed for {name}")
            cache[key] = ["__error__"]
            continue
        ids: list[str] = []
        for result in payload.get("results", []) or []:
            for vuln in result.get("vulns", []) or []:
                vuln_id = vuln.get("id")
                if vuln_id:
                    ids.append(vuln_id)
        cache[key] = sorted(set(ids))
    if pending:
        _save_cache(cache_path, cache)

    # Pull advisory detail for anything new.
    detail_cache_path = _cache_file(ctx, "detail")
    details = _load_cache(detail_cache_path)
    new_ids: set[str] = set()
    for key, ids in cache.items():
        if ids == ["__error__"]:
            continue
        for vuln_id in ids or []:
            if vuln_id not in details:
                new_ids.add(vuln_id)
    for vuln_id in sorted(new_ids):
        data = _get(f"{OSV_VULN_URL}/{vuln_id}")
        details[vuln_id] = data or {}
        # "fetched" is a count of network round-trips, not of cache hits; the
        # two are conflated in a single "cached" number, which reads as the
        # opposite of what it measures.
        notes["fetched"] += 1
    if new_ids:
        _save_cache(detail_cache_path, details)

    affected_deps: list[tuple] = []
    advisories_by_key: dict[str, list[str]] = {}
    for dep in queryable:
        key = keyed[id(dep)]
        ids = [i for i in cache.get(key, []) if i != "__error__"]
        if not ids:
            continue
        advisories_by_key.setdefault(key, ids)
        affected_deps.append((dep, key, ids))

    for dep, key, ids in affected_deps:
        worst = Severity.MEDIUM
        evidence: list[str] = []
        for vuln_id in ids:
            advisory = details.get(vuln_id) or {}
            severity = _severity_for(advisory)
            if severity.rank > worst.rank:
                worst = severity
            fixed = _fixed_versions(advisory)
            summary = (advisory.get("summary") or "").strip()
            line = f"{vuln_id}"
            if fixed:
                line += f" (fixed in {', '.join(sorted(set(fixed))[:3])})"
            if summary:
                line += f": {summary[:120]}"
            evidence.append(line)
        findings.append(make_finding(
            "deps.vulnerability",
            detail=(f"{dep.name} {dep.version} is affected by {len(ids)} published "
                    f"advisory/advisories."),
            severity=worst,
            evidence=evidence[:8],
            locations=[Location(dep.manifest, dep.line or None)],
            occurrences=len(ids),
        ))

    notes["affected"] = len(affected_deps)
    notes["advisories"] = sum(len(v) for v in advisories_by_key.values())
    if notes["errors"]:
        notes["degraded"] = True
    return notes
