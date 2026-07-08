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
      - name: unique tool identifier
      - description: human and LLM-readable description
      - parameters: JSON Schema for function parameters
      - execute(): async method that returns a result string
    """

    name: str
    description: str
    parameters: dict

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """Execute the tool with the given arguments.

        Returns:
            Result string to send back to the LLM.
        """
        ...

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
