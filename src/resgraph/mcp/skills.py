"""Investigation playbooks as MCP prompts.

A skill that references a tool the registry doesn't know, or skips a
required section, fails at startup — never silently at runtime.

Decisions: D21 (SPEC.md).
"""

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from resgraph.tools.registry import TOOL_REGISTRY

REQUIRED_SECTIONS = (
    "## Goal",
    "## When to use",
    "## Steps",
    "## Tools to call",
    "## Examples",
    "## Anti-patterns",
)


class SkillError(Exception):
    pass


class SkillManifest(BaseModel):
    name: str
    version: str
    description: str
    scope: str
    tool_refs: list[str]


@dataclass(frozen=True)
class Skill:
    manifest: SkillManifest
    body: str


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise SkillError(f"{path}: missing YAML frontmatter")
    try:
        _, fm, body = text.split("---\n", 2)
    except ValueError as e:
        raise SkillError(f"{path}: unterminated frontmatter") from e
    return fm, body


def load_skill(path: Path) -> Skill:
    fm, body = _split_frontmatter(path.read_text(), path)
    try:
        manifest = SkillManifest.model_validate(yaml.safe_load(fm))
    except (ValidationError, yaml.YAMLError) as e:
        raise SkillError(f"{path}: invalid manifest: {e}") from e
    known = {entry.name for entry in TOOL_REGISTRY}
    unknown = [t for t in manifest.tool_refs if t not in known]
    if unknown:
        raise SkillError(
            f"{path}: tool_refs name tools the registry does not know: {unknown} "
            f"(known: {sorted(known)})"
        )
    positions = [body.find(s) for s in REQUIRED_SECTIONS]
    missing = [s for s, i in zip(REQUIRED_SECTIONS, positions, strict=True) if i < 0]
    if missing:
        raise SkillError(f"{path}: missing required sections: {missing}")
    if positions != sorted(positions):
        raise SkillError(f"{path}: sections out of fixed order {REQUIRED_SECTIONS}")
    return Skill(manifest=manifest, body=body)


def load_skills(root: Path) -> list[Skill]:
    return [load_skill(p) for p in sorted(root.glob("*/SKILL.md"))]
