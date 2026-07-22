"""System prompt builder — renders from template. Content lives in the template."""

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def build(agent_id: str = "slife", agent_name: str = "") -> str:
    """Render the system prompt.

    Args:
        agent_id: Agent identity (always set, from ``--agent`` CLI flag).
        agent_name: Optional human-readable display name for this agent.
    """
    today = date.today()
    return _env.get_template("system_prompt.j2").render(
        agent_id=agent_id,
        agent_name=agent_name,
        current_date=today.isoformat(),
        current_weekday=today.strftime("%A"),
    ).strip()
