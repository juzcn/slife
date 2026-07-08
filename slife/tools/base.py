"""Abstract base classes for the tool system."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Result of executing a tool."""

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


class Tool(ABC):
    """Abstract base class for all tools.

    Subclasses must define:
      - name: unique tool identifier (class-level str)
      - description: human and LLM-readable description (class-level str)
      - parameters: JSON Schema for function parameters (class-level dict)
      - execute(): async method that returns a result string

    Validation happens at class definition time via __init_subclass__.
    """

    name: str
    description: str
    parameters: dict

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
