"""Function-calling agent loop with token tracking and thinking support."""

import asyncio
import json
from collections.abc import Callable, Awaitable
from dataclasses import dataclass

from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation
from slife.tools.registry import ToolRegistry


@dataclass
class ToolCallInfo:
    """Information about a single tool call from the LLM."""

    id: str
    name: str
    arguments: dict


@dataclass
class AgentResult:
    """Result of running the agent loop."""

    text: str
    usage: TokenUsage


class MaxIterationsExceeded(Exception):
    """Raised when the agent loop exceeds the configured iteration limit."""

    def __init__(self, iterations: int):
        self.iterations = iterations
        super().__init__(f"Agent exceeded maximum of {iterations} iterations")


class AgentLoop:
    """Core function-calling agent loop.

    The loop:
      1. Sends conversation + tools to the LLM
      2. If the LLM returns tool_calls, executes them and loops
      3. If the LLM returns text (no tool_calls), returns the final text

    Tracks cumulative token usage across all API calls in the loop.
    Callbacks allow the TUI to display streaming text, thinking content,
    tool calls, tool results, and token usage in real time.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        max_iterations: int = 10,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations

    # ── Tool call helpers ──────────────────────────────────────────

    @staticmethod
    def _parse_tool_calls(message) -> list[ToolCallInfo]:
        """Parse OpenAI tool_calls from a response message."""
        parsed = []
        for tc in message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            parsed.append(
                ToolCallInfo(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                )
            )
        return parsed

    @staticmethod
    def _serialize_tool_calls(tool_calls: list[ToolCallInfo]) -> list[dict]:
        """Serialize ToolCallInfo list back to OpenAI API format."""
        return [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(
                        tc.arguments, ensure_ascii=False
                    ),
                },
            }
            for tc in tool_calls
        ]

    # ── Main loop ──────────────────────────────────────────────────

    async def run(
        self,
        user_input: str,
        conversation: Conversation,
        images: list[str] | None = None,
        on_thinking_chunk: Callable[[str], Awaitable[None]] | None = None,
        on_text_chunk: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call: Callable[[ToolCallInfo], Awaitable[None]] | None = None,
        on_tool_result: (
            Callable[[str, str, bool], Awaitable[None]] | None
        ) = None,
        on_token_usage: (
            Callable[[TokenUsage], Awaitable[None]] | None
        ) = None,
    ) -> AgentResult:
        """Run the agent loop for a single user input.

        Args:
            user_input: The user's message text.
            conversation: The conversation history (mutated in place).
            images: Optional list of image file paths to attach.
            on_thinking_chunk: Called with each char of reasoning content.
            on_text_chunk: Called with each character of the final response.
            on_tool_call: Called before each tool execution.
            on_tool_result: Called after each tool execution with
                            (tool_call_id, result_str, is_error).
            on_token_usage: Called with cumulative usage after each LLM call.

        Returns:
            AgentResult with final text and cumulative token usage.

        Raises:
            MaxIterationsExceeded: If the loop exceeds max_iterations.
        """
        conversation.add_user_message(user_input, images=images)
        total_usage = TokenUsage()

        for iteration in range(self.max_iterations):
            response, usage = await self.llm_client.chat(
                messages=conversation.to_openai_messages(),
                tools=self.tool_registry.to_openai_functions(),
                stream=False,
            )

            total_usage = total_usage + usage

            if on_token_usage:
                await on_token_usage(total_usage)

            message = response.choices[0].message

            # Emit reasoning/thinking content if present (deepseek-reasoner / V4 thinking)
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning and on_thinking_chunk:
                for char in reasoning:
                    await on_thinking_chunk(char)
                    await asyncio.sleep(0)

            # Check for tool calls
            if message.tool_calls:
                tool_calls = self._parse_tool_calls(message)

                # Record assistant message with tool calls
                conversation.add_assistant_message(
                    content=message.content,
                    tool_calls=self._serialize_tool_calls(tool_calls),
                )

                # Execute each tool
                for tc in tool_calls:
                    if on_tool_call:
                        await on_tool_call(tc)

                    result = await self.tool_registry.execute(
                        tc.name, **tc.arguments
                    )
                    is_error = result.startswith("Error")

                    if on_tool_result:
                        await on_tool_result(tc.id, result, is_error)

                    conversation.add_tool_result(tc.id, result)

                continue  # Loop back to send tool results to LLM

            # No tool calls — this is the final text response
            content = message.content or ""

            if on_text_chunk and content:
                for char in content:
                    await on_text_chunk(char)
                    await asyncio.sleep(0)

            conversation.add_assistant_message(content=content)
            return AgentResult(text=content, usage=total_usage)

        raise MaxIterationsExceeded(self.max_iterations)
