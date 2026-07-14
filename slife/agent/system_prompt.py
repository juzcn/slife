"""System prompt builder — renders from template. Content lives in the template."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def build(agent_name: str | None = None) -> str:
    """Render the system prompt.

    Args:
        agent_name: Optional display name for this agent instance.
                    When provided it is injected as ``agent_name`` in
                    the Jinja2 context so the template can personalise
                    the prompt.
    """
    return _env.get_template("system_prompt.j2").render(
        agent_name=agent_name,
    ).strip()
