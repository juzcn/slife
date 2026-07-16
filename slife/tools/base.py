"""Abstract base classes for the tool system."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from slife.config import Config


def require_params(**params: str) -> str | None:
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
