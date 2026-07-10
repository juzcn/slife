"""System prompt builder — Jinja2 template, single responsibility, testable.

Renders the final system prompt from a template with dynamic platform context.
Skills are intentionally excluded — they are discoverable on demand
via the list_skills / use_skill tool chain.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from slife.platform import IS_WINDOWS

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def build(base_prompt: str) -> str:
    """Build the final system prompt.

    Args:
        base_prompt: Core prompt from config (user-configurable).

    Returns:
        The assembled system prompt, with OS notice rendered from template.
    """
    template = _env.get_template("system_prompt.j2")
    return template.render(
        base_prompt=base_prompt,
        is_windows=IS_WINDOWS,
    ).strip()
