"""Skill tools — progressive disclosure of natural-language playbooks.

skill_list:   list all available skills' names and descriptions
skill_use:    load a skill's full documentation into context
skill_set:   install/update a skill from a remote URL (fetch files into the skills dir)
skill_remove: delete a skill directory and its contents
"""

import json
import logging
import zipfile
from pathlib import Path
from typing import ClassVar

from slife.tools._config_io import format_source_info, read_config, with_fetched_at, write_config
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


def _ensure_within(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and require it to stay under ``base``.

    Raises ``ValueError`` on any path that escapes ``base`` (path traversal,
    absolute paths, ``..`` segments).  Used to sandbox skill file writes,
    archive extraction targets, and deletion targets to the skills root.
    """
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Path escapes '{base}': {candidate}")
    return resolved


def _extract_zip_safely(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip archive, refusing entries that escape ``dest``.

    ``zipfile`` has no ``filter=`` protection (unlike tarfile's PEP 706
    ``filter="data"``), so validate every member manually: absolute paths,
    ``..`` traversal, and symlinks are rejected.  Raises ``ValueError`` on
    the first unsafe member, leaving the caller to clean up.
    """
    import shutil
    import stat as _stat

    dest_resolved = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if not target.is_relative_to(dest_resolved):
            raise ValueError(f"Archive entry escapes '{dest}': {member!r}")
        info = zf.getinfo(member)
        # Refuse symlinks — their target could point anywhere.
        if _stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"Archive entry is a symlink, refusing: {member!r}")
        if member.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _disabled_skill_names(config_path: Path | None) -> set[str]:
    """Return skill names disabled via ``skill_set_enabled`` — the ``skills:``
    config section entries with ``enabled: false``. Empty set when nothing is
    disabled or the config is unreadable."""
    if config_path is None:
        return set()
    try:
        raw = read_config(config_path)
    except Exception:
        return set()
    skills = raw.get("skills", {})
    if not isinstance(skills, dict):
        return set()
    return {
        n for n, e in skills.items()
        if isinstance(e, dict) and e.get("enabled") is False
    }


def get_skills_summary(
    skills_dir: str | Path = "skills",
    disabled: set[str] | None = None,
) -> str:
    """Scan skills_dir and return name + description for each skill.

    Only directories containing a SKILL.md are considered valid skills.
    *disabled* names (from skill_set_enabled) are omitted.
    Returns empty string if no skills are found.
    """
    skills = _iter_skills(Path(skills_dir))
    if not skills:
        return ""
    disabled = disabled or set()

    lines = [f"> **Skills root:** `{Path(skills_dir).resolve()}` — use this path for skill scripts.\n"]
    for d, fm, _body in skills:
        name = fm.get("name", d.name)
        if name in disabled:
            continue
        desc = fm.get("description", "(no description)")
        line = f"- **{name}**: {desc}"
        # Read source from _meta.json if present
        meta_path = d / "_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                src_str = format_source_info(meta.get("source"))
                if src_str:
                    line += f"  \n  source: {src_str}"
            except (json.JSONDecodeError, OSError):
                pass
        lines.append(line)

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
            logger.info("skill_loaded name=%s", skill_name)
            # Prepend the absolute skills directory so the agent can
            # construct correct paths to scripts (e.g. "python
            # <skills_dir>/baidu-search/scripts/search.py") regardless
            # of the current working directory.
            return (
                f"> **Skills root:** `{skills_dir.resolve()}`\n"
                f"> When running skill scripts, use this absolute path.\n\n"
                f"{content}"
            )

    # Build hint with available names
    available = [f"  - {fm.get('name', d.name)}" for d, fm, _body in skills]
    hint = "\n".join(available) if available else "  (none)"
    return f"Skill '{skill_name}' not found.\n\nAvailable skills:\n{hint}"




# ═══════════════════════════════════════════════════════════════════════════
# Mixin — shared __init__ + from_config for all skill tools
# ═══════════════════════════════════════════════════════════════════════════


def _resolve_skills_dir(skills_dir: str = "") -> Path:
    """Resolve the skills directory.

    Priority:
      1. Explicit path (from config or argument) — used as-is.
      2. Delegated to ``slife.paths.get_skills_dir()`` —
         ``<data_dir>/skills/``.
    """
    from slife.paths import get_skills_dir

    if skills_dir:
        return Path(skills_dir)
    return get_skills_dir()


class _SkillDirMixin:
    """Shared skills_dir init and from_config — mixed into Tool subclasses."""

    def __init__(self, skills_dir: str = ""):
        self.skills_dir = _resolve_skills_dir(skills_dir)

    @classmethod
    def from_config(cls, cfg, config, ctx=None):  # pyright: ignore[reportIncompatibleMethodOverride]
        tool = cls(skills_dir=cfg.get("skills_dir", ""))
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool


# ═══════════════════════════════════════════════════════════════════════════
# Tool classes
# ═══════════════════════════════════════════════════════════════════════════


class ListSkillsTool(_SkillDirMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """List all available skills with their names and descriptions."""

    name = "skill_list"
    category = "Skills"
    description = "List installed skills with names and one-line descriptions."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        config = ctx.config if ctx is not None else None
        config_path = config._path if config is not None else None
        disabled = _disabled_skill_names(config_path)
        result = get_skills_summary(self.skills_dir, disabled=disabled)
        return result if result else "No skills available."


class UseSkillTool(_SkillDirMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Load a skill's full SKILL.md documentation into context."""

    name = "skill_use"
    category = "Skills"
    description = "Return the full SKILL.md documentation for a skill."
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "Skill name, from skill_list."},
        },
        "required": ["skill_name"],
    }

    async def execute(self, **kwargs) -> str:
        skill_name: str = kwargs["skill_name"]
        ctx = getattr(self, "_ctx", None)
        config = ctx.config if ctx is not None else None
        config_path = config._path if config is not None else None
        disabled = _disabled_skill_names(config_path)
        if skill_name in disabled:
            return (
                f"Skill '{skill_name}' is disabled. "
                f"Use skill_set_enabled(name=\"{skill_name}\", enabled=true) to re-enable."
            )
        return _read_skill(self.skills_dir, skill_name)


class SetSkillTool(_SkillDirMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Install or update a skill by writing its files to the local skills directory.

    The agent is responsible for fetching the skill's files (e.g. via
    GitHub MCP, fetch MCP, or other tools). This tool just writes them
    to disk.

    Two input modes:
      - files: list of {path, content} dicts (use with GitHub MCP)
      - archive: base64-encoded .zip or .tar.gz (use with fetch MCP)

    After installation, skill_list and skill_use pick it up immediately
    (the skills directory is re-scanned on every call).
    """

    name = "skill_set"
    category = "Skills"
    description = "Install/update a skill (upsert — add + update in one call) from [{path, content}] files or a base64 .zip/.tar.gz archive."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Directory name, kebab-case (e.g. 'browser-use')."},
            "files": {
                "type": "array",
                "description": "[{path, content}]. Must include SKILL.md with YAML frontmatter — write its description in the skill's own language.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path (e.g. 'SKILL.md', 'scripts/run.py')."},
                        "content": {"type": "string", "description": "File content."},
                    },
                    "required": ["path", "content"],
                },
            },
            "archive": {"type": "string", "description": "Base64-encoded .zip or .tar.gz."},
            "source": {
                "type": "object",
                "description": "Provenance for future updates.",
                "properties": {
                    "url": {"type": "string", "description": "Discovery URL."},
                    "type": {"type": "string", "description": "Source type: github, url, marketplace."},
                    "version": {"type": "string", "description": "Version at install time."},
                    "description": {"type": "string", "description": "Optional note."},
                },
            },
        },
        "required": ["name"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        files: list[dict] | None = kwargs.get("files")
        archive_b64: str | None = kwargs.get("archive")
        source: dict | None = kwargs.get("source")

        if not files and not archive_b64:
            return (
                "[FAIL] Either 'files' or 'archive' is required.\n"
                "  - files: list of {path, content} (use with GitHub MCP)\n"
                "  - archive: base64-encoded .zip/.tar.gz (use with fetch MCP)"
            )
        if files and archive_b64:
            return "[FAIL] Provide 'files' or 'archive', not both."

        skill_dir = self.skills_dir / name
        # Reject path traversal in the skill name (e.g. "../../foo") before
        # creating any directory.
        try:
            skill_dir = _ensure_within(self.skills_dir, skill_dir)
        except ValueError:
            return f"[FAIL] Invalid skill name: {name!r}"
        is_update = skill_dir.exists()

        skill_dir.mkdir(parents=True, exist_ok=True)

        try:
            if archive_b64:
                result = self._install_from_archive(name, archive_b64, skill_dir, is_update)
            else:
                result = self._install_from_files(name, files, skill_dir, is_update)  # type: ignore[arg-type]
            self._write_meta_json(skill_dir, source)
            return result
        except Exception as e:
            if not is_update:
                # A fresh install failed — remove the partial directory.
                import shutil
                shutil.rmtree(skill_dir, ignore_errors=True)
            else:
                # Updating an existing skill failed — keep the previous
                # version intact instead of destroying it.
                logger.warning("skill_update_failed name=%s err=%s", name, e)
            logger.exception("skill_install_failed name=%s", name)
            return f"[FAIL] Error installing skill '{name}': {e}"

    def _install_from_files(self, name: str, files: list[dict], skill_dir: Path, is_update: bool = False) -> str:
        """Write individual files to the skill directory."""
        count = 0
        for f in files:
            rel = f["path"]
            # Reject any path that escapes the skill directory (traversal).
            file_path = _ensure_within(skill_dir, skill_dir / rel)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(f["content"], encoding="utf-8")
            count += 1
            logger.debug("skill_wrote_file path=%s", rel)

        has_skill_md = (skill_dir / "SKILL.md").exists()
        action = "Updated" if is_update else "Installed"
        msg = f"[OK] {action} skill '{name}' ({count} files) → {skill_dir}"
        if not has_skill_md:
            msg += (
                "\n[WARN] No SKILL.md found. skill_list will not discover "
                "this skill until a SKILL.md with proper frontmatter is added."
            )
        return msg

    def _install_from_archive(self, name: str, archive_b64: str, skill_dir: Path, is_update: bool = False) -> str:
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
                # zipfile has no filter= protection (unlike tarfile's
                # PEP 706 filter="data"), so validate members manually.
                _extract_zip_safely(zf, skill_dir)
        elif data[:2].hex() == '1f8b':  # gzip magic
            with tarfile.open(fileobj=bio, mode="r:gz") as tf:
                tf.extractall(skill_dir, filter="data")
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
        action = "Updated" if is_update else "Installed"
        msg = f"[OK] {action} skill '{name}' from archive → {skill_dir}"
        if not has_skill_md:
            msg += (
                "\n[WARN] No SKILL.md found. skill_list will not discover "
                "this skill until a SKILL.md with proper frontmatter is added."
            )
        return msg

    def _write_meta_json(self, skill_dir: Path, source: dict | None) -> None:
        """Write _meta.json with source provenance to the skill directory.

        Merges with existing _meta.json if present, preserving external
        fields like ownerId or slug.
        """
        source = with_fetched_at(source)
        if not source:
            return

        meta_path = skill_dir / "_meta.json"
        existing: dict = {}
        if meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        existing["source"] = source
        meta_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.debug("skill_meta_written dir=%s", skill_dir)


class RemoveSkillTool(_SkillDirMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Remove a skill by deleting its directory and SKILL.md.

    Matches by frontmatter 'name' field first, then by directory name.
    """

    name = "skill_remove"
    category = "Skills"
    description = "Delete a skill directory and all its contents."
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "Skill name, from skill_list."},
        },
        "required": ["skill_name"],
    }

    async def execute(self, **kwargs) -> str:
        skill_name: str = kwargs["skill_name"]

        # Reject path traversal in the skill name before touching the FS.
        try:
            _ensure_within(self.skills_dir, self.skills_dir / skill_name)
        except ValueError:
            return f"[FAIL] Invalid skill name: {skill_name!r}"

        # 1) Try matching via _iter_skills (directories with SKILL.md)
        skills = _iter_skills(self.skills_dir)
        for d, fm, _body in skills:
            if fm.get("name") == skill_name or d.name == skill_name:
                import shutil
                shutil.rmtree(d)
                logger.info("skill_removed name=%s", skill_name)
                return f"[OK] Removed skill '{skill_name}' (deleted {d})."

        # 2) Try matching by directory name directly (handles git clones
        #    or archives that lack SKILL.md)
        direct = self.skills_dir / skill_name
        if direct.exists() and direct.is_dir():
            import shutil
            shutil.rmtree(direct)
            logger.info("skill_dir_removed path=%s", direct)
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


# ═══════════════════════════════════════════════════════════════════════
# skill_set_enabled
# ═══════════════════════════════════════════════════════════════════════

class SkillSetEnabledTool(_SkillDirMixin, Tool):  # type: ignore[reportIncompatibleMethodOverride]
    name = "skill_set_enabled"
    category = "Skills"
    category: ClassVar[str] = "Skills"
    description = ("Enable or disable a skill. Takes effect immediately: skill_list "
                   "hides disabled skills and skill_use refuses them.")
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name, from skill_list."},
            "enabled": {"type": "boolean", "description": "Enable or disable."},
        },
        "required": ["name", "enabled"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        enabled: bool = kwargs["enabled"]

        # The skill must exist on disk (frontmatter name or directory name) —
        # the toggle records into the ``skills:`` config section, which
        # skill_list / skill_use read.
        exists = any(
            fm.get("name") == name or d.name == name
            for d, fm, _ in _iter_skills(self.skills_dir)
        )
        if not exists:
            return f"'{name}' not found. skill_list shows available skills."

        ctx = getattr(self, "_ctx", None)
        config = ctx.config if ctx is not None else None
        config_path = config._path if config is not None else None
        if config_path is None:
            return "Error: config path unavailable — cannot persist the toggle."
        raw = read_config(config_path)
        entries = raw.get("skills")
        if not isinstance(entries, dict):
            entries = {}
            raw["skills"] = entries
        entry = entries.get(name)
        if not isinstance(entry, dict):
            entry = {}
            entries[name] = entry
        entry["enabled"] = enabled
        write_config(config_path, raw)
        state = "enabled" if enabled else "disabled"
        logger.info("skill_set_enabled name=%s enabled=%s", name, enabled)
        return f"[OK] Skill '{name}' {state}."
