"""Slife tool specification & abstract base class.

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

      from_config()  : classmethod    — factory when tool needs constructor args

  Validation happens at class-definition time — a tool that forgets
  ``name`` or ``parameters`` raises ``TypeError`` immediately.

Convenience helpers
  :func:`make_params` — build a JSON Schema from keyword field defs.
  :func:`require_params` — validate that named kwargs are non-empty.

Minimal example::

      from slife.tools.base import Tool

      class PingTool(Tool):
          name = "ping"
          description = "Return pong."
          parameters = {"type": "object", "properties": {}, "required": []}

          async def execute(self, **kwargs) -> str:
              return "pong"

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
  ``slife.yaml`` only to override defaults or disable a tool.

═══════════════════════════════════════════════════════════════════════
Shared helpers
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Self

if TYPE_CHECKING:
    from slife.config import Config
    from slife.tools.context import ToolContext

logger = logging.getLogger(__name__)


class _MemfilesClientMixin:
    """Delegate data-mutating ops to the memfiles plugin's MCP client.

    Shared by the schedule tools (``schedule.py``) and the user-preference
    tool (``user_prefs.py``): the main process never touches the plugin's
    store — every data op reaches it through ``ToolContext.memfiles_client``.
    The offline message is a class attribute so each feature can name its
    own gap ("scheduled-task tools are unavailable" vs "user preferences are
    unavailable").
    """

    #: Returned when the plugin's client is not yet connected.
    offline_message: ClassVar[str] = (
        "Error: memfiles plugin not connected."
    )

    def _client(self):
        ctx = getattr(self, "_ctx", None)
        return getattr(ctx, "memfiles_client", None) if ctx is not None else None

    async def _call(self, tool: str, arguments: dict | None = None) -> Any:
        """Call an internal ``__*`` tool; never raises (mirrors every slife
        tool's error-string contract).  Subclasses may post-process the raw
        string (schedule tools parse JSON), hence the loose return type."""
        client = self._client()
        if client is None:
            return self.offline_message
        try:
            return await client.call_tool(tool, arguments)
        except Exception as e:
            logger.debug("memfiles_tool_error tool=%s err=%s", tool, e)
            return f"Error: {tool} failed — {e}"


# ── JSON Schema helpers ────────────────────────────────────────────


def make_params(**fields: dict) -> dict:
    """Build a JSON Schema parameters dict from keyword field definitions.

    Fields WITHOUT a ``"default"`` key are automatically marked as
    ``required``.  Fields WITH a ``"default"`` are optional.

    The schema is closed (``additionalProperties: false``): a parameter the
    tool does not declare is a mistake, not something to swallow.  The same
    helper that names the fields is the one that declares the surface, so
    an undeclared key can never be silently accepted —
    :func:`validate_args` enforces this at dispatch.

    Example::

        make_params(
            query={"type": "string", "description": "Search query."},
            limit={"type": "integer", "description": "Max.", "default": 10},
        )
        # → {"type": "object",
        #    "properties": {...},
        #    "required": ["query"],
        #    "additionalProperties": False}

    For complex nested schemas (arrays of objects, oneOf, etc.) write
    the JSON Schema dict directly — ``make_params`` covers the 90 %
    case of flat keyword arguments.
    """
    required = [k for k, v in fields.items() if "default" not in v]
    return {
        "type": "object",
        "properties": dict(fields),
        "required": required,
        "additionalProperties": False,
    }


# ── Validation helpers ────────────────────────────────────────────


def require_params(
    _hints: dict[str, str] | None = None, **params: object,
) -> str | None:
    """Validate that all named parameters are non-empty.

    Returns an error message string if any parameter is falsy, or ``None``
    if all are valid.

    ``_hints`` maps a missing parameter to an appended guidance clause, so a
    caller keeps one message shape while explaining what is expected::

        if err := require_params(subagent_name=name,
                                 _hints={"subagent_name": "e.g. \"coder-1\""}):
            return err
        # "Error: subagent_name is required. — e.g. "coder-1""

    A single missing param reads ``"Error: <name> is required."``; several
    read ``"Error: <a> and <b> are required."``.  This is the one message
    shape every tool uses — no module spells the guard out as its own string.
    """
    missing = [k for k, v in params.items() if not v]
    if not missing:
        return None
    if len(missing) == 1:
        name = missing[0]
        hint = _hints.get(name) if _hints else None
        if hint:
            return f"Error: {name} is required — {hint}"
        return f"Error: {name} is required."
    return f"Error: {' and '.join(missing)} are required."


def validate_args(parameters: dict, tool_name: str, args: dict) -> str | None:
    """Validate a call's arguments against the tool's own schema.

    The complement of :func:`require_params`, one level up: ``require_params``
    checks *values* inside a tool that already received its arguments, while
    this checks the *call* — that the argument names exist and that nothing
    required is absent — before the tool runs at all.

    Returns an error string if the call does not match the schema, or ``None``
    if it does.  Two rules, both read off ``parameters``:

    * a key the schema declares ``required`` must be PRESENT (an empty value
      present is the tool's business — ``require_params`` speaks to that);
    * when the schema is closed (``additionalProperties: false``), a key
      outside ``properties`` is refused, and the error names the parameters
      that do exist.

    The second rule is why ``make_params`` closes its schemas: a model that
    guesses a parameter name gets corrected on the spot instead of having the
    argument dropped on the floor by the tool's ``**kwargs``.

    Schemas that leave ``additionalProperties`` unset stay permissive — remote
    (MCP / REST) schemas are adopted verbatim from their server and are the
    server's contract to declare, not ours to tighten.
    """
    props = parameters.get("properties") or {}
    problems: list[str] = []

    # Unknown names first: a guessed parameter is usually *why* a required one
    # is missing, so naming it — next to the names that do exist — is the
    # part that lets the caller correct the call in one step.
    if parameters.get("additionalProperties") is False:
        unknown = [k for k in args if k not in props]
        if unknown:
            names = ", ".join(repr(k) for k in unknown)
            plural = "s" if len(unknown) > 1 else ""
            problems.append(
                f"unknown parameter{plural} {names} — {tool_name} accepts: "
                f"{', '.join(props) or '(none)'}"
            )

    missing = [k for k in parameters.get("required") or [] if k not in args]
    if missing:
        if len(missing) == 1:
            problems.append(f"{missing[0]} is required")
        else:
            problems.append(f"{' and '.join(missing)} are required")

    if problems:
        return f"Error: {'. '.join(problems)}."
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

    # Logical category for grouping in system_tools_list output — a
    # free-form display label each builtin tool sets itself (e.g. System,
    # Execution, Skills, Models, Config, Credentials, CLI, REST API,
    # Vision, Display, Subagent, Harness).  Built-in plugin tools
    # are grouped by their plugin name instead; external MCP tools are
    # excluded from the listing.
    category: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("name", "description", "parameters"):
            if not hasattr(cls, attr) or getattr(cls, attr) in (None, ""):
                raise TypeError(
                    f"{cls.__name__} must define a non-empty '{attr}' "
                    f"class attribute."
                )
        # A harness-authored schema is CLOSED: a parameter the tool does not
        # declare is a mistake, not something for the tool's ``**kwargs`` to
        # swallow.  Applied here, at the one place every tool class passes
        # through, so it holds for every authoring style — the hand-written
        # ``parameters = {...}`` literal and ``make_params`` alike — instead
        # of only the ones that remembered.
        #
        # Own-dict only (``cls.__dict__``): a schema inherited from a parent
        # class belongs to that parent, and closing it twice is a no-op the
        # second time anyway.  A schema that states its own answer — an
        # explicit ``additionalProperties`` — keeps it: a tool that genuinely
        # takes free-form keys says so, and ``MCPProxyTool`` (which sets
        # ``parameters`` per INSTANCE from a remote server's inputSchema)
        # never reaches this rule at all, since a remote schema is the
        # server's contract to declare, not ours to tighten.
        own = cls.__dict__.get("parameters")
        if isinstance(own, dict) and "additionalProperties" not in own:
            cls.parameters = {**own, "additionalProperties": False}

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """Execute the tool with the given arguments.

        Returns:
            Result string to send back to the LLM.
        """
        ...

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None", ctx: "ToolContext | None" = None) -> Self:
        """Create tool instance from config override dict.

        The default implementation calls cls() with no arguments.
        Override in subclasses that need constructor parameters
        (e.g. timeout, skills_dir).

        *ctx* is a :class:`~slife.tools.context.ToolContext` holding
        runtime references (registry, config, MCP client, message_history).
        Tools that need any of these store it as ``self._ctx``.
        """
        tool = cls()
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool

    def to_openai_function(self) -> dict:
        """Convert to OpenAI function definition format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
