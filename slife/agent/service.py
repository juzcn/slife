"""Agent service layer — wires together LLM, tools, conversation, and loop.

Owns the agent's runtime state. The TUI delegates to this service
rather than directly managing agent internals.
"""

from slife.agent.system_prompt import build as build_system_prompt
from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation
from slife.agent.loop import AgentLoop, AgentEventHandler, AgentResult
from slife.tools.factory import create_tools_from_config


class AgentService:
    """Wires together LLM client, tools, conversation, and agent loop.

    Owns the agent's runtime state. The TUI delegates to this service
    rather than directly managing agent internals.
    """

    def __init__(self, config: Config):
        self.config = config
        self.tool_registry = create_tools_from_config(config.tools)
        self.llm_client = LLMClient(config.active_model)
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
        )
        self.conversation = Conversation(system_prompt=build_system_prompt())
        self.session_usage = TokenUsage()

    @property
    def model_display_name(self) -> str:
        """Human-readable name of the active model."""
        return self.config.active_model.display_name

    @property
    def thinking_enabled(self) -> bool:
        """Whether thinking/reasoning mode is active."""
        return self.config.active_model.thinking_enabled

    def clear(self) -> None:
        """Reset conversation history and session usage."""
        self.conversation.clear()
        self.session_usage = TokenUsage()

    async def process_message(
        self,
        user_input: str,
        images: list[str] | None,
        handler: AgentEventHandler,
    ) -> AgentResult:
        """Run the agent loop for a user message via streaming."""
        return await self.agent_loop.run(
            user_input=user_input,
            conversation=self.conversation,
            images=images,
            handler=handler,
        )
