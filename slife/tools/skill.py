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


def get_skills_summary(skills_dir: str | Path = "skills") -> str:
    """Scan skills_dir and return name + description for each skill.

    Only directories containing a SKILL.md are considered valid skills.
    Returns empty string if no skills are found.
    """
    skills_dir = Path(skills_dir)
    if not skills_dir.exists():
        return ""

    entries = sorted(
        d for d in skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    )
    if not entries:
        return ""

    lines = []
    for d in entries:
        content = (d / "SKILL.md").read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(content)
        name = fm.get("name", d.name)
        desc = fm.get("description", "(no description)")
        lines.append(f"- **{name}**: {desc}")

    return "\n".join(lines)


def _list_skills(skills_dir: Path) -> str:
    """Legacy wrapper for Tool class."""
    result = get_skills_summary(skills_dir)
    return result if result else "No skills available."


def _read_skill(skills_dir: Path, skill_name: str) -> str:
    """Find and return the full SKILL.md content for a named skill.

    Matches by frontmatter 'name' field first, then by directory name.
    """
    if not skills_dir.exists():
        return f"Skills directory not found: {skills_dir}"

    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.exists():
            continue

        content = md.read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(content)
        if fm.get("name") == skill_name or d.name == skill_name:
            logger.info("Loaded skill: %s", skill_name)
            return content

    # Build hint with available names
    available = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.exists():
            continue
        fm, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
        available.append(f"  - {fm.get('name', d.name)}")

    hint = "\n".join(available) if available else "  (none)"
    return f"Skill '{skill_name}' not found.\n\nAvailable skills:\n{hint}"


class ListSkillsTool(Tool):
    """List all available skills with their names and descriptions."""

    name = "list_skills"
    description = (
        "List all available skills (natural-language operation manuals). "
        "Each skill is a how-to guide the assistant can follow — some "
        "provide step-by-step instructions, others include executable "
        "commands. Use this to discover what capabilities are available "
        "before loading a specific skill with use_skill."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)

    async def execute(self) -> str:
        return _list_skills(self.skills_dir)


class UseSkillTool(Tool):
    """Load a specific skill's full documentation into context."""

    name = "use_skill"
    description = (
        "Load a skill's complete manual into the conversation context. "
        "The returned document tells you what to do — it may include "
        "step-by-step instructions, commands to run via execute_shell, "
        "conventions to follow, or reference material. "
        "Call list_skills first to see what skills are available, "
        "then use_skill with the skill name to load the one you need."
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Name of the skill to load, as shown by list_skills.",
            },
        },
        "required": ["skill_name"],
    }

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)

    async def execute(self, skill_name: str) -> str:
        return _read_skill(self.skills_dir, skill_name)
