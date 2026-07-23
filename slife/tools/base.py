"""Slife native tool specification & abstract base class.

═══════════════════════════════════════════════════════════════════════
Native Tool Contract (third-party tools MUST follow this)
═══════════════════════════════════════════════════════════════════════

File
  ``slife/tools/<name>.py`` — one or more ``Tool`` subclasses.
  The factory auto-discovers all modules in this package.

Class contract
  Every tool MUST define four class attributes::

      name        : str   — unique identifier (snake_case, e.g. "my_tool")
      description : str   — LLM-visible description (one sentence)
      parameters  : dict  — JSON Schema for function arguments
      execute()   : async → str  — the tool's implementation

  Optional class attrs::

      requires_a2a   : bool = False   — only register when MQTT mesh is active
      _subagent_skip : bool = False   — hide from subagent workers
      from_config()  : classmethod    — factory when tool needs constructor args

  Validation happens at class-definition time — a tool that forgets
  ``name`` or ``parameters`` raises ``TypeError`` immediately.

Convenience helpers
  :func:`make_params` — build a JSON Schema from keyword field defs.
  :func:`require_params` — validate that named kwargs are non-empty.
  ``NO_PARAMS`` — ready-to-use schema for tools with no arguments.

Minimal example
  :file:`slife/tools/os_info.py` (the simplest built-in tool)::

      from slife.tools.base import Tool, NO_PARAMS
      from slife.platform import get_os_info

      class GetOsInfoTool(Tool):
          name = "get_os_info"
          description = "Return the current OS: Windows, Linux, or macOS."
          parameters = NO_PARAMS

          async def execute(self, **kwargs) -> str:
              return get_os_info()

Tool with arguments (using :func:`make_params`)::

      from slife.tools.base import Tool, make_params

      class MyTool(Tool):
          name = "my_tool"
          description = "Does something useful."
          parameters = make_params(
              query={"type": "string", "description": "Search query."},
              limit={"type": "integer", "description": "Max results.", "default": 10},
          )

          async def execute(self, query: str = "", limit: int = 10, **kwargs) -> str:
              ...

Discovery
  ``slife.tools.factory.create_tools_from_config()`` imports every
  ``slife.tools.*`` module and collects ``Tool.__subclasses__()``.
  No registry decorator or manual import is needed — just place the
  file in the package.  Use the optional ``tools:`` array in
  ``slife.json5`` only to override defaults or disable a tool.

═══════════════════════════════════════════════════════════════════════
Shared helpers
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)


# ── JSON Schema helpers ────────────────────────────────────────────


#: Ready-to-use parameter schema for tools that take no arguments.
NO_PARAMS: dict = {
    "type": "object",
    "properties": {},
    "required": [],
}


def make_params(**fields: dict) -> dict:
    """Build a JSON Schema parameters dict from keyword field definitions.

    Fields WITHOUT a ``"default"`` key are automatically marked as
    ``required``.  Fields WITH a ``"default"`` are optional.

    Example::

        make_params(
            query={"type": "string", "description": "Search query."},
            limit={"type": "integer", "description": "Max.", "default": 10},
        )
        # → {"type": "object",
        #    "properties": {...},
        #    "required": ["query"]}

    For complex nested schemas (arrays of objects, oneOf, etc.) write
    the JSON Schema dict directly — ``make_params`` covers the 90 %
    case of flat keyword arguments.
    """
    required = [k for k, v in fields.items() if "default" not in v]
    return {
        "type": "object",
        "properties": dict(fields),
        "required": required,
    }


# ── Validation helpers ────────────────────────────────────────────


def require_params(**params: object) -> str | None:
    """Validate that all named parameters are non-empty.

    Returns an error message string if any parameter is falsy,
    or ``None`` if all are valid.

    Usage::

        if err := require_params(agent_id=agent_id, task=task):
            return err
    """
    missing = [k for k, v in params.items() if not v]
    if missing:
        return f"Error: {' and '.join(missing)} required."
    return None


class Tool(ABC):
    """Abstract base class for all tools.

    Subclasses must define:
      - name: unique tool identifier (class-level str)
      - description: human and LLM-readable description (class-level str)
      - parameters: JSON Schema for function parameters (class-level dict)
      - execute(): async method that returns a result string

    Validation happens at class definition time via __init_subclass__.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict]

    # Set to True on tools that only work with the MQTT/A2A mesh.
    # The factory skips registration when a2a_config is absent or disabled.
    requires_a2a: ClassVar[bool] = False

    # Set to True on tools that should NOT be available to subagents.
    # Subagents inherit the main agent's tool set but skip these.
    _subagent_skip: ClassVar[bool] = False

    # Set to True on tools that require user approval before execution.
    # Default False — no approval needed.  External MCP server tools
    # inherit this from the server's ``require_approval`` config.
    requires_approval: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("name", "description", "parameters"):
            if not hasattr(cls, attr) or getattr(cls, attr) in (None, ""):
                raise TypeError(
                    f"{cls.__name__} must define a non-empty '{attr}' "
                    f"class attribute."
                )

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """Execute the tool with the given arguments.

        Returns:
            Result string to send back to the LLM.
        """
        ...

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None") -> "Tool":
        """Create tool instance from config override dict.

        The default implementation calls cls() with no arguments.
        Override in subclasses that need constructor parameters
        (e.g. timeout, skills_dir).
        """
        return cls()

    @classmethod
    def to_openai_function(cls) -> dict:
        """Convert to OpenAI function definition format."""
        return {
            "type": "function",
            "function": {
                "name": cls.name,
                "description": cls.description,
                "parameters": cls.parameters,
            },
        }
