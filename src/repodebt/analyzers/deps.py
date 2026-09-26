"""Dependency manifest parsing and dependency-risk rules.

Parsers are intentionally lenient: a manifest that cannot be understood is
reported (``deps.parse_error``) rather than silently contributing zero
dependencies, because "we found no dependencies" and "we failed to read the
manifest" are very different facts.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from ..model import Confidence, Dimension, Location, Severity
from ..walker import FileInfo
from .base import Analyzer, Context, Result, make_finding

# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

PROD = "prod"
DEV = "dev"
OPTIONAL = "optional"
PEER = "peer"


@dataclass
class DeclaredDependency:
    name: str
    spec: str
    ecosystem: str
    manifest: str
    group: str = PROD
    line: int = 0
    pinned_exact: bool = False

    @property
    def version(self) -> str | None:
        return extract_version(self.spec, self.ecosystem)


@dataclass
class Manifest:
    path: str
    ecosystem: str
    dependencies: list[DeclaredDependency] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------
# Version helpers
# --------------------------------------------------------------------------

_EXACT_PATTERNS = {
    "npm": re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$"),
    "python": re.compile(r"^\d+(?:\.\d+)*(?:[-_.]?[0-9A-Za-z]+)*$"),
    "cargo": re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$"),
    "gomod": re.compile(r"^v\d+\.\d+\.\d+"),
    "maven": re.compile(r"^\d+(?:\.\d+)*[-.][A-Za-z0-9.\-]+$"),
    "bundler": re.compile(r"^\d+(?:\.\d+)*$"),
    "generic": re.compile(r"^\d+(?:\.\d+)+"),
}

_UNPINNED_MARKERS = ("^", "~", ">", "<", "*", "latest", "x", "X", " ", "||", "||=")

_ENV_MARKER = re.compile(r";\s*python_version|;\s*sys_platform|;\s*platform_system|;\s*os_name")


def extract_version(spec: str, ecosystem: str) -> str | None:
    """Pull a comparable version string out of a range expression."""
    if not spec:
        return None
    text = spec.strip()
    if ecosystem == "npm":
        text = re.sub(r"^[\^~>=<\s]*", "", text)
        text = text.split("||")[0].strip()
        match = re.match(r"(\d+(?:\.\d+)*)", text)
        return match.group(1) if match else None
    if ecosystem == "gomod":
        match = re.match(r"v?(\d[\w.\-+]*)", text)
        return match.group(1) if match else None
    if ecosystem == "maven":
        placeholder = re.match(r"^\$\{([^}]+)\}$", text.strip())
        if placeholder:
            # A property reference: report the property so it is visible even
            # though the resolved version is not knowable from the POM alone.
            return placeholder.group(1)
        match = re.match(r"\$?\{?[\w.\-]*\}?([\d][\w.\-]*)", text)
        return match.group(1) if match else None
    if ecosystem == "gradle":
        # The parser has already split the coordinate, so a bare version
        # reaches here. Ranges ("[1.0,2.0)") name a lower bound rather than a
        # release, and the generic path's "["-strip (there to drop Python
        # extras) would discard them entirely.
        if re.search(r"\+|latest\.|\*", text):
            # A dynamic selector names no concrete release, so there is
            # nothing for an advisory lookup to match against. The marker is
            # tested against the whole spec rather than the extracted
            # substring: "1.2.*" loses its "*" before extraction and would
            # otherwise read as a concrete 1.2.
            return None
        match = re.search(r"\d[\w.\-+]*", text)
        if not match:
            return None
        return match.group(0).rstrip(".")
    # python / cargo / bundler / composer / pub
    text = _ENV_MARKER.split(text)[0].strip()
    text = text.split(",")[0].strip()
    for prefix in ("===", "==", ">=", "<=", "~=", "!=", ">", "<", "^", "~", "="):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    text = text.split("[")[0].strip()
    match = re.match(r"(\d[\w.\-+]*)", text)
    return match.group(1) if match else None


def is_pinned_exact(spec: str, ecosystem: str) -> bool:
    text = spec.strip()
    if not text:
        return False
    if any(marker in text for marker in ("||", " ", "&&")) and ecosystem not in ("maven",):
        # Compound or multi-constraint specs are ranges.
        if ecosystem != "python" or not text.startswith("=="):
            return False
    if ecosystem == "python":
        return text.split(";")[0].strip().startswith("===") or text.split(";")[0].strip().startswith("==")
    if ecosystem == "npm":
        return bool(_EXACT_PATTERNS["npm"].match(text))
    if ecosystem == "gomod":
        return bool(_EXACT_PATTERNS["gomod"].match(text))
    if ecosystem == "bundler":
        return bool(_EXACT_PATTERNS["bundler"].match(text))
    if ecosystem == "gradle":
        return "latest" not in text and "[" not in text and "+" not in text
    if ecosystem == "maven":
        return "${" not in text
    if ecosystem == "composer" or ecosystem == "pub":
        return re.match(r"^\d", text) is not None
    return _EXACT_PATTERNS["generic"].match(text) is not None


def is_range(spec: str, ecosystem: str) -> bool:
    return not is_pinned_exact(spec, ecosystem) and bool(extract_version(spec, ecosystem))


def version_tuple(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    parts = re.findall(r"\d+", version.split("-")[0])
    return tuple(int(p) for p in parts[:4]) or (0,)


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

def _line_of(text: str, needle: str) -> int:
    index = text.find(needle)
    return text.count("\n", 0, index) + 1 if index >= 0 else 0


def parse_package_json(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="npm")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        manifest.error = f"invalid JSON at line {exc.lineno}: {exc.msg}"
        return manifest
    for group, key in ((PROD, "dependencies"), (DEV, "devDependencies"),
                       (OPTIONAL, "optionalDependencies"), (PEER, "peerDependencies")):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            if not isinstance(spec, str):
                continue
            manifest.dependencies.append(DeclaredDependency(
                name=name, spec=spec, ecosystem="npm", manifest=path, group=group,
                line=_line_of(text, f'"{name}"'),
                pinned_exact=is_pinned_exact(spec, "npm"),
            ))
    return manifest


_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*(?:\[[^\]]*\])?)\s*"
    r"(?P<spec>(?:[<>!=~]=?|===|\s)*[^#;]*)"
)


#: Filename fragments that mark a requirements file as holding test/tooling
#: dependencies rather than things the project ships. Without this, a
#: ``requirements-dev.txt`` listing pytest and coverage is reported as a
#: production dependency manifest: the counts overstate what is deployed, and
#: an online advisory lookup would query packages the release never installs.
_DEV_REQ_PARTS = ("dev", "devel", "test", "testing", "contrib")


def _requirements_group(path: str) -> str:
    """Classify a requirements file by name as production or development."""
    name = re.split(r"[/\\]", path.rsplit("/", 1)[-1])[-1].lower()
    # requirements/dev.txt and requirements/test.txt put the marker in the
    # directory rather than the filename, so look at both components.
    parts = re.split(r"[/\\]", path.lower())
    for part in parts[1:]:
        if any(marker in part for marker in _DEV_REQ_PARTS):
            return DEV
    stem = name[: -len(".txt")] if name.endswith(".txt") else name
    if any(marker in stem for marker in _DEV_REQ_PARTS):
        return DEV
    return PROD


def parse_requirements(text: str, path: str, group: str | None = None) -> Manifest:
    manifest = Manifest(path=path, ecosystem="python")
    if group is None:
        group = _requirements_group(path)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Strip inline comments that are not part of a URL.
        if "#" in line and "://" not in line:
            line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("git+", "http://", "https://", "file:", ".", "/")):
            continue
        match = _REQUIREMENT_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        spec = (match.group("spec") or "").strip()
        base = re.split(r"[\[<]", name)[0]
        manifest.dependencies.append(DeclaredDependency(
            name=base, spec=spec, ecosystem="python", manifest=path, group=group,
            pinned_exact=is_pinned_exact(spec, "python"),
        ))
    return manifest


def parse_pyproject(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="python")
    if tomllib is None:  # pragma: no cover
        manifest.error = "tomllib unavailable"
        return manifest
    try:
        data = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        manifest.error = str(exc)
        return manifest

    project = data.get("project", {})
    for dep in project.get("dependencies", []) or []:
        if isinstance(dep, str):
            manifest.dependencies.append(_pep508(dep, path, text, PROD))
    optional = project.get("optional-dependencies", {}) or {}
    for group, deps in optional.items():
        target = OPTIONAL if group == "test" else OPTIONAL
        for dep in deps or []:
            if isinstance(dep, str):
                manifest.dependencies.append(_pep508(dep, path, text, target))
    groups = data.get("dependency-groups", {}) or {}
    for group, deps in groups.items():
        target = DEV if group in ("dev", "lint", "test", "tests", "typing") else OPTIONAL
        for dep in deps or []:
            if isinstance(dep, str) and not dep.startswith("-"):
                manifest.dependencies.append(_pep508(dep, path, text, target))
            elif isinstance(dep, dict) and "git" in dep:
                pass  # VCS dependency, nothing to pin
    build = data.get("build-system", {}) or {}
    for dep in build.get("requires", []) or []:
        if isinstance(dep, str):
            manifest.dependencies.append(_pep508(dep, path, text, DEV))

    poetry = ((data.get("tool", {}) or {}).get("poetry", {}) or {})
    legacy = ((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("dependencies", {}) or {}
    for name, spec in legacy.items():
        if name.lower() == "python":
            continue
        if isinstance(spec, dict):
            version = str(spec.get("version", ""))
            group = OPTIONAL if spec.get("optional") else PROD
        else:
            version = str(spec)
            group = PROD
        manifest.dependencies.append(DeclaredDependency(
            name=name, spec=version, ecosystem="python", manifest=path, group=group,
            line=_line_of(text, name), pinned_exact=is_pinned_exact(version, "python"),
        ))
    dev_deps = poetry.get("dev-dependencies", {}) or {}
    for name, spec in dev_deps.items():
        version = spec if isinstance(spec, str) else str((spec or {}).get("version", ""))
        manifest.dependencies.append(DeclaredDependency(
            name=name, spec=version, ecosystem="python", manifest=path, group=DEV,
            line=_line_of(text, name), pinned_exact=is_pinned_exact(version, "python"),
        ))
    return manifest


def _pep508(dep: str, path: str, text: str, group: str) -> DeclaredDependency:
    spec = dep
    name = dep
    for sep in ("[", "<", ">", "=", "!", "~", ";", " "):
        if sep in spec:
            head, _, tail = spec.partition(sep)
            if sep == "[" and head.strip():
                name = head.strip()
                spec = sep + tail
                break
            if head.strip() and not head.strip().endswith(":"):
                if sep in "=<>!~;":
                    name = head.strip()
                    spec = sep + tail
                    break
    name = re.split(r"[\[<>=!~; ]", name)[0].strip()
    return DeclaredDependency(
        name=name, spec=spec.strip(), ecosystem="python", manifest=path, group=group,
        line=_line_of(text, name), pinned_exact=is_pinned_exact(spec, "python"),
    )


def parse_pipfile(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="python")
    section = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if "#" in raw and "://" not in raw else raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.strip("[]").strip()
            continue
        if not stripped or "=" not in stripped or section not in ("packages", "dev-packages"):
            continue
        name, _, spec = stripped.partition("=")
        name = name.strip().strip('"').strip("'")
        spec = spec.strip().strip('"').strip("'")
        if not name or name.startswith("#"):
            continue
        manifest.dependencies.append(DeclaredDependency(
            name=name, spec=spec, ecosystem="python", manifest=path,
            group=PROD if section == "packages" else DEV,
            line=_line_of(text, name), pinned_exact=is_pinned_exact(spec, "python"),
        ))
    return manifest


_GO_REQUIRE = re.compile(r"^\s*(?P<name>[\w.\-/~]+\.[\w.\-/~]+)\s+(?P<spec>v[\w.\-+]+)")


def parse_go_mod(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="gomod")
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if in_block:
            match = _GO_REQUIRE.match(line)
            if match:
                manifest.dependencies.append(DeclaredDependency(
                    name=match.group("name"), spec=match.group("spec"), ecosystem="gomod",
                    manifest=path, group=PROD, line=_line_of(text, match.group("name")),
                    pinned_exact=is_pinned_exact(match.group("spec"), "gomod"),
                ))
            continue
        if line.startswith("require "):
            match = _GO_REQUIRE.match(line[len("require "):])
            if match:
                manifest.dependencies.append(DeclaredDependency(
                    name=match.group("name"), spec=match.group("spec"), ecosystem="gomod",
                    manifest=path, group=PROD, line=_line_of(text, match.group("name")),
                    pinned_exact=is_pinned_exact(match.group("spec"), "gomod"),
                ))
    return manifest


def parse_cargo_toml(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="cargo")
    if tomllib is None:  # pragma: no cover
        manifest.error = "tomllib unavailable"
        return manifest
    try:
        data = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        manifest.error = str(exc)
        return manifest
    for name, spec in (data.get("dependencies", {}) or {}).items():
        if isinstance(spec, str):
            version, line = spec, _line_of(text, name)
        elif isinstance(spec, dict):
            version = str(spec.get("version", ""))
            line = _line_of(text, name)
        else:
            continue
        manifest.dependencies.append(DeclaredDependency(
            name=name, spec=version, ecosystem="cargo", manifest=path, group=PROD,
            line=line, pinned_exact=is_pinned_exact(version, "cargo"),
        ))
    for name, spec in (data.get("dev-dependencies", {}) or {}).items():
        version = spec if isinstance(spec, str) else str((spec or {}).get("version", ""))
        manifest.dependencies.append(DeclaredDependency(
            name=name, spec=version, ecosystem="cargo", manifest=path, group=DEV,
            line=_line_of(text, name), pinned_exact=is_pinned_exact(version, "cargo"),
        ))
    for target in (data.get("target", {}) or {}).values():
        if not isinstance(target, dict):
            continue
        for name, spec in (target.get("dependencies", {}) or {}).items():
            version = spec if isinstance(spec, str) else str((spec or {}).get("version", ""))
            manifest.dependencies.append(DeclaredDependency(
                name=name, spec=version, ecosystem="cargo", manifest=path, group=PROD,
                line=_line_of(text, name), pinned_exact=is_pinned_exact(version, "cargo"),
            ))
    return manifest


_MAVEN_DEP = re.compile(
    r"<dependency>(?P<body>.*?)</dependency>", re.DOTALL
)
_MAVEN_FIELD = re.compile(r"<(?P<tag>groupId|artifactId|version|scope|optional)>"
                          r"(?P<value>[^<]*)</(?P=tag)>")


def parse_pom_xml(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="maven")
    for match in _MAVEN_DEP.finditer(text):
        body = match.group("body")
        fields = {m.group("tag"): m.group("value").strip()
                  for m in _MAVEN_FIELD.finditer(body)}
        group_id = fields.get("groupId", "")
        artifact = fields.get("artifactId", "")
        if not artifact:
            continue
        scope = fields.get("scope", "compile")
        group = DEV if scope in ("test", "provided") else PROD
        name = f"{group_id}:{artifact}" if group_id else artifact
        version = fields.get("version", "")
        line = text.count("\n", 0, match.start()) + 1
        manifest.dependencies.append(DeclaredDependency(
            name=name, spec=version, ecosystem="maven", manifest=path, group=group,
            line=line, pinned_exact=is_pinned_exact(version, "maven"),
        ))
    return manifest


_GRADLE_CONFIGURATIONS = {
    PROD: r"api|implementation|compile|compileOnly|runtimeOnly|annotationProcessor",
    DEV: r"testImplementation|testCompile|testRuntimeOnly|androidTestImplementation",
}
#: A declaration, capturing the argument that follows the configuration. Both
#: notations Gradle accepts arrive here:
#:   implementation 'group:artifact:version'      -> a quoted coordinate
#:   api group: 'g', name: 'a', version: '1.0'     -> key/value pairs
_GRADLE_DECL = re.compile(r"^\s*(?P<config>%s)\s*[\s(]\s*(?P<arg>[^\n]*)", re.M)
_GRADLE_SHORT = re.compile(r"""^\s*['"]([^'"]+)['"]""")
_GRADLE_PAIR = re.compile(r"""(\w+)\s*:\s*['"]([^'"]+)['"]""")


def parse_build_gradle(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="gradle")
    for group, configurations in _GRADLE_CONFIGURATIONS.items():
        pattern = re.compile(_GRADLE_DECL.pattern % configurations, re.M)
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            arg = match.group("arg")
            short = _GRADLE_SHORT.match(arg)
            if short:
                parts = short.group(1).split(":")
                _add_gradle(manifest, path, group, parts, line)
                continue
            # Map notation. Key order is not fixed by Gradle and "group" may be
            # omitted, so read the pairs rather than assuming positions.
            pairs = dict(_GRADLE_PAIR.findall(arg))
            if "name" not in pairs or "version" not in pairs:
                continue
            # An omitted group defaults to empty, so the coordinate is just the
            # artifact name; building "":name:version" would leave a leading
            # colon in the reported package.
            coordinate = [pairs["group"], pairs["name"], pairs["version"]] \
                if pairs.get("group") else [pairs["name"], pairs["version"]]
            _add_gradle(manifest, path, group, coordinate, line)
    return manifest


def _add_gradle(manifest: Manifest, path: str, group: str, parts: list[str], line: int) -> None:
    if len(parts) >= 3:
        name, version = f"{parts[0]}:{parts[1]}", parts[2]
    elif len(parts) == 2:
        name, version = parts[0], parts[1]
    else:
        name, version = parts[0], ""
    manifest.dependencies.append(DeclaredDependency(
        name=name, spec=version, ecosystem="gradle", manifest=path, group=group,
        line=line, pinned_exact=is_pinned_exact(version, "gradle"),
    ))


_GEM_RE = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""", re.M)


def parse_gemfile(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="bundler")
    dev_block = re.search(
        r"group\s+(?::\w+\s*,\s*)?:test\s+do(?P<body>.*?)\nend", text, re.DOTALL
    )
    dev_body = dev_block.group("body") if dev_block else ""

    def add(source: str, group: str, offset: int = 0) -> None:
        for match in _GEM_RE.finditer(source):
            name = match.group(1)
            spec = match.group(2) or ""
            line = text.count("\n", 0, offset + match.start()) + 1
            manifest.dependencies.append(DeclaredDependency(
                name=name, spec=spec, ecosystem="bundler", manifest=path, group=group,
                line=line, pinned_exact=is_pinned_exact(spec, "bundler"),
            ))

    dev_offset = dev_block.start("body") if dev_block else 0
    add(text, PROD)
    if dev_body:
        add(dev_body, DEV, dev_offset)
    return manifest


def parse_composer_json(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="composer")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        manifest.error = f"invalid JSON at line {exc.lineno}: {exc.msg}"
        return manifest
    for key, group in (("require", PROD), ("require-dev", DEV)):
        for name, spec in (data.get(key, {}) or {}).items():
            if name == "php" or name.startswith("ext-"):
                continue
            spec = str(spec)
            manifest.dependencies.append(DeclaredDependency(
                name=name, spec=spec, ecosystem="composer", manifest=path, group=group,
                line=_line_of(text, f'"{name}"'), pinned_exact=is_pinned_exact(spec, "composer"),
            ))
    return manifest


_YAML_DEP = re.compile(r"""^\s{2}([A-Za-z0-9_.\-]+):\s*['"]?([^'"\n]*)['"]?\s*$""")


def parse_pubspec_yaml(text: str, path: str) -> Manifest:
    manifest = Manifest(path=path, ecosystem="pub")
    section = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            section = raw.strip().rstrip(":")
            continue
        if section not in ("dependencies", "dev_dependencies"):
            continue
        match = _YAML_DEP.match(raw)
        if not match:
            continue
        name, spec = match.group(1), match.group(2).strip()
        if name in ("sdk", "flutter"):
            continue
        manifest.dependencies.append(DeclaredDependency(
            name=name, spec=spec, ecosystem="pub", manifest=path,
            group=PROD if section == "dependencies" else DEV,
            line=_line_of(text, name), pinned_exact=is_pinned_exact(spec, "pub"),
        ))
    return manifest


PARSERS = {
    "package.json": parse_package_json,
    "pyproject.toml": parse_pyproject,
    "Pipfile": parse_pipfile,
    "go.mod": parse_go_mod,
    "Cargo.toml": parse_cargo_toml,
    "pom.xml": parse_pom_xml,
    "build.gradle": parse_build_gradle,
    "build.gradle.kts": parse_build_gradle,
    "Gemfile": parse_gemfile,
    "composer.json": parse_composer_json,
    "pubspec.yaml": parse_pubspec_yaml,
}

#: Extra parsers keyed by filename prefix.
PREFIX_PARSERS = {"requirements": parse_requirements, "constraints": parse_requirements}


def parse_manifest(info: FileInfo) -> Manifest | None:
    name = info.name
    text = info.text()
    parser = PARSERS.get(name)
    if parser:
        try:
            return parser(text, info.rel)
        except Exception as exc:  # noqa: BLE001
            manifest = Manifest(path=info.rel, ecosystem="unknown")
            manifest.error = f"parser failed: {exc}"
            return manifest
    for prefix, fallback in PREFIX_PARSERS.items():
        if name.startswith(prefix) and name.endswith((".txt", ".in")):
            try:
                if fallback is parse_requirements:
                    return fallback(text, info.rel, _requirements_group(info.rel))
                return fallback(text, info.rel)
            except Exception as exc:  # noqa: BLE001
                manifest = Manifest(path=info.rel, ecosystem="python")
                manifest.error = f"parser failed: {exc}"
                return manifest
    return None


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------

@dataclass
class _Cache:
    manifests: dict[str, Manifest] = field(default_factory=dict)


def collect_dependencies(ctx: Context) -> list[DeclaredDependency]:
    """Parse every manifest in the working tree. Cached per context."""
    cache: _Cache = ctx.scratch.setdefault("_deps_cache", _Cache())
    if cache.manifests:
        out: list[DeclaredDependency] = []
        for manifest in cache.manifests.values():
            out.extend(manifest.dependencies)
        return out
    for info in ctx.files:
        if not info.is_manifest and not info.name.startswith(("requirements", "constraints")):
            continue
        manifest = parse_manifest(info)
        if manifest is not None:
            cache.manifests[info.rel] = manifest
    return [d for m in cache.manifests.values() for d in m.dependencies]


def _lockfile_for(ecosystem: str, files: set[str]) -> str | None:
    from ..walker import LOCKFILE_LANGUAGES

    for filename, eco in LOCKFILE_LANGUAGES.items():
        if eco != ecosystem:
            continue
        for candidate in files:
            if candidate == filename or candidate.endswith("/" + filename):
                return candidate
    return None


class DepsAnalyzer(Analyzer):
    dimension = Dimension.DEPS
    name = "deps"

    def available(self, ctx: Context) -> bool:
        return any(f.is_manifest or f.name.startswith("requirements") for f in ctx.files)

    def analyze(self, ctx: Context) -> Result:
        config = ctx.config
        findings: list = []
        cache: _Cache = ctx.scratch.setdefault("_deps_cache", _Cache())
        if not cache.manifests:
            collect_dependencies(ctx)

        manifests = cache.manifests
        deps = [d for m in manifests.values() for d in m.dependencies]
        all_paths = {f.rel for f in ctx.files} | {f.name for f in ctx.files}
        ecosystems = sorted({d.ecosystem for d in deps})

        # -- parse errors ---------------------------------------------------
        for path, manifest in sorted(manifests.items()):
            if manifest.error:
                findings.append(make_finding(
                    "deps.parse_error",
                    detail=f"{path}: {manifest.error}",
                    confidence=Confidence.HIGH,
                    locations=[Location(path)],
                ))

        # -- lockfiles ------------------------------------------------------
        lock_presence: dict[str, str | None] = {}
        for ecosystem in ecosystems:
            lock = _lockfile_for(ecosystem, all_paths)
            lock_presence[ecosystem] = lock
            if lock is None:
                continue
            manifest_paths = [p for p, m in manifests.items() if m.ecosystem == ecosystem]
            for manifest_path in manifest_paths:
                stale = self._lock_is_stale(ctx, manifest_path, lock)
                if stale is None:
                    continue
                days, detail = stale
                if days > 1.0:
                    findings.append(make_finding(
                        "deps.stale_lock",
                        detail=(f"{lock} was last modified {days:.0f} days before "
                                f"{manifest_path}, so the pinned versions may no longer "
                                f"match the manifest ({detail})."),
                        evidence=[f"lock: {lock}", f"manifest: {manifest_path}"],
                        locations=[Location(lock), Location(manifest_path)],
                    ))

        for ecosystem in ecosystems:
            declared = [d for d in deps if d.ecosystem == ecosystem]
            if lock_presence.get(ecosystem) is None and any(
                d.group == PROD for d in declared
            ):
                count = len(declared)
                findings.append(make_finding(
                    "deps.missing_lock",
                    detail=(f"No lockfile found for the {ecosystem} ecosystem "
                            f"({count} {'dependency' if count == 1 else 'dependencies'} "
                            f"declared), so installs are not reproducible."),
                    locations=[Location(".")],
                ))

        # -- pinning --------------------------------------------------------
        unpinned: list[DeclaredDependency] = []
        for dep in deps:
            if dep.group != PROD:
                continue
            if not dep.spec:
                continue
            if is_range(dep.spec, dep.ecosystem) and not dep.pinned_exact:
                unpinned.append(dep)
        if unpinned:
            by_eco: dict[str, list[DeclaredDependency]] = defaultdict(list)
            for dep in unpinned:
                by_eco[dep.ecosystem].append(dep)
            for ecosystem, group in sorted(by_eco.items()):
                share = len(group) / max(1, len([d for d in deps if d.ecosystem == ecosystem]))
                severity = Severity.MEDIUM if share < 0.5 else Severity.HIGH
                findings.append(make_finding(
                    "deps.unpinned_prod",
                    detail=(f"{len(group)} of the {ecosystem} production dependencies use "
                            f"version ranges rather than exact versions "
                            f"({share:.0%} of the ecosystem's dependencies)."),
                    evidence=[f"{d.name}{d.spec}" for d in group[:10]],
                    locations=[Location(d.manifest, d.line or None) for d in group[:20]],
                    severity=severity,
                    occurrences=len(group),
                ))

        # -- version skew ---------------------------------------------------
        by_name: dict[str, list[DeclaredDependency]] = defaultdict(list)
        for dep in deps:
            key = _skew_key(dep)
            by_name[key].append(dep)
        for key, group in sorted(by_name.items()):
            versions = {d.version for d in group if d.version}
            manifests_touched = {d.manifest for d in group}
            if len(versions) < 2 or len(manifests_touched) < 2:
                continue
            findings.append(make_finding(
                "deps.version_skew",
                detail=(f"{key} is declared at {len(versions)} different versions "
                        f"across {len(manifests_touched)} manifests: "
                        f"{', '.join(sorted(v for v in versions if v)[:6])}."),
                evidence=[f"{d.manifest}: {d.name}{d.spec}" for d in group[:8]],
                locations=[Location(d.manifest, d.line or None) for d in group[:10]],
                occurrences=len(group),
            ))

        # -- duplicates within a manifest ------------------------------------
        per_manifest: dict[str, list[DeclaredDependency]] = defaultdict(list)
        for dep in deps:
            per_manifest[dep.manifest].append(dep)
        for path, group in sorted(per_manifest.items()):
            names = defaultdict(list)
            for dep in group:
                names[_skew_key(dep)].append(dep)
            for key, entries in names.items():
                if len(entries) < 2:
                    continue
                if len({d.spec for d in entries}) < 2:
                    continue  # identical duplicates in one file: harmless
                findings.append(make_finding(
                    "deps.duplicate_declaration",
                    detail=f"{key} is declared {len(entries)} times in {path} with "
                           f"different constraints.",
                    evidence=[d.spec for d in entries[:6]],
                    locations=[Location(path, d.line or None) for d in entries[:6]],
                    occurrences=len(entries),
                ))

        # -- dev dependency leakage -----------------------------------------
        for path, group in sorted(per_manifest.items()):
            for dep in group:
                if dep.group == PROD and _looks_like_test_only(dep.name):
                    findings.append(make_finding(
                        "deps.dev_in_prod",
                        detail=(f"{dep.name} is declared in the production set of "
                                f"{path} but is a test-only package."),
                        locations=[Location(path, dep.line or None)],
                    ))

        # -- online intelligence ---------------------------------------------
        unavailable: dict[str, str] = {}
        online_notes: dict[str, Any] = {}
        if not ctx.online:
            if deps:
                unavailable["advisories"] = "requires --online (OSV.dev advisory lookup)"
                unavailable["release_age"] = "requires --online"
        else:
            online_notes = self._online(ctx, deps, findings)

        prod_count = len([d for d in deps if d.group == PROD])
        dev_count = len([d for d in deps if d.group == DEV])
        optional_count = len([d for d in deps if d.group == OPTIONAL])

        metrics: dict[str, Any] = {
            "manifests": sorted(manifests.keys()),
            "manifest_count": len(manifests),
            "ecosystems": ecosystems,
            "dependencies_total": len(deps),
            "dependencies_prod": prod_count,
            "dependencies_dev": dev_count,
            "dependencies_optional": optional_count,
            "lockfiles": {k: v for k, v in lock_presence.items()},
            "unpinned_prod_count": len(unpinned),
            "parse_failures": [p for p, m in manifests.items() if m.error],
        }
        if online_notes:
            metrics["online"] = online_notes

        return Result(findings=findings, metrics=metrics, unavailable=unavailable)

    # -- helpers -----------------------------------------------------------
    def _lock_is_stale(self, ctx: Context, manifest_path: str, lock_path: str):
        if ctx.git.available:
            history = ctx.git.history(3650)
            manifest_entry = history.files.get(manifest_path)
            lock_entry = history.files.get(lock_path)
            if manifest_entry and lock_entry:
                if manifest_entry.last_timestamp > lock_entry.last_timestamp:
                    days = (manifest_entry.last_timestamp - lock_entry.last_timestamp) / 86400.0
                    return days, "manifest is newer in commit history"
                return None
        manifest_file = ctx.root / manifest_path
        lock_file = ctx.root / lock_path
        try:
            delta = manifest_file.stat().st_mtime - lock_file.stat().st_mtime
        except OSError:
            return None
        if delta <= 0:
            return None
        return delta / 86400.0, "compared by file modification time"

    def _online(self, ctx: Context, deps: list[DeclaredDependency], findings: list) -> dict[str, Any]:
        from .osv import enrich_dependencies

        return enrich_dependencies(ctx, deps, findings, make_finding)


_TEST_ONLY_NAMES = {
    "pytest", "pytest-cov", "pytest-mock", "mock", "faker", "factory-boy", "freezegun",
    "hypothesis", "nose", "nose2", "tox", "coverage", "unittest2", "responses", "vcrpy",
    "jest", "vitest", "mocha", "chai", "sinon", "supertest", "cypress", "enzyme",
    "@types/jest", "@types/mocha", "ts-jest", "karma", "jasmine-core", "nock",
    "junit", "testng", "mockito", "junit-jupiter", "assertj", "rest-assured",
    "rspec", "rspec-rails", "capybara", "factory_bot", "shoulda-matchers", "vcr",
    "testcontainers", "phpunit", "phpunit/phpunit", "mockery", "codeception",
    "cucumber", "ginkgo", "gomega", "testify", "httptest", "quicktest", "gtest",
    "gmock", "catch2", "conan-test", "xunit", "NUnit", "AwesomeAssertions",
}


def _looks_like_test_only(name: str) -> bool:
    base = name.split("/")[-1].lower()
    if base in _TEST_ONLY_NAMES:
        return True
    return any(base.startswith(prefix) for prefix in ("pytest-", "jest-", "ts-jest"))


def _skew_key(dep: DeclaredDependency) -> str:
    """Normalize a name so the same package across ecosystems compares equal."""
    name = dep.name
    if dep.ecosystem in ("npm", "composer", "pub"):
        name = name.split("/")[-1] if dep.ecosystem == "npm" else name
        name = name.lower()
    if dep.ecosystem == "maven":
        parts = name.split(":")
        name = parts[-1] if parts else name
    if dep.ecosystem == "python":
        name = name.lower().replace("_", "-")
        name = re.sub(r"\[.*\]", "", name)
    return f"{name}"
