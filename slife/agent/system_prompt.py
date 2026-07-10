"""System prompt builder — renders from template. Content lives in the template."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def build() -> str:
    return _env.get_template("system_prompt.j2").render().strip()
