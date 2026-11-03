"""Bounded, ecosystem-aware dependency requirement parsing and probe routing.

The parser never grants authorization. Unsupported ecosystems return UNKNOWN:
a Python distribution probe must not silently stand in for C++ or Java.
Adapters observe only Runtime-owned facts; execution stays with V9 tools.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Mapping

_VERSION = r"[0-9]+(?:\.[0-9]+){0,3}"
_NAME = r"[a-zA-Z][a-zA-Z0-9_.-]{0,65}"
_TOOL_NAMES = (
    "python", "git", "cmake", "gcc", "g++", "clang", "clang++",
    "java", "javac", "mvn", "gradle",
)
_TOOL_RE = re.compile(
    r"(?<![a-zA-Z0-9_.-])(" + "|".join(re.escape(x) for x in sorted(_TOOL_NAMES, key=len, reverse=True))
    + r")\s*(==|>=|<=)\s*(" + _VERSION + r")(?![a-zA-Z0-9_.-])", re.I,
)
_QUALIFIED_RE = re.compile(
    r"(?<![a-zA-Z0-9_.-])(python|pip|cpp|c\+\+|cmake|java|maven|gradle)\s*:"
    r"\s*(" + _NAME + r"(?::" + _NAME + r")?)\s*(==|>=|<=)\s*("
    + _VERSION + r")(?![a-zA-Z0-9_.-])", re.I,
)
_PACKAGE_RE = re.compile(
    r"(?:\b(?:package|dependency|distribution)\s+|(?:套件|相依套件)\s*)"
    r"(" + _NAME + r")\s*(==|>=|<=)\s*(" + _VERSION + r")", re.I,
)
_BARE_RE = re.compile(
    r"(?<![a-zA-Z0-9_.:-])(" + _NAME + r")\s*(==|>=|<=)\s*("
    + _VERSION + r")(?![a-zA-Z0-9_.-])", re.I,
)
_ECOSYSTEM = {
    "python": "python", "pip": "python",
    "cpp": "cpp", "c++": "cpp", "cmake": "cpp",
    "java": "java", "maven": "java", "gradle": "java",
}


@dataclass(frozen=True)
class DependencyRequirement:
    ecosystem: str
    name: str
    operator: str
    required_version: str
    kind: str = "PACKAGE"

    @property
    def fact_id(self) -> str:
        if self.kind == "TOOL":
            return "version:" + self.name
        if self.ecosystem == "python":
            return "package:" + self.name
        return "dependency:" + self.ecosystem + ":" + self.name


def parse_requirements(request: str, *, limit: int = 20) -> list[DependencyRequirement]:
    """Parse explicit ecosystem qualifiers first; retain legacy bare pip syntax.

    Bare version constraints are Python distributions for backward compatibility
    with existing Live A requests. C++/Java packages require an explicit
    ecosystem qualifier to prevent falsely reporting Python metadata as theirs.
    """
    text = str(request or "")
    matches: list[tuple[int, int, DependencyRequirement]] = []
    occupied: list[tuple[int, int]] = []

    def add(match: re.Match, row: DependencyRequirement) -> None:
        span = match.span()
        if any(span[0] < hi and lo < span[1] for lo, hi in occupied):
            return
        occupied.append(span)
        matches.append((span[0], span[1], row))

    for match in _TOOL_RE.finditer(text):
        add(match, DependencyRequirement("tool", match.group(1).lower(),
                                         match.group(2), match.group(3), "TOOL"))
    for match in _QUALIFIED_RE.finditer(text):
        ecosystem = _ECOSYSTEM[match.group(1).lower()]
        add(match, DependencyRequirement(ecosystem, match.group(2).lower(),
                                         match.group(3), match.group(4)))
    for pattern in (_PACKAGE_RE, _BARE_RE):
        for match in pattern.finditer(text):
            add(match, DependencyRequirement("python", match.group(1).lower(),
                                             match.group(2), match.group(3)))
    # For legacy unqualified names, infer only a uniquely identified
    # ecosystem. If several language toolchains are mentioned, fail closed
    # as UNKNOWN instead of misreporting a C++/Java dependency as pip-missing.
    contexts = set()
    if re.search(r"(?i)\b(?:python|pip|wheel)\b|requirements[.]txt", text):
        contexts.add("python")
    if re.search(r"(?i)c\+\+|\b(?:cmake|vcpkg|conan)\b", text):
        contexts.add("cpp")
    if re.search(r"(?i)\b(?:java|maven|gradle|javac)\b|pom[.]xml|build[.]gradle", text):
        contexts.add("java")
    implied = next(iter(contexts)) if len(contexts) == 1 else (
        "unknown" if len(contexts) > 1 else "python"
    )
    unique: list[DependencyRequirement] = []
    seen: set[str] = set()
    for _start, _end, row in sorted(matches, key=lambda item: item[0]):
        if row.kind == "PACKAGE" and row.ecosystem == "python":
            # Explicit package syntax is not itself a Python ecosystem claim.
            # Fully qualified constraints retain the ecosystem parsed above.
            raw = text[_start:_end].lower()
            if not re.match(r"\s*(python|pip)\s*:", raw):
                row = DependencyRequirement(implied, row.name, row.operator,
                                            row.required_version)
        if row.fact_id not in seen:
            seen.add(row.fact_id)
            unique.append(row)
        if len(unique) >= limit:
            break
    return unique


def probe_dependency(
    requirement: DependencyRequirement, *,
    python_probe: Callable[[str], tuple[str, str]],
) -> tuple[str, str, str]:
    """Route a safe read-only probe; unknown adapters fail closed."""
    if requirement.ecosystem == "python":
        status, version = python_probe(requirement.name)
        return status, version, "BUNDLED_PYTHON_METADATA_PROBE"
    return "UNKNOWN", "", "UNSUPPORTED_ECOSYSTEM_PROBE"


def tool_snapshot_status(
    requirement: DependencyRequirement,
    environment_tools: Mapping[str, object],
    *, version_satisfies: Callable[[str, str, str], bool],
    version_extract: Callable[[str], str],
) -> tuple[str, str]:
    """No subprocess: Runtime-owned environment snapshot is the only authority."""
    entry = environment_tools.get(requirement.name, {})
    entry = entry if isinstance(entry, Mapping) else {}
    observed = version_extract(str(entry.get("version", "")))
    status = str(entry.get("status", "UNKNOWN")).upper()
    if status == "MISSING":
        return "MISSING", observed
    if status != "AVAILABLE" or not observed:
        return "UNKNOWN", observed
    return ("SATISFIED" if version_satisfies(observed, requirement.operator,
                                             requirement.required_version)
            else "MISSING"), observed


def classify_remediation(command: str) -> str | None:
    """Identify candidate dependency remediation, never authorize it.

    Only Python pip has a working PR-C/D adapter in this revision. Unsupported
    ecosystems cannot proceed through the Python package recovery lane.
    """
    if re.search(r"(?i)(?:\bpip(?:\d+)?\s+install\b|"
                 r"\b(?:python|py)(?:\.exe)?\s+-m\s+pip\s+install\b)", command):
        return "python"
    if re.search(r"(?i)\b(?:vcpkg|conan)\s+install\b", command):
        return "cpp"
    if re.search(r"(?i)\b(?:mvn|mvnw)\s+dependency:get\b", command):
        return "java"
    return None
