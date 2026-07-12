"""Skill tools — 自然语言操作手册的渐进式披露.

list_skills: 列出所有可用 skill 的名称和描述
use_skill:   加载指定 skill 的完整文档到上下文
"""

import logging
from pathlib import Path

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a SKILL.md file.

    Expects:
        ---
        name: xxx
        description: xxx
        ---
        markdown body...

    Returns (frontmatter_dict, body_text).
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, content

    end = 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1

    if end >= len(lines):
        return {}, content

    fm = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip() or fm.get(key.strip(), "")

    body = "\n".join(lines[end + 1 :]).strip()
    return fm, body


def _iter_skills(skills_dir: Path) -> list[tuple[Path, dict, str]]:
    """Scan skills_dir and return (directory, frontmatter, body) for each skill.

    Only directories containing a SKILL.md are considered valid skills.
    Returns empty list if skills_dir does not exist.
    """
    if not skills_dir.exists():
        return []

    result = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.exists():
            continue
        content = md.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(content)
        result.append((d, fm, body))
    return result


def get_skills_summary(skills_dir: str | Path = "skills") -> str:
    """Scan skills_dir and return name + description for each skill.

    Only directories containing a SKILL.md are considered valid skills.
    Returns empty string if no skills are found.
    """
    skills = _iter_skills(Path(skills_dir))
    if not skills:
        return ""

    lines = []
    for d, fm, _body in skills:
        name = fm.get("name", d.name)
        desc = fm.get("description", "(no description)")
        lines.append(f"- **{name}**: {desc}")

    return "\n".join(lines)


def _read_skill(skills_dir: Path, skill_name: str) -> str:
    """Find and return the full SKILL.md content for a named skill.

    Matches by frontmatter 'name' field first, then by directory name.
    """
    skills = _iter_skills(skills_dir)
    if not skills:
        return f"Skills directory not found: {skills_dir}"

    for d, fm, _body in skills:
        if fm.get("name") == skill_name or d.name == skill_name:
            content = (d / "SKILL.md").read_text(encoding="utf-8")
            logger.info("Loaded skill: %s", skill_name)
            return content

    # Build hint with available names
    available = [f"  - {fm.get('name', d.name)}" for d, fm, _body in skills]
    hint = "\n".join(available) if available else "  (none)"
    return f"Skill '{skill_name}' not found.\n\nAvailable skills:\n{hint}"


class ListSkillsTool(Tool):
    """List all available skills with their names and descriptions."""

    name = "list_skills"
    description = "List available skills and their descriptions."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)

    async def execute(self, **kwargs) -> str:
        result = get_skills_summary(self.skills_dir)
        return result if result else "No skills available."


class UseSkillTool(Tool):
    """Load a specific skill's full documentation into context."""

    name = "use_skill"
    description = "Load a skill's full instructions into context."
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill name from list_skills",
            },
        },
        "required": ["skill_name"],
    }

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)

    async def execute(self, **kwargs) -> str:
        skill_name: str = kwargs["skill_name"]
        return _read_skill(self.skills_dir, skill_name)
