"""Skill tools — 自然语言操作手册的渐进式披露.

list_skills:   列出所有可用 skill 的名称和描述
use_skill:     加载指定 skill 的完整文档到上下文
add_skill:     从远程 URL 安装 skill（拉取文件写入 skills 目录）
remove_skill:  删除一个 skill 目录及其内容
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

    fm: dict[str, str] = {}
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


class AddSkillTool(Tool):
    """Install a skill by writing its files to the local skills directory.

    The agent is responsible for fetching the skill's files (e.g. via
    GitHub MCP, fetch MCP, or other tools). This tool just writes them
    to disk.

    Two input modes:
      - files: list of {path, content} dicts (use with GitHub MCP)
      - archive: base64-encoded .zip or .tar.gz (use with fetch MCP)

    After installation, list_skills and use_skill pick it up immediately
    (the skills directory is re-scanned on every call).
    """

    name = "add_skill"
    description = (
        "Write a skill folder to the local skills directory. "
        "Provide either individual files or a base64-encoded archive. "
        "The skill is discovered by list_skills on the next call."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Local directory name. Lowercase kebab-case (e.g. 'browser-use').",
            },
            "files": {
                "type": "array",
                "description": "Skill files as [{path, content}]. At minimum include SKILL.md "
                "with valid YAML frontmatter (name + description).",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to the skill root (e.g. 'SKILL.md', 'scripts/run.py').",
                        },
                        "content": {
                            "type": "string",
                            "description": "File content as a string.",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
            "archive": {
                "type": "string",
                "description": "Base64-encoded .zip or .tar.gz. Decoded and extracted into skills/<name>/.",
            },
        },
        "required": ["name"],
    }

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        files: list[dict] | None = kwargs.get("files")
        archive_b64: str | None = kwargs.get("archive")

        if not files and not archive_b64:
            return (
                "[FAIL] Either 'files' or 'archive' is required.\n"
                "  - files: list of {path, content} (use with GitHub MCP)\n"
                "  - archive: base64-encoded .zip/.tar.gz (use with fetch MCP)"
            )
        if files and archive_b64:
            return "[FAIL] Provide 'files' or 'archive', not both."

        skill_dir = self.skills_dir / name

        if skill_dir.exists():
            return (
                f"Skill '{name}' already exists at {skill_dir}.\n"
                f"Use remove_skill first if you want to replace it."
            )

        skill_dir.mkdir(parents=True, exist_ok=True)

        try:
            if archive_b64:
                return self._install_from_archive(name, archive_b64, skill_dir)
            else:
                return self._install_from_files(name, files, skill_dir)
        except Exception as e:
            import shutil
            shutil.rmtree(skill_dir, ignore_errors=True)
            logger.exception("Failed to install skill '%s'", name)
            return f"[FAIL] Error installing skill '{name}': {e}"

    def _install_from_files(self, name: str, files: list[dict], skill_dir: Path) -> str:
        """Write individual files to the skill directory."""
        count = 0
        for f in files:
            file_path = skill_dir / f["path"]
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(f["content"], encoding="utf-8")
            count += 1
            logger.debug("Wrote: %s", f["path"])

        has_skill_md = (skill_dir / "SKILL.md").exists()
        msg = f"[OK] Installed skill '{name}' ({count} files) → {skill_dir}"
        if not has_skill_md:
            msg += (
                "\n[WARN] No SKILL.md found. list_skills will not discover "
                "this skill until a SKILL.md with proper frontmatter is added."
            )
        return msg

    def _install_from_archive(self, name: str, archive_b64: str, skill_dir: Path) -> str:
        """Decode and extract a base64-encoded archive into the skill directory."""
        import base64
        import io
        import zipfile
        import tarfile

        data = base64.b64decode(archive_b64)
        bio = io.BytesIO(data)

        # Detect format by magic bytes
        if data[:2] == b'PK':
            with zipfile.ZipFile(bio) as zf:
                zf.extractall(skill_dir)
        elif data[:2].hex() == '1f8b':  # gzip magic
            with tarfile.open(fileobj=bio, mode="r:gz") as tf:
                tf.extractall(skill_dir)
        else:
            raise ValueError("Unknown archive format (expected .zip or .tar.gz)")

        # Flatten single wrapper directory if it contains SKILL.md
        entries = list(skill_dir.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            wrapper = entries[0]
            if (wrapper / "SKILL.md").exists():
                import shutil
                for item in wrapper.iterdir():
                    shutil.move(str(item), str(skill_dir / item.name))
                wrapper.rmdir()

        has_skill_md = (skill_dir / "SKILL.md").exists()
        msg = f"[OK] Installed skill '{name}' from archive → {skill_dir}"
        if not has_skill_md:
            msg += (
                "\n[WARN] No SKILL.md found. list_skills will not discover "
                "this skill until a SKILL.md with proper frontmatter is added."
            )
        return msg


class RemoveSkillTool(Tool):
    """Remove a skill by deleting its directory and SKILL.md.

    Matches by frontmatter 'name' field first, then by directory name.
    """

    name = "remove_skill"
    description = (
        "Remove a skill and its SKILL.md file. Use list_skills to see "
        "what's available. This deletes the skill directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Name of the skill to remove (from list_skills).",
            },
        },
        "required": ["skill_name"],
    }

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)

    async def execute(self, **kwargs) -> str:
        skill_name: str = kwargs["skill_name"]

        # 1) Try matching via _iter_skills (directories with SKILL.md)
        skills = _iter_skills(self.skills_dir)
        for d, fm, _body in skills:
            if fm.get("name") == skill_name or d.name == skill_name:
                import shutil
                shutil.rmtree(d)
                logger.info("Removed skill: %s (dir: %s)", skill_name, d)
                return f"[OK] Removed skill '{skill_name}' (deleted {d})."

        # 2) Try matching by directory name directly (handles git clones
        #    or archives that lack SKILL.md)
        direct = self.skills_dir / skill_name
        if direct.exists() and direct.is_dir():
            import shutil
            shutil.rmtree(direct)
            logger.info("Removed directory: %s (no SKILL.md)", direct)
            return (
                f"[OK] Removed directory '{skill_name}' ({direct}).\n"
                f"Note: it had no SKILL.md — may not have been a valid skill."
            )

        # 3) Not found — list what's available
        available = [f"  - {fm.get('name', d.name)}" for d, fm, _body in skills]
        # Also list directories without SKILL.md
        if self.skills_dir.exists():
            for item in sorted(self.skills_dir.iterdir()):
                if item.is_dir() and not (item / "SKILL.md").exists():
                    available.append(f"  - {item.name} (no SKILL.md)")
        hint = "\n".join(available) if available else "  (none)"
        return f"Skill '{skill_name}' not found.\n\nAvailable skills/directories:\n{hint}"
