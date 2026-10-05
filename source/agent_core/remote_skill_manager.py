"""Resolve and package explicitly requested local skills for RemoteAgent."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .remote_binding import normalize_skill_path


MAX_SKILL_FILES = 32
MAX_SKILL_BYTES = 512 * 1024
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# A skill is an explicit, standalone slash command only.  Do not infer a
# skill from ordinary prose (for example, "使用 precise-trace") or from
# legacy "$skill" syntax.  The whitespace boundary also prevents URLs and
# Windows paths from being interpreted as commands.
_SLASH_INVOCATION = re.compile(
    rf"(?<!\S)/(?P<name>{_NAME})(?![A-Za-z0-9._-])"
)
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+\.md(?:#[^)]+)?)\)", re.IGNORECASE)


class RemoteSkillError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteSkillPackage:
    name: str
    root: str
    files: tuple[str, ...]
    sha256: str
    bundle_path: str

    def prompt_metadata(self) -> dict:
        return {
            "name": self.name,
            "scope": "request_only",
            "delivery": "markdown_attachment",
            "bundle": Path(self.bundle_path).name,
            "sha256": self.sha256,
            "sources": list(self.files),
        }


class RemoteSkillManager:
    def __init__(self, skill_path: str | Path):
        self.skill_path = Path(
            normalize_skill_path(skill_path, require_exists=True)
        )

    def list_skills(self) -> list[str]:
        return sorted(
            child.name
            for child in self.skill_path.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        )

    @staticmethod
    def looks_like_invocation(request: str) -> bool:
        return bool(_SLASH_INVOCATION.search(str(request or "")))

    def requested_skill(self, request: str) -> str:
        text = str(request or "")
        names = list(dict.fromkeys(
            match.group("name")
            for match in _SLASH_INVOCATION.finditer(text)
        ))
        if not names:
            return ""
        available = set(self.list_skills())
        # An unknown slash token is ordinary user text.  This is intentional:
        # only an exact existing skill tag is a skill invocation.
        names = [name for name in names if name in available]
        if not names:
            return ""
        if len(names) > 1:
            raise RemoteSkillError(
                "remote_skill_multiple_not_supported:" + ",".join(names)
            )
        name = names[0]
        return name

    def _resolve_resource(self, skill_root: Path, raw: str) -> Path | None:
        value = str(raw or "").strip().strip("<>").split("#", 1)[0]
        if not value or "://" in value:
            return None
        candidate = (skill_root / value).resolve()
        try:
            candidate.relative_to(skill_root)
        except ValueError as exc:
            raise RemoteSkillError(f"remote_skill_resource_escape:{value}") from exc
        if candidate.suffix.lower() != ".md" or not candidate.is_file():
            return None
        return candidate

    def _collect(self, name: str) -> list[tuple[str, str]]:
        if not re.fullmatch(_NAME, str(name or "")):
            raise RemoteSkillError(f"remote_skill_name_invalid:{name}")
        skill_root = (self.skill_path / name).resolve()
        try:
            skill_root.relative_to(self.skill_path)
        except ValueError as exc:
            raise RemoteSkillError(f"remote_skill_path_escape:{name}") from exc
        entry = skill_root / "SKILL.md"
        if not entry.is_file():
            raise RemoteSkillError(f"remote_skill_not_found:{name}")

        pending = [entry]
        seen: set[Path] = set()
        output: list[tuple[str, str]] = []
        total = 0
        while pending:
            path = pending.pop(0).resolve()
            if path in seen:
                continue
            seen.add(path)
            if len(seen) > MAX_SKILL_FILES:
                raise RemoteSkillError("remote_skill_file_limit_exceeded")
            content = path.read_text(encoding="utf-8")
            total += len(content.encode("utf-8"))
            if total > MAX_SKILL_BYTES:
                raise RemoteSkillError("remote_skill_size_limit_exceeded")
            relative = path.relative_to(skill_root).as_posix()
            output.append((relative, content))
            for raw in _MARKDOWN_LINK.findall(content):
                resource = self._resolve_resource(skill_root, raw)
                if resource is not None and resource not in seen:
                    pending.append(resource)
        return output

    def package(
        self, name: str, *, output_dir: str | Path, request_id: str
    ) -> RemoteSkillPackage:
        sources = self._collect(name)
        digest = hashlib.sha256()
        sections = [
            "# RemoteAgent Skill Context",
            "",
            f"- Skill: `{name}`",
            "- Scope: current request only",
            "- Instruction: Treat the following skill documents as authoritative for this request.",
        ]
        for relative, content in sources:
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content.encode("utf-8"))
            digest.update(b"\0")
            sections.extend(("", f"## Source: `{relative}`", "", content.rstrip()))
        sha256 = digest.hexdigest()
        safe_request = re.sub(r"[^A-Za-z0-9._-]", "_", str(request_id or "request"))
        target_dir = Path(output_dir).resolve() / safe_request
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{name}.skill-context.md"
        target.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
        return RemoteSkillPackage(
            name=name,
            root=str(self.skill_path),
            files=tuple(relative for relative, _content in sources),
            sha256=sha256,
            bundle_path=str(target),
        )


__all__ = [
    "MAX_SKILL_BYTES", "MAX_SKILL_FILES", "RemoteSkillError",
    "RemoteSkillManager", "RemoteSkillPackage",
]
