"""Agent service layer — wires together LLM, tools, conversation, and loop.

Owns the agent's runtime state. The TUI delegates to this service
rather than directly managing agent internals.

If MCP is enabled in config, also manages the MCP wrapper connection
and registers MCP proxy tools.

If A2A is enabled in config, manages the P2P agent mesh: connects to
the MQTT broker, publishes presence, discovers peers, and routes tasks
through a unified Inbox.
"""

import asyncio
import json
import logging
import os
import sys
import time as _time
from collections import deque
from datetime import datetime
from collections.abc import Callable
from pathlib import Path

from slife.agent.system_prompt import build as build_system_prompt
from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation, IMAGE_NOTE_PREFIX, turn_header
from slife.agent.heartbeat import HEARTBEAT_MARK
from slife.agent.loop import AgentLoop, AgentEventHandler, AgentResult
from slife.agent.inbox import Inbox, ConversationStore
from slife.agent.plugins import PluginLifecycle, PluginStartStatus
from slife.a2a.identity import HUMAN
from slife.tools.factory import create_tools_from_config
from slife.mcp.tool_adapter import create_proxy_tools

logger = logging.getLogger(__name__)


class MemoryDatabaseError(Exception):
    """The memory database is present but unreadable/unwritable (broken
    schema, corruption, disk error).

    Memory is core — the agent must not run without it.  Restore treats
    this as fatal (startup abort); turn-save treats it as a hard stop.
    """


# Module-level callbacks invoked when the active model is switched at
# runtime (e.g. by the model_switch tool).  Each callback receives the
# new model ref string (e.g. "deepseek/deepseek-v4-flash").
_on_model_switched: list[Callable[[str], None]] = []

# ── Memfiles tunnel readiness watch ──────────────────────────────────────
# The memfiles plugin eager-starts its ngrok tunnel on a background task, so
# the harness's one-time probe must not race a still-running attempt: a
# failed start retries up to 3× with 2s/4s backoff (~9s before it concludes).
# The harness probes __tunnel_status until the plugin reports a terminal
# state, bounded by these constants, and surfaces "tunnel down" only once.
_TUNNEL_SETTLE_TIMEOUT = 20.0  # seconds — max wait for the eager attempt
_TUNNEL_PROBE_INTERVAL = 1.0   # seconds — between __tunnel_status probes


# ── Tool result compaction for permanent memory ─────────────────────────
#
# The live conversation keeps oversized tool results whole — the model
# reasons over them during the turn (the 20% tool_result_ceiling is the
# only live cap, a hard window-safety constraint).  Permanent memory does
# NOT: tool output is reproducible (re-run the tool), so the diary stores
# a head+tail digest.  This keeps diary turns small enough that session
# restore can fill the context floor, and keeps memory_search recall cheap.
# Truncation is always announced to the model via an explicit marker.


def compact_tool_results(turn_messages: list[dict], budget_chars: int) -> int:
    """Compress oversized tool results in a turn to head+tail digests.

    A ``tool`` message whose content exceeds *budget_chars* is replaced
    with ``head + marker + tail`` (the budget split evenly between head
    and tail).  The marker tells a future reader the output was compacted
    at save time, how large it originally was, and which tool to re-run to
    retrieve the full version.  Results that already fit are left
    untouched.  Returns the number of messages compacted.

    The replacement is a *copy* — the caller's ``turn_messages`` list
    entries are swapped for new dicts, never mutated in place, so the
    live conversation keeps the full output.
    """
    if budget_chars <= 0:
        return 0
    head_chars = max(budget_chars // 2, 1)
    tail_chars = max(budget_chars - head_chars, 1)

    # Map tool_call_id → tool name so the marker can name the tool to
    # re-run (a future reader doesn't have the live assistant message).
    name_by_id: dict[str, str] = {}
    for m in turn_messages:
        for tc in m.get("tool_calls") or []:
            cid = tc.get("id")
            if cid:
                name_by_id[cid] = (tc.get("function") or {}).get("name", "")

    compacted = 0
    for i, m in enumerate(turn_messages):
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str) or len(content) <= budget_chars:
            continue
        if "[compacted at save:" in content:
            # Already compacted — re-applying would double-wrap and write an
            # inflated original-size claim.  Skip so compaction is idempotent.
            continue
        tcid = m.get("tool_call_id")
        tool_name = name_by_id.get(str(tcid), "") if tcid else ""
        name_note = f"by re-running {tool_name}" if tool_name else "by re-running the tool"
        marker = (
            f"\n… [compacted at save: original {len(content)} chars — "
            f"full output retrievable {name_note}]\n"
        )
        turn_messages[i] = {
            **m,
            "content": content[:head_chars] + marker + content[-tail_chars:],
        }
        compacted += 1
    return compacted


def _extract_turn_annotation(
    turn_messages: list[dict],
) -> tuple[str | None, str | None]:
    """Extract a rowid-less ``memory_turn_summarize`` call (the current turn).

    The model annotates the in-flight turn by calling ``memory_turn_summarize``
    without a rowid; the tool returns "captured" without writing.  The
    summary/tags are applied here, at the single save point, so the annotation
    lands on exactly the turn being saved — no ``latest_rowid()`` race, no
    cross-process pending state, and a rolled-back turn simply never applies.
    Explicit-rowid calls are ignored (the tool already wrote them).
    """
    summary: str | None = None
    tags: str | None = None
    for msg in turn_messages:
        for tc in msg.get("tool_calls") or []:
            name = tc.get("function", {}).get("name", "")
            if name.split("__")[-1] != "memory_turn_summarize":
                continue
            try:
                args = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if "rowid" in args and args.get("rowid") is not None:
                continue  # explicit rowid — the tool handled it already
            if args.get("summary"):
                summary = args["summary"]
            if args.get("tags"):
                tags = args["tags"]
    return summary, tags


class AgentService:
    """Wires together LLM client, tools, conversation, and agent loop.

    Owns the agent's runtime state. The TUI delegates to this service
    rather than directly managing agent internals.

    If MCP is enabled in config, also manages the MCP wrapper connection
    and registers MCP proxy tools.

    If A2A is enabled, manages the P2P mesh: Inbox, A2AClient, and
    per-source conversations.
    """

    def __init__(self, config: Config, is_subagent: bool = False):
        self.config = config
        # __post_init__ guarantees these are never None at runtime
        assert self.config.a2a_config is not None
        assert self.config.subagent_config is not None
        self.is_subagent = is_subagent

        # Build the shared ToolContext — replaces scattered module-level
        # singletons (get_registry, get_config, _rest_api_mcp_client, etc.)
        from slife.tools.context import ToolContext
        self._tool_ctx = ToolContext(config=config)
        self.tool_registry = create_tools_from_config(
            config.tools, config=config, is_subagent=is_subagent,
            ctx=self._tool_ctx,
        )
        # Backfill the registry reference (created by the factory)
        self._tool_ctx.registry = self.tool_registry
        # Also set the module-level singleton so check_mcp()
        # and other legacy callers of get_registry() can find it.
        # Subagents get their own filtered registry — don't overwrite
        # the main agent's reference with a subagent's.
        if not is_subagent:
            from slife.tools.registry import set_registry
            set_registry(self.tool_registry)
        self.llm_client = LLMClient(config.active_model)
        # Max tool result = tool_result_ceiling × context_window × 3 chars/token
        max_tool_result_chars = int(
            config.tool_result_ceiling
            * config.active_model.context_window
            * 3
        )
        # Pending A2A peer presence events (epoch, TUI line), drained into
        # the context footer on the next turn.  Unbounded by design — events
        # are consumed on every turn, so the steady-state size is "events
        # since last turn"; the guard below only protects against a
        # pathological long-idle + heavy-flapping session.
        self._presence_events: deque[tuple[float, str]] = deque()
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
            max_tool_result_chars=max_tool_result_chars,
            tool_timeout=config.tool_timeout,
            context_window=config.active_model.context_window,
            context_ceiling=config.context_ceiling,
            context_floor=config.context_floor,
            memdb_enabled=not is_subagent,
            supports_vision=config.active_model.supports_vision,
            model_name=config.active_model.display_name,
            input_modalities=", ".join(config.active_model.input_modalities),
            presence_provider=self._drain_presence_events,
            advance_context_start=self.advance_context_start,
        )
        self.conversation = Conversation(
            system_prompt=build_system_prompt(self.config),
        )
        self._tool_ctx.conversation = self.conversation
        # Runtime iteration-cap hook for the set_max_iterations meta tool.
        self._tool_ctx.set_max_iterations = self.agent_loop.set_max_iterations
        # Live-context boundary hooks — _trim_after_save advances the
        # boundary so a restart rebuilds the exit-time context; clear_context
        # flushes it for a fresh start.  Bound methods resolve the memdb
        # client at call time (it is not connected during __init__), so a
        # missing/unready client degrades to "skip" rather than raising.
        self._tool_ctx.advance_context_start = self.advance_context_start
        self._tool_ctx.set_context_start_latest = self.set_context_start_latest
        self._tool_ctx.reset_context_time = self.agent_loop.reset_context_time
        self.session_usage = TokenUsage()

        # Autonomous heartbeat — background idle task + TUI surfacing hooks.
        self._on_autonomous = None
        self._on_heartbeat = None
        self._heartbeat_task: asyncio.Task | None = None

        # ── Unified message queue (always active) ──────────────────
        # Every input — human keyboard, A2A MQTT, WeChat — flows
        # through the same inbox queue.  Processed serially.
        conversations = ConversationStore(
            system_prompt=build_system_prompt(self.config),
        )
        conversations._convs[HUMAN] = self.conversation

        self.inbox = Inbox(
            agent_loop=self.agent_loop,
            conversations=conversations,
            a2a_client=None,  # (legacy, unused)
            on_activity=self._notify_a2a_activity,  # always active for WeChat etc.
            on_turn_complete=self.save_to_memory,
        )
        self._inbox_task: asyncio.Task | None = None

        # ── Memory health ──────────────────────────────────────────
        # Memory is core.  A fatal turn-save failure (broken schema,
        # corruption, disk error) sets _memory_broken and freezes the
        # inbox — the agent must not keep running turns it can't persist.
        self._memory_broken = False
        self._memory_error = ""
        # TUI callback (set by the app) → persistent red banner.
        self._on_memory_broken: "Callable[[str], None] | None" = None
        # TUI callback (set by the app) → file-sharing tunnel unavailable.
        # The harness owns the surfacing: the memfiles plugin never talks
        # to the TUI — the main process probes __tunnel_status after the
        # plugin is ready and reports a terminal failure here.
        self._on_tunnel_down: "Callable[[str], None] | None" = None

        # ── Plugin lifecycle containers (replace dynamic setattr/getattr) ─
        self._plugins: dict[str, PluginLifecycle] = {
            "mcp": PluginLifecycle("mcp", self),
            "memdb": PluginLifecycle("memdb", self),
            "wechat": PluginLifecycle("wechat", self),
            "memfiles": PluginLifecycle("memfiles", self),
            "a2a": PluginLifecycle("a2a", self),
        }

        # A2A integration state
        self._subagent_manager = None
        self._on_a2a_callbacks: list = []  # callbacks for TUI notification

        # Register for runtime model-switch notifications so the
        # LLM client and agent loop stay in sync with the active model.
        _on_model_switched.append(self.reload_active_model)

    @property
    def model_display_name(self) -> str:
        """Active model as ``provider/model`` — the canonical ref the LLM sees.

        Multiple providers can serve the same model name, so the status bar
        shows the full ref (``deepseek/deepseek-v4-flash``) to disambiguate.
        """
        return self.config.active_model.ref

    @property
    def context_window(self) -> int:
        """Context window size (tokens) of the active model."""
        return self.config.active_model.context_window

    @property
    def current_context_tokens(self) -> int:
        """Context tokens the next API call would send — same single source
        as ``_sys_note`` (see :meth:`AgentLoop.context_tokens_for`):
        last API call's actual prompt tokens, else the restore-time
        estimate, else a live conversation estimate."""
        return self.agent_loop.context_tokens_for(self.conversation)

    @property
    def thinking_enabled(self) -> bool:
        """Whether thinking/reasoning mode is active."""
        return self.config.active_model.thinking_enabled

    def switch_model(self, ref: str) -> str:
        """Switch the active model directly from the UI — no LLM round-trip.

        Unlike ``model_switch`` (which requires the LLM to call it), this
        works even when the current model is unavailable: it validates the
        ref against the in-memory registry, persists ``active_model`` to
        the config file, and rebuilds the runtime (LLM client, agent loop,
        system prompt).  Returns a human-readable confirmation.

        Raises ``ValueError`` for an unknown ref.
        """
        if not any(m.ref == ref for m in self.config.models):
            raise ValueError(
                f"Unknown model ref '{ref}'. "
                f"Available: {[m.ref for m in self.config.models]}"
            )
        raw = self.config._read_config("switch_model", ref)
        if raw is not None:
            raw["active_model"] = ref
            self.config._write_config(raw)
        self.reload_active_model(ref)
        return f"Switched to {self.config.active_model.display_name}"

    def reload_active_model(self, new_ref: str) -> None:
        """Reload runtime state after the active model is switched.

        Rebuilds the LLM client, updates the agent loop's model-specific
        settings, and refreshes the conversation system prompt so the
        next turn uses the new model.
        """
        old_ref = self.config.active_model_ref
        self.config.active_model_ref = new_ref
        model = self.config.active_model

        logger.info(
            "model_reloaded from=%s to=%s display=%s",
            old_ref, new_ref, model.display_name,
        )

        # Rebuild LLM client with the new model config
        self.llm_client = LLMClient(model)

        # Update agent loop with new model's capabilities
        self.agent_loop.llm_client = self.llm_client
        self.agent_loop.supports_vision = model.supports_vision
        self.agent_loop.model_name = model.display_name
        self.agent_loop.input_modalities = ", ".join(model.input_modalities)
        self.agent_loop.context_window = model.context_window
        # The live tool-result cap is computed from the model's window — it
        # must track the switch or a 128K→32K switch leaves the old cap in
        # place (240% of the new window) and oversized results overflow.
        self.agent_loop.max_tool_result_chars = int(
            self.config.tool_result_ceiling * model.context_window * 3
        )
        # Drop stale per-conversation token caches (keyed by the old model's
        # prompt-token footprint) so the status bar stops reporting it.
        self.agent_loop._usage_by_conv.clear()
        # Also drop the restore-time fallback estimate — after a 32K→200K
        # switch the status bar / trim gate must not report the old window's
        # reading until the first API call under the new model.
        self.agent_loop._last_usage = TokenUsage()

        # Rebuild system prompt with updated model info — for the human
        # conversation AND every persistent one (WeChat) and future ones.
        new_system = build_system_prompt(self.config)
        if self.conversation.messages and self.conversation.messages[0]["role"] == "system":
            self.conversation.messages[0]["content"] = new_system
        self.inbox._conversations.update_system_prompt(new_system)

    @property
    def mcp_enabled(self) -> bool:
        """Whether MCP wrapper integration is active."""
        c = self._plugins["mcp"].client
        return c is not None and c.is_connected

    @property
    def a2a_enabled(self) -> bool:
        """Whether the A2A P2P mesh is active (a2a plugin connected)."""
        client = self._plugins["a2a"].client
        return client is not None and client.is_connected

    @property
    def subagent_manager(self):
        """The SubagentManager, if A2A is enabled and subagent support is active."""
        return self._subagent_manager

    def clear(self) -> None:
        """Reset conversation history and session usage."""
        self.conversation.clear()
        self.session_usage = TokenUsage()

    # ── Plugin auto-discovery & lifecycle ───────────────────────────────

    async def start_plugin_server(
        self, name: str, module: str,
    ) -> PluginStartStatus:
        """Spawn a plugin child process, connect, and register its tools.

        Single entry point for ALL plugins — built-in (mcp, memdb,
        wechat) and third-party.  The plugin's ``server.py`` must expose
        a ``main()`` that calls ``run_plugin_server(mcp)``.

        Dispatches internally for MCP (configurable wrapper command) and
        WeChat (poll loop); everything else uses the generic spawn path.
        Returns ``PluginStartStatus.STARTED`` on success, ``SKIPPED`` for
        expected no-ops (not configured / dependency absent — e.g. a2a
        without a running MQTT broker), ``FAILED`` on controlled failure,
        and raises on unexpected errors.
        """
        # ── MCP wrapper: custom command, auto-connects external servers ──
        if name == "mcp":
            if not self.config.mcp_config:
                logger.debug("mcp_skipped reason=no_config")
                return PluginStartStatus.SKIPPED
            await self.start_mcp()
            return (
                PluginStartStatus.STARTED
                if self.mcp_enabled else PluginStartStatus.FAILED
            )

        # ── WeChat: needs poll loop after registration ──────────────────
        if name == "wechat":
            return await self.start_wechat()

        # ── Memfiles: fully generic plugin — spawn, connect, register.
        # The plugin owns the ngrok tunnel and file serving; the harness
        # only exposes the plugin's MCP client for health checks.
        if name == "memfiles":
            started = await self._spawn_plugin_generic(name, module)
            if started:
                self._tool_ctx.memfiles_client = self._plugins["memfiles"].client
                # Publish the port so subagents can inherit it and reuse
                # the main agent's memfiles plugin (no second ngrok tunnel).
                os.environ["SLIFE_MEMFILES_PORT"] = str(self._plugins["memfiles"].port)
                self._start_generic_watchdog(name, module)
                # Harness-owned tunnel readiness: probe __tunnel_status once
                # the eager ngrok attempt settles, surface "tunnel down" only
                # on a terminal failure.  The plugin never talks to the TUI.
                self._watch_memfiles_tunnel()
            return (
                PluginStartStatus.STARTED if started else PluginStartStatus.FAILED
            )

        # ── A2A: mesh channel plugin — probe + config env + poll loop ──
        if name == "a2a":
            return await self.start_a2a()

        # ── Generic: spawn python -m <module>, connect, register tools ──
        started = await self._spawn_plugin_generic(name, module)
        if started:
            self._start_generic_watchdog(name, module)
        return (
            PluginStartStatus.STARTED if started else PluginStartStatus.FAILED
        )

    def _start_generic_watchdog(self, name: str, module: str) -> None:
        """Attach a crash watchdog to a generically-spawned plugin.

        Covers both the built-ins and auto-discovered third-party plugins —
        ``_spawn_plugin_generic`` creates a ``PluginLifecycle`` for the latter,
        so every plugin is managed via ``self._plugins``. The
        restart callback re-invokes the generic spawn; ``_spawn_plugin_generic``
        itself never starts a watchdog, so a restart never stacks a second
        monitor.
        """
        if name not in self._plugins:
            return

        async def _restart() -> None:
            await self._spawn_plugin_generic(name, module)
            if name == "memfiles":
                # _spawn_plugin_generic replaced self._plugins["memfiles"].client,
                # but the harness's ToolContext still points at the dead client —
                # re-point it or check_memfiles reports the restarted plugin offline.
                self._tool_ctx.memfiles_client = self._plugins[name].client

        self._plugins[name].start_watchdog(restart_cb=_restart)

    def _watch_memfiles_tunnel(self) -> None:
        """After memfiles loads, watch its eager ngrok attempt to settle and
        surface a TUI message when the tunnel is down.

        The harness owns the surfacing (main-process side); the plugin never
        talks to the TUI.  The tunnel is reported at most once, when it
        reaches a terminal ``failed`` state — a live tunnel stays silent.
        """
        client = self._plugins["memfiles"].client
        if client is None:
            return
        asyncio.create_task(self._check_memfiles_tunnel(client))

    async def _check_memfiles_tunnel(self, client) -> None:
        """Probe ``__tunnel_status`` until the eager attempt concludes, then
        report once if the tunnel failed.

        The plugin eager-starts the tunnel on a background task, so a single
        probe at ready-time would race and misread ``starting`` as down.  We
        follow the attempt to its terminal state (``active`` / ``failed``),
        bounded by ``_TUNNEL_SETTLE_TIMEOUT`` — an unresolved state within
        the window stays silent rather than guessing.
        """
        deadline = _time.monotonic() + _TUNNEL_SETTLE_TIMEOUT
        while True:
            try:
                raw = await client.call_tool("__tunnel_status")
                data = json.loads(raw)
            except Exception as e:
                logger.warning("tunnel_status_probe_failed err=%s", e)
                return  # plugin gone/unreachable — nothing to surface
            if data.get("active"):
                return  # tunnel is up — nothing to surface
            if data.get("state") == "failed":
                self._report_tunnel_down(data.get("hint") or "")
                return
            if _time.monotonic() >= deadline:
                return  # never reached a terminal state — stay silent
            await asyncio.sleep(_TUNNEL_PROBE_INTERVAL)

    def _report_tunnel_down(self, detail: str) -> None:
        """Log the failure and surface a one-line warning to the TUI.

        The message is deliberately generic (any failure cause — account
        limit, missing token, SDK absent) and points at ``system_health``
        for the concrete reason, matching the established convention
        (memfiles errors reference system_health for details).
        """
        logger.warning("tunnel_unavailable detail=%s", detail)
        cb = self._on_tunnel_down
        if cb is not None:
            try:
                cb(
                    "⚠ 文件分享隧道不可用：expose_file 与文件分享链接将不可用。"
                    "可问 system_health 查看具体原因。"
                )
            except Exception:
                logger.debug("surface_tunnel_down_error", exc_info=True)

    async def _spawn_plugin_generic(self, name: str, module: str) -> bool:
        """Spawn a plugin child, connect, and register its ``<name>__*`` tools."""
        from slife.mcp.process import MCPWrapperProcess

        logger.info("plugin_spawn name=%s module=%s", name, module)

        # Auto-discovered third-party plugins get a PluginLifecycle too, so the
        # crash watchdog and shutdown manage them exactly like the built-ins
        if name not in self._plugins:
            from slife.agent.plugins import PluginLifecycle
            self._plugins[name] = PluginLifecycle(name, self)

        process = MCPWrapperProcess(
            command=sys.executable,
            args=["-m", module],
        )
        try:
            await process.start()
            client = await process.create_client(tool_timeout=self.config.tool_timeout)

            # Discover tools — retry once on a timeout.  A Streamable HTTP
            # session established in the plugin's "signalled but not yet
            # serving" window can hang on Windows/Proactor (memdb's slow
            # lifespan makes this the likeliest).  By the retry the plugin is
            # definitely serving, so a fresh session succeeds — the race
            # self-heals instead of failing the load.
            try:
                plugin_tools = await client.list_tools()
            except TimeoutError:
                logger.warning("plugin_tools_timeout_retry name=%s", name)
                await client.disconnect()
                client = await process.create_client(
                    tool_timeout=self.config.tool_timeout,
                )
                plugin_tools = await client.list_tools()
            logger.debug("plugin_tools name=%s count=%d names=%s",
                         name, len(plugin_tools),
                         [t["name"] for t in plugin_tools])

            # Register as proxy tools — filter out harness-only tools.
            # Canonical marker: a plugin tool named ``__*`` (double underscore) is
            # harness-internal — called programmatically via call_tool(), never
            # exposed to the LLM.  (Single ``_`` = harness but LLM-visible, e.g.
            # the native `_sys_note`.)  The "harness-only" description
            # is kept as a secondary safety check.
            tagged = [
                {**t, "server": name}
                for t in plugin_tools
                if not t.get("name", "").startswith("__")
                and "harness-only" not in t.get("description", "").lower()
            ]
            if len(tagged) < len(plugin_tools):
                logger.debug(
                    "plugin_tools_filtered name=%s kept=%d dropped=%d",
                    name, len(tagged), len(plugin_tools) - len(tagged),
                )
            proxy_tools = create_proxy_tools(client, tagged)
            for tool in proxy_tools:
                self.tool_registry.register(tool)

            logger.info("plugin_ready name=%s tools=%d",
                         name, len(self.tool_registry.list_tools()))

            # Store for cleanup/watchdog — PluginLifecycle for all plugins
            # (built-in or auto-discovered third-party).
            self._plugins[name].client = client
            self._plugins[name].process = process
            self._plugins[name].port = process.port
            os.environ[f"SLIFE_{name.upper()}_PORT"] = str(process.port)

            return True
        except BaseException:
            # A failed spawn must not leave the lifecycle pointing at a
            # live-but-unconnected child (watchdog stall) or an orphaned
            # process (leak) — reset and stop it before re-raising.
            # BaseException (not just Exception) so a cancellation from the
            # app's required-plugin timeout also stops the child instead of
            # orphaning it.
            self._plugins[name].process = None
            self._plugins[name].client = None
            self._plugins[name].port = 0
            try:
                await process.stop()
            except Exception:
                pass
            raise

    # ── MCP lifecycle ──────────────────────────────────────────────────

    async def start_mcp(self) -> None:
        """Start the MCP wrapper as a child process and register its tools."""
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None  # guaranteed by Config.__post_init__

        logger.info("mcp_init_start")
        try:
            await self._connect_mcp_wrapper()
            await self._register_plugin_tools(
                "mcp",
                on_server_added=self._persist_server,
                on_server_removed=self._unpersist_server,
                on_server_updated=self._on_server_updated,
            )
            from slife.health import record
            record(
                "mcp_wrapper", "ok",
                key="status", value="connected",
                hint="MCP wrapper started and management tools registered.",
            )
        except Exception as e:
            logger.error("mcp_wrapper_init_failed err=%s", e)
            from slife.health import record
            record(
                "mcp_wrapper", "error",
                key="status", value="failed",
                hint=f"MCP wrapper failed to start: {e}. "
                     "MCP tools (filesystem, search, etc.) are unavailable.",
            )
            raise
        # Fire-and-forget: external MCP servers connect in background
        # so the UI appears immediately. Tools register as each server
        # connects — no need to block startup on slow/hung servers.
        asyncio.create_task(self._auto_connect_mcp_servers())
        asyncio.create_task(self._auto_connect_rest_apis())

        # Watchdog: on MCP wrapper crash, respawn + reconnect external servers
        async def _restart_mcp():
            await self._connect_mcp_wrapper()
            await self._register_plugin_tools(
                "mcp",
                on_server_added=self._persist_server,
                on_server_removed=self._unpersist_server,
                on_server_updated=self._on_server_updated,
            )
            asyncio.create_task(self._auto_connect_mcp_servers())
            asyncio.create_task(self._auto_connect_rest_apis())

        self._plugins["mcp"].start_watchdog(restart_cb=_restart_mcp)

        logger.info("mcp_init_done tools=%d", len(self.tool_registry.list_tools()))

    # ── HTTP-connect helpers (subagents share the main agent's plugins) ──

    async def _connect_plugin_http(self, name: str, port: int) -> None:
        """Connect to an already-running plugin via Streamable HTTP.

        Shared by :meth:`connect_mcp_http`, :meth:`connect_memdb_http`,
        and :meth:`connect_wechat_http`.
        """
        logger.info("%s_http_connect port=%s", name, port)
        await self._plugins[name].connect_http(port)

    async def connect_mcp_http(self, port: int) -> None:
        """Connect to an already-running MCP wrapper via Streamable HTTP.

        Used by subagents to share the main agent's plugin servers
        instead of spawning their own.

        Both main agents and subagents eagerly discover external MCP
        tools and register them as direct tools (e.g.
        ``tavily-mcp__tavily_search``).  Subagents use
        :meth:`_discover_existing_mcp_tools` which only lists tools
        from already-connected servers — it never spawns new processes.
        """
        await self._connect_plugin_http("mcp", port)
        await self._register_plugin_tools(
            "mcp",
            on_server_added=self._persist_server,
            on_server_removed=self._unpersist_server,
            on_server_updated=self._on_server_updated,
        )
        if not self.is_subagent:
            await self._auto_connect_mcp_servers()
            await self._auto_connect_rest_apis()
        else:
            await self._discover_existing_mcp_tools()
        logger.info("mcp_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_memdb_http(self, port: int) -> None:
        """Connect to an already-running memdb server via Streamable HTTP."""
        await self._connect_plugin_http("memdb", port)
        await self._register_plugin_tools("memdb")
        logger.info("memdb_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_wechat_http(self, port: int) -> None:
        """Connect to an already-running wechat server via Streamable HTTP."""
        await self._connect_plugin_http("wechat", port)
        await self._register_plugin_tools("wechat")
        logger.info("wechat_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_memfiles_http(self, port: int) -> None:
        """Connect to the main agent's memfiles plugin via Streamable HTTP.

        Used by subagents to reuse the main agent's file-cabinet plugin
        instead of spawning their own (which would also fight over the
        single free-tier ngrok tunnel).  Registers the ``memfiles__*``
        tools and exposes the client for health checks.
        """
        await self._connect_plugin_http("memfiles", port)
        await self._register_plugin_tools("memfiles")
        self._tool_ctx.memfiles_client = self._plugins["memfiles"].client
        logger.info("memfiles_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_a2a_http(self, port: int) -> None:
        """Connect to the main agent's a2a plugin via Streamable HTTP.

        Used by subagents to reuse the main agent's mesh channel.  Subagents
        register the plugin's ``a2a_*`` tools (so they can send as the main
        agent) but never drain the inbound queue (that stays with the main
        agent).
        """
        await self._connect_plugin_http("a2a", port)
        await self._register_plugin_tools("a2a")
        # Expose the mesh client for harness drain/dispatch plumbing.
        self._tool_ctx.a2a_mcp_client = self._plugins["a2a"].client
        logger.info("a2a_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    # ── MCP private helpers ──────────────────────────────────────────

    async def _connect_mcp_wrapper(self) -> None:
        """Spawn the MCP wrapper as a child process via Streamable HTTP."""
        from slife.mcp.process import MCPWrapperProcess

        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

        logger.info("mcp_wrapper_spawn transport=streamable-http")
        self._mcp_process = MCPWrapperProcess(
            command=mcp_cfg.wrapper_command,
            args=mcp_cfg.wrapper_args,
        )
        try:
            await self._mcp_process.start()
            self._plugins["mcp"].process = self._mcp_process
            self._plugins["mcp"].port = self._mcp_process.port
            os.environ["SLIFE_MCP_PORT"] = str(self._plugins["mcp"].port)
            self._plugins["mcp"].client = await self._mcp_process.create_client()
        except Exception:
            # A failed connect must not leave the lifecycle pointing at a
            # live-but-unconnected child — the mcp watchdog would block on
            # its wait() forever instead of backing off and retrying
            self._plugins["mcp"].process = None
            self._plugins["mcp"].port = 0
            self._plugins["mcp"].client = None
            try:
                await self._mcp_process.stop()
            except Exception:
                pass
            raise

    async def _register_plugin_tools(self, name: str, **kwargs) -> None:
        """Discover and register a connected plugin's tools as proxy tools.

        Tags each tool with the plugin's ``server`` name (the ``server__tool``
        prefix), filters out harness-only tools (names starting with ``__``),
        creates proxy tools, and registers them.

        Args:
            name: Plugin short name (``"mcp"``, ``"memdb"``, ``"wechat"``,
                ``"a2a"`` — harness-only tools are ``_``-prefixed and filtered;
                the ``a2a_*`` mesh tools register as proxy tools).
            **kwargs: Forwarded to :func:`create_proxy_tools` (e.g.
                ``on_server_added``, ``on_server_removed``).
        """
        client = self._plugins[name].client
        assert client is not None
        tools = await client.list_tools()
        logger.debug(
            "%s_tools names=%s", name,
            [t["name"] for t in tools],
        )

        # Harness-only: ``__`` (double-underscore) plugin tools are internal
        # and filtered out for all agents.
        tagged = [
            {**t, "server": name}
            for t in tools
            if not t["name"].startswith("__")
        ]

        proxy_tools = create_proxy_tools(self._plugins[name].client, tagged, **kwargs)
        for tool in proxy_tools:
            self.tool_registry.register(tool)
        logger.debug("%s_tools_registered count=%d", name, len(proxy_tools))

        # MCP-specific: let REST API tools call mcp_set / mcp_remove
        if name == "mcp":
            self._tool_ctx.mcp_client = self._plugins["mcp"].client

    async def _auto_connect_mcp_servers(self) -> None:
        """Auto-connect to pre-configured MCP servers and discover
        their tools.

        Servers are connected in parallel — each spawns its own
        subprocess independently, so total time is max(single-server)
        rather than sum(all-servers).  For 5 servers this cuts
        startup from ~18 s to ~9 s.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None
        assert self._plugins["mcp"].client is not None
        servers = mcp_cfg.servers
        if not servers:
            return

        logger.info("mcp_auto_connect servers=%d", len(servers))
        mcp_client = self._plugins["mcp"].client  # narrow for closure

        async def _connect_one(name: str, cfg: dict) -> None:
            try:
                if cfg.get("enabled") is False:
                    # Load into pool but don't connect or register tools.
                    # Server stays in pool (disabled) — no connection made.
                    # Use mcp_set_enabled(name=..., enabled=true) to enable later.
                    logger.debug("mcp_server_loading_disabled name=%s", name)
                    result = await mcp_client.call_tool(
                        "mcp_set",
                        {
                            "name": name,
                            "command": cfg.get("command", ""),
                            "args": cfg.get("args", []),
                            "env": cfg.get("env"),
                            "url": cfg.get("url", ""),
                            "headers": cfg.get("headers"),
                            "auth": cfg.get("auth"),
                            "enabled": False,
                        },
                    )
                    logger.debug(
                        "mcp_server_loaded_disabled name=%s result=%.200s",
                        name, result,
                    )
                    return
                # ── os_paths: auto-detect OS-accessible paths ──────────
                # When a file MCP has ``os_paths: true``, inject every
                # OS-accessible path as a ``--allow-path`` arg so the LLM
                # can reach anything the OS user can.  Permissions are
                # enforced by the OS kernel — not by the MCP config.
                args = list(cfg.get("args", []))
                if cfg.get("os_paths"):
                    from slife.os_detect import get_os_accessible_paths
                    os_paths = get_os_accessible_paths()
                    for p in os_paths:
                        args.extend(["--allow-path", p])
                    logger.debug(
                        "mcp_os_paths server=%s paths=%s", name, os_paths,
                    )

                result = await mcp_client.call_tool(
                    "mcp_set",
                    {
                        "name": name,
                        "command": cfg.get("command", ""),
                        "args": args,
                        "env": cfg.get("env"),
                        "url": cfg.get("url", ""),
                        "headers": cfg.get("headers"),
                        "auth": cfg.get("auth"),
                    },
                )
                logger.debug(
                    "mcp_server_connected name=%s result=%.200s",
                    name, result,
                )
                from slife.health import record
                record(
                    "mcp_server", "ok",
                    key=name, value="connected",
                    hint=f"MCP server '{name}' connected.",
                )
                # Register tools immediately — enabled implies eager.
                await self._discover_and_register_external_tools(server_name=name)
            except Exception as e:
                logger.error("mcp_auto_connect_failed server=%s err=%s", name, e)
                from slife.health import record
                record(
                    "mcp_server", "error",
                    key=name, value="connect_failed",
                    hint=f"MCP server '{name}' failed to connect: {e}",
                )

        await asyncio.gather(
            *(_connect_one(name, cfg) for name, cfg in servers.items())
        )

    async def _auto_connect_rest_apis(self) -> None:
        """Auto-connect REST APIs from the ``rest_apis`` config section.

        Each entry is connected via ``mcp_set`` with
        anyapi-mcp-server as the backend.  The LLM never sees npx or
        anyapi-mcp-server — it only sees the generated tools prefixed
        with the API name.
        """
        if not self._plugins["mcp"].client:
            return

        rest_apis = self.config.rest_apis
        if not rest_apis:
            return

        mcp_client = self._plugins["mcp"].client
        logger.info("rest_api_auto_connect count=%d", len(rest_apis))

        async def _connect_one(name: str, cfg: dict) -> None:
            try:
                if cfg.get("enabled") is False:
                    logger.debug("rest_api_skipped name=%s reason=disabled", name)
                    return
                spec_url = cfg.get("spec_url", "")
                base_url = cfg.get("base_url", "")
                api_key = cfg.get("api_key", "")
                description = cfg.get("description", "")

                if not spec_url or not base_url:
                    logger.warning(
                        "rest_api_skip name=%s reason=missing_spec_or_base", name,
                    )
                    return

                mcp_args = [
                    "-y", "anyapi-mcp-server",
                    "--name", name,
                    "--spec", spec_url,
                    "--base-url", base_url,
                ]
                if api_key:
                    mcp_args.extend([
                        "--header", f"Authorization: Bearer ${{{api_key}}}",
                    ])

                result = await mcp_client.call_tool(
                    "mcp_set",
                    {
                        "name": name,
                        "command": "npx",
                        "args": mcp_args,
                        "description": description,
                    },
                )
                logger.debug(
                    "rest_api_connected name=%s result=%.200s", name, result,
                )
            except Exception as e:
                logger.warning("rest_api_connect_failed name=%s err=%s", name, e)

        await asyncio.gather(
            *(_connect_one(name, cfg) for name, cfg in rest_apis.items())
        )

    async def _discover_existing_mcp_tools(self) -> None:
        """Discover tools from already-connected external MCP servers.

        Used by subagents — they share the main agent's MCP gateway, so
        external servers are already spawned.  Unlike
        :meth:`_auto_connect_mcp_servers`, this never calls
        ``mcp_set`` — it only lists tools from connected servers
        and registers them as proxy tools.

        Each ``mcp_list_tools`` call uses its own POST SSE stream,
        so concurrent requests are safe with the standard MCP library
        transport.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None
        assert self._plugins["mcp"].client is not None
        servers = mcp_cfg.servers
        if not servers:
            return

        logger.info("mcp_discover_existing servers=%d", len(servers))

        async def _discover_one(name: str, cfg: dict) -> None:
            try:
                if cfg.get("enabled") is False:
                    logger.debug("mcp_server_skipped name=%s reason=disabled", name)
                    return
                await self._discover_and_register_external_tools(server_name=name)
            except Exception as e:
                logger.debug("mcp_discover_existing_failed server=%s err=%s", name, e)

        await asyncio.gather(
            *(_discover_one(name, cfg) for name, cfg in servers.items())
        )

        logger.info(
            "mcp_discover_existing_done tools=%d",
            len(self.tool_registry.list_tools()),
        )

    # ── MCP tool discovery & registration ────────────────────────────

    async def _discover_and_register_external_tools(self, server_name: str) -> None:
        """Discover tools from a specific MCP server and register as proxy tools."""
        assert self._plugins["mcp"].client is not None
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

        try:
            tools_json = await self._plugins["mcp"].client.call_tool(
                "mcp_list_tools", {"server": server_name}
            )
            tools_data = json.loads(tools_json)
            external = tools_data.get("tools", [])

            if external:
                proxy_tools = create_proxy_tools(
                    self._plugins["mcp"].client, external,
                    on_server_added=self._persist_server,
                    on_server_removed=self._unpersist_server,
                    on_server_updated=self._on_server_updated,
                )
                for tool in proxy_tools:
                    self.tool_registry.register(tool)
                logger.debug(
                    "mcp_tools_registered server=%s count=%d",
                    server_name, len(proxy_tools),
                )
            else:
                logger.debug("mcp_no_tools server=%s", server_name)
        except Exception as e:
            logger.error("mcp_discover_failed server=%s err=%s", server_name, e)
            from slife.health import record
            record(
                "mcp_server", "warning",
                key=server_name, value="discovery_failed",
                hint=f"MCP server '{server_name}' connected but tool discovery failed: {e}",
            )

    async def _persist_server(self, name: str, command: str, args: list[str], env: dict | None = None, description: str = "", source: dict | None = None, url: str = "", headers: dict[str, str] | None = None):
        """Callback: persist a newly-added (or updated) MCP server to config
        file and immediately discover and register its tools.

        If a server with the same name already exists, its old proxy tools
        are unregistered first — this handles reconfiguration of servers
        like anyapi-mcp-server when the user provides new args.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None
        existing = mcp_cfg.servers.get(name)
        if existing:
            logger.debug(
                "mcp_server_update name=%s", name
            )
            self.tool_registry.unregister_by_prefix(f"{name}__")

        self.config.save_mcp_server(name, command, args, env, description, source, url=url, headers=headers)
        # Discover and register the new server's tools right away
        await self._discover_and_register_external_tools(server_name=name)

    async def _unpersist_server(self, name: str):
        """Callback: remove server from config and unregister its tools."""
        self.config.remove_mcp_server(name)
        removed = self.tool_registry.unregister_by_prefix(f"{name}__")
        if removed:
            logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)

    async def _on_server_updated(self, name: str, enabled: bool):
        """Callback: persist the enabled flag and update tool registration.

        - enabled=False → unregister tools, persist enabled=False.
        - enabled=True  → persist enabled=True, re-discover + register tools.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

        if not enabled:
            # Unregister tools from agent loop, persist disabled
            removed = self.tool_registry.unregister_by_prefix(f"{name}__")
            if removed:
                logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)
            self.config.set_server_enabled(name, False)
            return

        # enabled=True — persist, re-discover and register tools
        self.tool_registry.unregister_by_prefix(f"{name}__")
        await self._discover_and_register_external_tools(server_name=name)
        self.config.set_server_enabled(name, True)

    # ── Stop helpers ────────────────────────────────────────────────────

    async def _stop_plugin(self, name: str, *, has_poll_task: bool = False) -> None:
        """Disconnect client and stop process for plugin *name*.

        Args:
            name: Plugin short name (``"mcp"``, ``"memdb"``, ``"wechat"``).
            has_poll_task: If True, cancel the plugin's poll task first.
        """
        await self._plugins[name].stop(has_poll_task=has_poll_task)

    async def stop_mcp(self) -> None:
        """Shut down the MCP wrapper and clean up."""
        await self._stop_plugin("mcp")

    async def stop_memdb(self) -> None:
        """Disconnect and shut down the memdb service."""
        await self._stop_plugin("memdb")

    async def stop_wechat(self) -> None:
        """Shut down the WeChat plugin and clean up."""
        await self._stop_plugin("wechat", has_poll_task=True)

    async def stop_memfiles(self) -> None:
        """Stop the memfiles plugin.

        The plugin's own lifespan disconnects the ngrok tunnel on shutdown,
        so the harness only stops the child process.
        """
        await self._stop_plugin("memfiles")

    def kill_child_processes(self) -> None:
        """Synchronous best-effort child process cleanup.

        Called from the finally block in main() — no event loop required.
        Directly terminates known subprocesses so they don't become
        orphans holding log file handles on Windows.

        Scans for all ``_<name>_process`` attributes — works with
        auto-discovered plugins, not just the three built-ins.
        """
        # Kill known plugin processes first
        for plugin in self._plugins.values():
            plugin.kill()

        # Scan for auto-discovered plugins
        for attr_name in dir(self):
            if not attr_name.endswith("_process") or not attr_name.startswith("_"):
                continue
            wrapper = getattr(self, attr_name, None)
            if wrapper is None:
                continue
            p = getattr(wrapper, "_process", None)
            if p is None:
                continue
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=3.0)  # type: ignore[call-arg]
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

        # Subagent manager cleanup
        mgr = self._subagent_manager
        if mgr is not None:
            for name in list(mgr._subagents.keys()):
                proc = mgr._subagents.get(name)
                if proc is not None and proc._process is not None:
                    try:
                        proc._process.terminate()
                    except Exception:
                        pass
                    try:
                        proc._process.wait(timeout=2.0)  # type: ignore[call-arg]
                    except Exception:
                        try:
                            proc._process.kill()
                        except Exception:
                            pass

    # ── Memory lifecycle ──────────────────────────────────────────────

    @property
    def memdb_enabled(self) -> bool:
        """Whether the memdb service is connected."""
        return self._plugins["memdb"].client is not None and self._plugins["memdb"].client.is_connected

    @property
    def wechat_enabled(self) -> bool:
        """Whether the WeChat plugin is connected."""
        return self._plugins["wechat"].client is not None and self._plugins["wechat"].client.is_connected

    # ── Plugin spawn + register helper ────────────────────────────────

    async def _spawn_and_register_plugin(
        self,
        name: str,
        module: str,
        harness_tools: set[str] | None = None,
    ) -> None:
        """Spawn a plugin child process, connect, and register its LLM-visible tools.

        Delegates to ``PluginLifecycle.spawn()`` for the actual work.

        *harness_tools* is **deprecated** — harness-only tools are now
        identified by the ``_`` prefix naming convention.
        """
        await self._plugins[name].spawn(module, harness_tools)


    async def start_memdb(self) -> bool:
        """Connect to slife-memdb and register tools. Returns True on success."""
        mem_cfg = self.config.memdb_config
        assert mem_cfg is not None  # guaranteed by Config.__post_init__

        logger.info("memdb_init_start")
        try:
            await self._spawn_and_register_plugin(
                "memdb",
                "slife.plugins.memdb.server",
            )
            self._plugins["memdb"].start_watchdog()
            logger.info("memdb_init_done tools=%d", len(self.tool_registry.list_tools()))
            from slife.health import record
            record(
                "memdb_service", "ok",
                key="status", value="connected",
                hint="memdb service started and tools registered.",
            )
            return True
        except Exception as e:
            logger.warning("memdb_init_failed err=%s fallback=continue_without_memdb", e)
            from slife.health import record
            record(
                "memdb_service", "error",
                key="status", value="failed",
                hint=f"memdb service failed to start: {e}. "
                     "Turn storage and search are unavailable.",
            )
            return False

    # ── WeChat lifecycle ───────────────────────────────────────────────

    async def start_wechat(self) -> PluginStartStatus:
        """Start the WeChat plugin if enabled in config.

        Returns ``STARTED`` on success, ``SKIPPED`` when WeChat is not
        enabled (expected), ``FAILED`` on start error.
        """
        wechat_cfg = self.config.wechat_config
        if wechat_cfg is None or not wechat_cfg.enabled:
            logger.debug("wechat_not_enabled")
            return PluginStartStatus.SKIPPED

        logger.info("wechat_init_start")
        try:
            await self._spawn_and_register_plugin(
                "wechat",
                "slife.plugins.wechat.server",
            )

            # Auto-restore session at startup (triggers server-side poll loop)
            wechat_client = self._plugins["wechat"].client
            assert wechat_client is not None
            try:
                await wechat_client.call_tool("check_status", {})
                logger.debug("wechat_auto_restore_triggered")
            except Exception:
                pass

            # Start background poll loop — injects WeChat messages into the inbox
            self._plugins["wechat"].poll_task = asyncio.create_task(self._wechat_poll_loop())

            # Watchdog: on crash, respawn + restore poll loop
            async def _restart_wechat():
                self._cancel_plugin_task("wechat")
                await self._spawn_and_register_plugin(
                    "wechat",
                    "slife.plugins.wechat.server",
                    )
                wc = self._plugins["wechat"].client
                if wc is not None:
                    try:
                        await wc.call_tool("check_status", {})
                    except Exception:
                        pass
                self._plugins["wechat"].poll_task = asyncio.create_task(
                    self._wechat_poll_loop(),
                )

            self._plugins["wechat"].start_watchdog(restart_cb=_restart_wechat)

            logger.info("wechat_init_done tools=%d", len(self.tool_registry.list_tools()))
            from slife.health import record
            record(
                "wechat_service", "ok",
                key="status", value="connected",
                hint="WeChat plugin started and tools registered.",
            )
            return PluginStartStatus.STARTED
        except Exception as e:
            logger.warning("wechat_init_failed err=%s fallback=continue_without_wechat", e)
            from slife.health import record
            record(
                "wechat_service", "error",
                key="status", value="failed",
                hint=f"WeChat plugin failed to start: {e}. "
                     "WeChat messaging is unavailable.",
            )
            return PluginStartStatus.FAILED

    async def _wechat_poll_loop(self, interval: float = 5.0) -> None:
        """Poll the wechat plugin for new messages and inject them into the inbox.

        Uses harness-only tools (wechat_drain_incoming, wechat_dispatch_reply)
        so all wechat-specific logic — typing indicators, message format —
        stays inside the plugin process.  The harness only sees generic
        messages with an on_reply callback.
        """
        import json as _json
        from slife.a2a.identity import AgentMessage, WECHAT

        logger.info("wechat_poll_loop_start interval=%.1fs", interval)

        while self.wechat_enabled:
            try:
                assert self._plugins["wechat"].client is not None

                result = await self._plugins["wechat"].client.call_tool(
                    "__wechat_drain_incoming", {},
                )
                data = _json.loads(result)
                msgs = data.get("messages", [])

                for m in msgs:
                    from_id = m.get("to_user_id", "")
                    ctx_token = m.get("context_token", "")
                    text = m.get("text", "")

                    if not text.strip():
                        continue

                    wc = self._plugins["wechat"].client  # local ref for closure

                    async def _reply(reply_text: str,
                                     uid=from_id, tok=ctx_token) -> None:
                        try:
                            await wc.call_tool("__wechat_dispatch_reply", {
                                "to_user_id": uid,
                                "context_token": tok,
                                "text": reply_text,
                            })
                            logger.debug("wechat_out to=%s len=%d", uid, len(reply_text))
                        except Exception as e:
                            logger.debug("wechat_reply_error err=%s", e)

                    msg = AgentMessage(
                        source=WECHAT,
                        content=text,
                        metadata={"channel": "wechat"},
                        on_reply=_reply,
                    )
                    await self.inbox.post(msg)
                    logger.debug("wechat_in from=%s text=%.100s", from_id, text)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("wechat_poll_error err=%s", e)

            await asyncio.sleep(interval)

        logger.info("wechat_poll_loop_stop")

    async def save_to_memory(
        self,
        user_message: str = "",
        token_count: int | None = None,
        prompt_tokens: int | None = None,
        conversation: "Conversation | None" = None,
        channel: str = "",
        images: "list[str] | None" = None,
        created_at: "datetime | str | None" = None,
        handler: "object | None" = None,
    ) -> None:
        """Save the just-completed turn as a new row in memory.

        Args:
            user_message: The user's input text.
            token_count: Cumulative token usage for the turn (billing).
            prompt_tokens: The LAST LLM call's prompt_tokens — the exact
                context size at turn end.  Persisted so restore primes the
                footer / _sys_note with the real exit-time occupancy.
            conversation: The conversation to extract messages from.
                Defaults to self.conversation (the TUI conversation).
            channel: Source channel — 'human', 'wechat', or remote agent id.
            created_at: The user-input timestamp (Enter-press moment, aware
                datetime or ISO-8601 str).  Written as the diary
                ``created_at`` so restore matches the live [HH:MM].
                ``None`` lets the store use its own now().
            handler: The turn's UI handler, if any.  Receives the captured
                completion time via ``set_completed_at`` so the live
                assistant message shows when the turn actually finished.
        """
        # Accumulate turn's billed tokens into the session total.
        if token_count:
            self.session_usage.total_tokens += token_count

        if not self.memdb_enabled:
            return

        conv = conversation if conversation is not None else self.conversation

        # Invariant: never persist an inconsistent turn.  Repair orphaned
        # tool_calls and close the turn if needed BEFORE extracting — the
        # same ensure used on load and before each user message.
        conv._ensure_turn_consistent()

        # Completion time — captured AFTER the final ensure (the turn is
        # now definitively done) and BEFORE the (potentially slow) MCP
        # save call, so completed_at reflects when the assistant finished,
        # not when the write landed.
        now = datetime.now().astimezone()

        # Push the completion time to the live UI so the assistant message
        # shows [HH:MM] at turn end — the same value restore will read.
        if handler is not None:
            set_completed = getattr(handler, "set_completed_at", None)
            if set_completed is not None:
                try:
                    set_completed(now)
                except Exception:
                    pass

        # Extract turn messages: everything after the matching user message.
        # Must handle both plain text (content is a str) and multimodal
        # messages (content is a list of {type, text/image_url} parts).
        #
        # Compare against the sanitized form of the input: add_user_message
        # stores sanitize_secrets(content), so a raw match would miss when
        # the user pasted an API key — and the turn would be saved with
        # empty messages (silent data loss).  If no user message matches at
        # all (the turn was rolled back on a content-policy / bad-request
        # error), there is nothing to persist.
        from slife.logfmt import sanitize_secrets
        target = sanitize_secrets(user_message)
        all_messages = list(conv.messages)
        turn_messages: list[dict] | None = None
        user_idx = -1  # index of the matched user message (for the footnote)
        for i in range(len(all_messages) - 1, -1, -1):
            msg = all_messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content == target:
                turn_messages = all_messages[i + 1:]
                user_idx = i
                break
            if isinstance(content, list):
                # Exclude the "[Image: …]" note that add_user_message appends
                # for dropped attachments — it is not the user's text, and
                # including it breaks the match so the turn silently fails.
                text = "".join(
                    p.get("text", "") for p in content
                    if p.get("type") == "text"
                    and not p.get("text", "").startswith(IMAGE_NOTE_PREFIX)
                )
                if text == target:
                    turn_messages = all_messages[i + 1:]
                    user_idx = i
                    break

        if turn_messages is None:
            return

        # Context trimming happens after this save, in
        # AgentLoop._trim_after_save (invoked below once the row is written)
        # — it uses this turn's real API usage and appends a runtime
        # [TrimContext: N] marker.  Each turn is saved here via
        # memory_save_turn, so trimmed turns remain searchable.

        # The runtime [TrimContext: N] marker must never reach the diary —
        # it is meaningful only in the live session.  Strip it from the
        # copy being persisted (the live conversation keeps its marker).
        from slife.agent.conversation import Conversation as _Conv
        turn_messages = _Conv.strip_trim_markers(turn_messages)

        # Permanent memory keeps only head+tail digests of oversized tool
        # results — the diary never hoards reproducible tool output, so a
        # single result can't grow a turn past what session restore can
        # rebuild within the context floor.  The live conversation is not
        # touched (the copy swapped into turn_messages is a new dict).
        compacted = compact_tool_results(
            turn_messages, self.config.memory_tool_result_chars,
        )
        if compacted:
            logger.info(
                "memory_tool_result_compacted count=%d budget=%d",
                compacted, self.config.memory_tool_result_chars,
            )

        assert self._plugins["memdb"].client is not None  # guarded by memdb_enabled
        save_args = {
            "user_message": user_message,
            "messages": turn_messages,
            "images": images or [],
            "token_count": token_count or 0,
            "prompt_tokens": prompt_tokens or 0,
            "who_helped": self.config.agent_name,
            "what_model": self.config.active_model.ref,
            "channel": channel,
        }
        if created_at:
            # Normalise an aware datetime to the store's ISO format; a str
            # (e.g. tests) is passed through as-is.
            save_args["created_at"] = (
                created_at.astimezone().isoformat(timespec="seconds")
                if isinstance(created_at, datetime)
                else created_at
            )
        save_args["completed_at"] = now.isoformat(timespec="seconds")
        # A rowid-less memory_turn_summarize captured the current turn's
        # summary/tags — ride them on the save so they land on the new row.
        summary, tags = _extract_turn_annotation(turn_messages)
        if summary is not None or tags is not None:
            save_args["summary"] = summary
            save_args["tags"] = tags
        try:
            result = await asyncio.wait_for(
                self._plugins["memdb"].client.call_tool(
                    "__memory_save_turn",
                    save_args,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            # The save is a fast insert (embedding is deferred to the memdb
            # plugin's background reindex) — a timeout now means the MCP
            # channel itself is slow, not a first-save model load.  The row
            # may still be written server-side.
            logger.warning("memdb_save_timeout reason=save_call_exceeded_timeout")
            await self._warn_memory_save(
                handler, "记忆保存超时：未能确认本轮已写入记忆",
            )
            return
        except Exception as e:
            # A raised call_tool is a transient MCP/channel failure (the
            # plugin returns {"error": ...} for DB-side failures instead).
            logger.warning("memdb_save_error err=%s", e)
            await self._warn_memory_save(
                handler, "记忆保存失败（通道错误）：未能确认本轮已写入记忆",
            )
            return

        # The plugin returns {"error": ...} on a persistent DB failure
        # (broken schema, corruption, disk).  Memory is core — this is a
        # hard stop, not a silent skip: freeze the inbox and surface it.
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("error"):
                self._memory_broken = True
                self._memory_error = parsed["error"]
                logger.error("memory_save_fatal err=%s", self._memory_error)
                if self.inbox is not None:
                    self.inbox.freeze(f"记忆保存失败: {self._memory_error}")
                if self._on_memory_broken is not None:
                    try:
                        self._on_memory_broken(self._memory_error)
                    except Exception:
                        pass
            elif isinstance(parsed, dict):
                # The turn now has a rowid — annotate its user message so the
                # next LLM call can reference it precisely (and the
                # memory_turn_summarize default no longer needs the racy
                # latest_rowid fallback).  Live TUI bubbles are NOT
                # retro-updated: their lack of the footnote is itself the
                # "current session" signal.  Best-effort — a failure only
                # leaves the message unannotated.
                try:
                    self._annotate_saved_turn(
                        conv, user_idx, rowid=parsed.get("rowid"),
                        created_at=created_at, completed_at=now,
                    )
                except Exception:
                    logger.debug("turn_annotation_skipped", exc_info=True)
                # Trim AFTER the turn is safely persisted: the just-completed
                # turn's real prompt_tokens are now known (the last API call's
                # usage), so the ceiling check is exact, and a trim can never
                # lose an unsaved turn.  Operates on *conv* (the conversation
                # this turn ran in — human / wechat / a2a), never the global.
                # Best-effort: a trim failure must not break the save flow.
                loop = getattr(self, "agent_loop", None)
                if loop is not None:
                    try:
                        await loop._trim_after_save(conv, handler)
                    except Exception:
                        logger.exception("trim_after_save_failed")
            else:
                # The channel returned something that is neither a save
                # ack nor an error object (non-JSON text, or JSON that
                # isn't an object) — the save may or may not have landed
                # and there is no way to tell.  Don't swallow it silently:
                # log it and surface a user-visible warning in the same
                # style as the max-iterations notice.
                logger.warning("memdb_save_unparsable response=%.120r", result)
                await self._warn_memory_save(
                    handler, "记忆保存未能确认：返回了无法解析的响应，本轮可能未写入记忆",
                )

    async def _warn_memory_save(
        self, handler, message: str,
    ) -> None:
        """Best-effort TUI warning for a failed memory save.

        Every soft save-failure path (timeout, channel raise, unparseable
        response) funnels through here so the user gets the same ✗ red
        system line as the max-iterations notice.  The persistent DB
        failure is the one exception — it keeps its own freezing banner
        (see ``on_memory_broken``).  Never raises: a failing handler must
        not break the turn flow.
        """
        warn = getattr(handler, "on_memory_save_warning", None)
        if warn is not None:
            try:
                await warn(message)
            except Exception:
                pass

    def _annotate_saved_turn(
        self,
        conversation: "Conversation",
        user_idx: int,
        rowid: int | None,
        created_at: "datetime | str | None",
        completed_at: datetime,
    ) -> None:
        """Append the turn footnote to the just-saved turn's user message.

        Called after a successful ``__memory_save_turn`` so the rowid is
        known: the next LLM call sees ``[Turn: N · start → end]`` and can
        reference the turn precisely.  Heartbeat turns are skipped (their
        user message is a synthetic trigger).  Purely additive and
        best-effort — a failure leaves the message unannotated.
        """
        if rowid is None:
            return
        msgs = conversation.messages
        if not (0 <= user_idx < len(msgs)) or msgs[user_idx].get("role") != "user":
            return
        content = msgs[user_idx].get("content", "")
        if isinstance(content, str):
            if content.startswith(HEARTBEAT_MARK):
                return
        elif isinstance(content, list):
            joined = "".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )
            if joined.startswith(HEARTBEAT_MARK):
                return
        else:
            return

        def _iso(value) -> str:
            if isinstance(value, datetime):
                return value.astimezone().isoformat(timespec="seconds")
            return value or ""

        header = turn_header({
            "rowid": rowid,
            "created_at": _iso(created_at),
            "completed_at": _iso(completed_at),
        })
        if not header:
            return
        if isinstance(content, str):
            msgs[user_idx]["content"] = content + " " + header
        else:
            msgs[user_idx]["content"] = list(content) + [
                {"type": "text", "text": " " + header}
            ]

    async def get_recent_turns(self, limit: int = 20) -> tuple[list[dict], int, int]:
        """Load recent turns for restore. Returns ([], 0, 0) if no turns.

        Restores the **exit-time context** verbatim: it reads the persisted
        live-context start boundary (:meth:`SessionStore.get_context_start`,
        maintained by ``_trim_after_save`` / ``clear_context``) and returns
        **every** turn recorded after it — the exact slice the agent was
        working with when it exited.  No re-slicing against the ceiling: the
        boundary already encodes the trimmed state, so restore simply replays
        it (the agent picks up exactly where it left off).

        Fetches newest-first in batches of *limit* (each batch already
        newest-first, so appending stays globally newest-first), then reverses
        to **oldest-first** so the restore rebuilds the conversation
        chronologically.  Heartbeat turns are included — they restore as
        ⚡ 自主, consistent with the live TUI.

        Returns ``(selected, skipped, budget)`` — *skipped* is always 0 (no
        turns are dropped for a budget), *budget* is 0 (no ceiling cap: the
        boundary already bounds what is restored).  Kept as a 3-tuple so the
        call site and ``restore_session`` stay compatible.

        A defensive hard cap (2× the ceiling) guards against a stale
        boundary of 0 from a pre-boundary DB: it would otherwise replay the
        entire history at once.  Normal operation never reaches it — the
        live trim bounds the in-context slice well below the ceiling.

        Reads directly from SQLite — independent of the memory plugin / MCP.
        """
        store = None
        db_path = None
        try:
            from slife.plugins.memdb.store import SessionStore
            from slife.ui.restore import estimate_turn_tokens

            db_path = self._get_memory_db_path()
            if not (db_path and db_path.is_file()):
                return [], 0, 0
            store = SessionStore(db_path)
            await store.setup(embedding_dim=0)
            start_rowid = await store.get_context_start()

            # Accumulate newest-first batches after the live-context boundary
            # until exhausted.  The defensive cap stops only a stale-boundary
            # (0) DB from replaying unbounded history.
            hard_cap = int(
                self.config.active_model.context_window
                * self.config.context_ceiling * 2
            )
            all_turns: list[dict] = []
            total = 0
            offset = 0
            while total < hard_cap:
                batch = await store.get_recent_turns(
                    limit=limit, offset=offset, after_rowid=start_rowid,
                )
                if not batch:
                    break
                all_turns.extend(batch)
                total += sum(estimate_turn_tokens(t) for t in batch)
                offset += limit

            all_turns.reverse()  # oldest-first for restore
            return all_turns, 0, 0
        except Exception as e:
            # A present-but-broken memory DB (missing column, corruption,
            # disk error) must NOT start a memory-less session silently —
            # memory is core, so restore failure is fatal.
            logger.error("memory_restore_fatal err=%s", e)
            raise MemoryDatabaseError(
                f"cannot read memory database {db_path or 'unknown'}: {e}"
            ) from e
        finally:
            # Close the aiosqlite connection so its worker thread doesn't
            # outlive the event loop (leaks + "Event loop is closed" in tests).
            if store is not None:
                try:
                    await store.close()
                except Exception:
                    pass

    async def advance_context_start(self, count: int) -> bool:
        """Persist the live-context boundary after a trim removed *count*.

        Called by ``AgentLoop._trim_after_save`` right after it evicts the
        oldest turns, so a restart rebuilds the exit-time context from
        exactly where the live one stood.  Best-effort — if the memdb
        channel is unreachable the boundary just stays stale, which makes
        the next restore a *superset* (old trimmed turns come back
        searchable in context), never a loss.
        """
        if count <= 0 or not self.memdb_enabled:
            return False
        plugin = self._plugins.get("memdb")
        client = getattr(plugin, "client", None) if plugin else None
        if client is None:
            return False
        try:
            await asyncio.wait_for(
                client.call_tool(
                    "__memory_context_start_advance", {"count": count},
                ),
                timeout=10.0,
            )
            return True
        except (asyncio.TimeoutError, Exception):
            logger.warning("context_start_advance_skipped count=%d", count)
            return False

    async def set_context_start_latest(self) -> bool:
        """Flush the live-context boundary to the latest saved turn.

        Called by ``clear_context`` so the next restore is a genuine fresh
        start (only turns saved afterwards come back).  Best-effort — a
        stale boundary only over-restores.
        """
        if not self.memdb_enabled:
            return False
        plugin = self._plugins.get("memdb")
        client = getattr(plugin, "client", None) if plugin else None
        if client is None:
            return False
        try:
            await asyncio.wait_for(
                client.call_tool("__memory_context_start_latest", {}),
                timeout=10.0,
            )
            return True
        except (asyncio.TimeoutError, Exception):
            logger.warning("context_start_latest_skipped")
            return False

    def _get_memory_db_path(self) -> Path | None:
        """Return the memory database path."""
        import os
        from slife.paths import get_data_dir

        env_path = os.environ.get("SLIFE_MEMDB_DB")
        if env_path:
            return Path(env_path)
        agent_name = os.environ.get("SLIFE_AGENT_NAME", "slife")
        return get_data_dir() / f"{agent_name}.db"

    # ── Autonomous heartbeat ──────────────────────────────────────────

    def on_autonomous(self, callback) -> None:
        """Register a callback for autonomous (heartbeat) output."""
        self._on_autonomous = callback

    def on_heartbeat(self, callback) -> None:
        """Register a callback for every heartbeat outcome (quiet|act)."""
        self._on_heartbeat = callback

    def on_memory_broken(self, callback) -> None:
        """Register a callback for a fatal memory-save failure (red banner)."""
        self._on_memory_broken = callback

    def on_tunnel_down(self, callback) -> None:
        """Register a callback for a file-sharing tunnel that failed to start."""
        self._on_tunnel_down = callback

    async def surface_autonomous(self, text: str) -> None:
        """Deliver an autonomous message to the TUI (⚡ 自主)."""
        cb = self._on_autonomous
        if cb is not None:
            try:
                await cb(text)
            except Exception:
                logger.debug("surface_autonomous_error", exc_info=True)

    async def _notify_heartbeat(self, outcome: str) -> None:
        """Notify the TUI that a heartbeat beat happened (status-bar pulse)."""
        cb = self._on_heartbeat
        if cb is not None:
            try:
                await cb(outcome)
            except Exception:
                pass

    async def surface_autonomous_reply(
        self, text: str, cancelled: bool = False
    ) -> None:
        """``on_reply`` for heartbeat turns — surface only real content.

        The quiet reply is exactly ``.`` (checked in, nothing to do); any
        other non-empty text is an autonomous act worth surfacing.  Both
        outcomes are notified as a heartbeat (status-bar pulse).
        """
        t = (text or "").strip()
        if t and t != ".":
            logger.info("heartbeat_act text=%.200s", t)
            await self.surface_autonomous(t)
            await self._notify_heartbeat("act")
        else:
            logger.info("heartbeat_quiet")
            await self._notify_heartbeat("quiet")

    # ── Inbox lifecycle (always active) ────────────────────────────────

    async def start_inbox(self) -> None:
        """Start the inbox background processor.

        Called during app startup before A2A/WeChat so the queue is
        ready to accept messages from any input channel.
        """
        if self._inbox_task is not None:
            return
        self._inbox_task = asyncio.create_task(self.inbox.run())
        logger.info("inbox_started")

        # Autonomous heartbeat — main agent only (period configurable via
        # agent.heartbeat_interval).  Subagents are workers and never
        # receive a heartbeat trigger.
        if not self.is_subagent:
            from slife.agent.heartbeat import heartbeat_loop

            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(heartbeat_loop(self))
                logger.info("heartbeat_started")

    async def stop_inbox(self) -> None:
        """Stop the inbox background processor (and the heartbeat)."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._inbox_task is None:
            return
        self._inbox_task.cancel()
        try:
            await self._inbox_task
        except asyncio.CancelledError:
            pass
        self._inbox_task = None
        logger.info("inbox_stopped")

    # ── A2A lifecycle ──────────────────────────────────────────────────

    async def start_a2a(self) -> PluginStartStatus:
        """Start the A2A mesh as a plugin (thin client: connect + drain).

        All A2A logic lives in the a2a plugin process (MQTT binding) — the
        harness only spawns it, registers the ``a2a_*`` tools, and polls
        ``__a2a_drain_incoming`` for inbound tasks/presence.  Probes
        Mosquitto first; returns ``SKIPPED`` when the broker is
        unreachable or A2A is not configured (expected, not an error).
        Idempotent.
        """
        if self._plugins["a2a"].process is not None:
            return PluginStartStatus.STARTED  # already started

        a2a_cfg = self.config.a2a_config
        if a2a_cfg is None or not a2a_cfg.enabled:
            logger.debug("a2a_disabled")
            return PluginStartStatus.SKIPPED

        # Only the MQTT transport binding is implemented.  A config that
        # somehow carries a different transport (e.g. a future gRPC/HTTP
        # binding) must not silently run MQTT — skip with a warning.
        if a2a_cfg.transport != "mqtt":
            logger.warning(
                "a2a_transport_unsupported transport=%s action=a2a_disabled "
                "supported=('mqtt',)",
                a2a_cfg.transport,
            )
            return PluginStartStatus.SKIPPED

        from slife.a2a.broker import probe_broker
        if not await probe_broker(a2a_cfg.broker_host, a2a_cfg.broker_port):
            logger.info(
                "a2a_broker_not_found host=%s port=%d action=a2a_disabled",
                a2a_cfg.broker_host, a2a_cfg.broker_port,
            )
            a2a_cfg.enabled = False
            return PluginStartStatus.SKIPPED
        a2a_cfg.enabled = True

        # Pass the a2a config to the plugin process via env.
        from dataclasses import asdict
        os.environ["SLIFE_A2A_CONFIG"] = json.dumps(
            asdict(a2a_cfg), ensure_ascii=False,
        )

        started = await self._spawn_plugin_generic(
            "a2a", "slife.plugins.a2a.server",
        )
        if not started:
            return PluginStartStatus.FAILED

        # Expose the mesh client for harness drain/dispatch plumbing (when the
        # plugin is down this stays None).
        self._tool_ctx.a2a_mcp_client = self._plugins["a2a"].client
        self._plugins["a2a"].poll_task = asyncio.create_task(self._a2a_poll_loop())

        # Crash watchdog — respawn the plugin and restart the drain loop.
        async def _restart_a2a() -> None:
            self._cancel_plugin_task("a2a")
            await self._spawn_plugin_generic("a2a", "slife.plugins.a2a.server")
            self._tool_ctx.a2a_mcp_client = self._plugins["a2a"].client
            self._plugins["a2a"].poll_task = asyncio.create_task(self._a2a_poll_loop())

        self._plugins["a2a"].start_watchdog(restart_cb=_restart_a2a)

        from slife.health import record
        record(
            "a2a", "ok",
            key="status", value="connected",
            hint="A2A P2P mesh connected (plugin).",
        )
        logger.info("a2a_plugin_started")
        return PluginStartStatus.STARTED

    def _cancel_plugin_task(self, name: str, attr: str = "poll_task") -> None:
        """Cancel and clear a plugin's background task (e.g. the WeChat/A2A
        poll loop) so a watchdog restart never stacks a second concurrent
        loop. Cancellation is safe: both poll loops catch ``CancelledError``
        and exit."""
        plugin = self._plugins.get(name)
        if plugin is None:
            return
        task = getattr(plugin, attr, None)
        if task is not None and not task.done():
            task.cancel()
        setattr(plugin, attr, None)

    async def _a2a_poll_loop(self, interval: float = 1.0) -> None:
        """Drain inbound a2a tasks/presence from the plugin into the inbox.

        The harness stays a thin client: it only drains the plugin's
        ``__a2a_drain_incoming`` and feeds the unified inbox.  Replies are
        routed back through the plugin via ``__a2a_dispatch_result``.
        """
        import json as _json
        from slife.a2a.identity import AgentName, AgentMessage
        from slife.a2a.card import AgentCard, format_presence_line

        logger.info("a2a_poll_loop_start interval=%.1fs", interval)

        while True:
            try:
                client = self._plugins["a2a"].client
                if client is None:
                    break
                result = await client.call_tool("__a2a_drain_incoming", {})
                data = _json.loads(result)

                # A peer cancelled a task — drop it if still queued, or stop
                # the running loop if it is the message being processed
                # (Esc-equivalent).
                for cev in data.get("cancellations", []):
                    cid = cev.get("corr_id", "")
                    if cid:
                        self.inbox.cancel_correlation(cid)

                a2a_client = client  # narrowed MCPClient for the reply closure
                for ev in data.get("tasks", []):
                    async def _reply(
                        reply_text: str, cancelled: bool = False,
                        rt=ev.get("reply_to", ""),
                        cid=ev.get("correlation_id", ""),
                    ) -> None:
                        try:
                            assert a2a_client is not None
                            await a2a_client.call_tool("__a2a_dispatch_result", {
                                "reply_to": rt, "corr_id": cid, "text": reply_text,
                                "cancelled": cancelled,
                            })
                        except Exception:
                            pass

                    # The sender knows the task_id (a2a_send_task_async returns
                    # it); surface the same id to the receiver so it can
                    # reference the task it is responding to instead of making
                    # one up (a reported mismatch in round-trips).
                    task_text = ev.get("content", "")
                    corr_id = ev.get("correlation_id", "")
                    src = ev.get("source", "unknown")
                    if corr_id:
                        task_text = f"[Task {corr_id} from {src}] {task_text}"
                    msg = AgentMessage(
                        source=AgentName(src),
                        content=task_text,
                        reply_to=ev.get("reply_to", ""),
                        correlation_id=ev.get("correlation_id", ""),
                        on_reply=_reply,
                    )
                    await self.inbox.post(msg)
                    logger.debug(
                        "a2a_in source=%s task=%.80s",
                        msg.source, ev.get("content", ""),
                    )

                for pev in data.get("presence", []):
                    # The presence card comes off the wire from any peer —
                    # guard the shape so a malformed entry can't crash the
                    # drain loop (which would freeze all A2A processing).
                    card_data = pev.get("card") if isinstance(pev, dict) else None
                    if not isinstance(card_data, dict):
                        continue
                    card = AgentCard(
                        agent_name=AgentName(card_data.get("agent_name", "?")),
                        status=card_data.get("status", "idle"),
                    )
                    text = format_presence_line(card, pev.get("event", ""))
                    if text is not None:
                        self._presence_events.append((_time.time(), text))
                    await self._notify_a2a_activity(
                        "agent_change", event=pev.get("event", ""), card=card,
                    )

                # Outbound async-task results (auto-push) — the peer's result
                # arrived over MQTT; surface it so the agent doesn't need to
                # poll or block on subscribe.
                for cev in data.get("task_completions", []):
                    corr_id = cev.get("corr_id", "")
                    result = cev.get("result", "")
                    peer = cev.get("peer", "") or corr_id or "peer"
                    if not result:
                        continue
                    state = "cancelled" if cev.get("cancelled") else "completed"
                    await self.inbox.post(AgentMessage(
                        source=AgentName(peer),
                        content=(
                            f"Peer **{peer}** {state} async task "
                            f"(ID: `{corr_id}`):\n\n{result}"
                        ),
                    ))
                    logger.debug("a2a_task_completed_autopushed peer=%s task=%s", peer, corr_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("a2a_poll_error err=%s", e)

            await asyncio.sleep(interval)

        logger.info("a2a_poll_loop_stop")


    def set_inbox_handler_factory(self, factory) -> None:
        """Register a factory that creates TUI handlers for inbox messages.

        Called by the TUI layer so remote A2A tasks always have a handler
        available, even before the first human message is typed.
        """
        if self.inbox is not None:
            self.inbox._conversations.set_default_handler_factory(factory)

        logger.info("a2a_init_done tools=%d", len(self.tool_registry.list_tools()))

    async def stop_a2a(self) -> None:
        """Leave the P2P mesh — stop the drain loop and the a2a plugin.

        Does NOT stop the inbox — the queue is independent of A2A
        and may still be used by human input / WeChat.
        """
        # PluginLifecycle.stop() sets _stopping first so the watchdog does
        # not spuriously restart the plugin on a graceful shutdown.
        await self._stop_plugin("a2a", has_poll_task=True)

        # Clear inbox a2a references
        if self.inbox is not None:
            self.inbox._a2a_client = None
            self.inbox._on_activity = None

        logger.info("a2a_shutdown")

    # ── Subagent lifecycle ─────────────────────────────────────────────

    async def start_subagent(self) -> None:
        """Set up local subagent spawning (stdin/stdout pipes).

        Recursion is allowed — a subagent can spawn its own descendants.

        Independent of A2A over MQTT — both transports coexist.
        """
        logger.info("subagent_init_start")

        from slife.subagent.process import SubagentManager, set_manager
        self._subagent_manager = SubagentManager(self.config)

        # Set module-level transport reference so native subagent tools
        # (Slife.tools.subagent) can access the live manager at call time.
        set_manager(self._subagent_manager)

        # When a subagent completes an async task, push the result into
        # the inbox so the user sees it without having to poll.
        async def _on_subagent_done(agent_name: str, task_id: str, result: str) -> None:
            from slife.a2a.identity import AgentMessage
            from slife.subagent.identity import SUBAGENT
            msg = AgentMessage(
                source=SUBAGENT,
                content=(
                    f"Subagent **{agent_name}** completed async task "
                    f"(ID: `{task_id}`):\n\n"
                    f"{result}"
                ),
            )
            await self.inbox.post(msg)

        self._subagent_manager.on_task_complete = _on_subagent_done

        logger.info("subagent_init_done tools=%d", len(self.tool_registry.list_tools()))
        from slife.health import record
        record(
            "subagent", "ok",
            key="status", value="ready",
            hint=f"Subagent manager ready (max_subagents={(self.config.subagent_config or {}).get('max_subagents', '?')}).",
        )

    async def stop_subagent(self) -> None:
        """Stop all local subagents and clean up."""
        if self._subagent_manager:
            try:
                await self._subagent_manager.stop_all()
            except Exception as e:
                logger.debug("subagent_stop_all_error err=%s", e)
            self._subagent_manager = None

        # Clear module-level transport reference
        from slife.subagent.process import clear_manager
        clear_manager()

        logger.info("subagent_shutdown")

    def _drain_presence_events(self) -> list[tuple[float, str]]:
        """Return pending presence events and clear the buffer.

        Called by ``AgentLoop`` at the start of each turn (read-once):
        events that happened since the last turn are injected into the
        context footer exactly once.  If the buffer ever grows
        pathologically large it is trimmed here, not silently at
        render time — the oldest entries are dropped with a warning.
        """
        if not self._presence_events:
            return []
        if len(self._presence_events) > 1000:
            logger.warning(
                "presence_events_overflow dropped=%d",
                len(self._presence_events) - 1000,
            )
        events = list(self._presence_events)[-1000:]
        self._presence_events.clear()
        return events

    async def _notify_a2a_activity(self, kind: str, **kwargs) -> None:
        """Fire all registered A2A activity callbacks."""
        for cb in self._on_a2a_callbacks:
            try:
                await cb(kind, **kwargs)
            except Exception:
                pass

    def on_a2a_activity(self, callback) -> None:
        """Register a callback for A2A events (TUI notification).

        Callback signature: ``async def cb(kind: str, **kwargs)``
        where *kind* is ``"agent_change"``, ``"task_received"``, or
        ``"task_completed"``.
        """
        self._on_a2a_callbacks.append(callback)

    # ── Message processing ────────────────────────────────────────────

    async def process_message(
        self,
        user_input: str,
        images: list[str] | None,
        handler: AgentEventHandler,
    ) -> AgentResult:
        """Run the agent loop for a user message via streaming.

        All messages (human keyboard, A2A, WeChat) go through the
        unified inbox queue — processed serially, never cancelled.
        """
        from slife.a2a.identity import AgentMessage

        msg = AgentMessage(
            source=HUMAN,
            content=user_input,
            images=images if images else [],
            handler=handler,
        )
        await self.inbox.post(msg)

        # Return a placeholder — TUIHandler will update the UI
        # as streaming events arrive.  The actual result is not
        # available synchronously with the inbox model.
        return AgentResult(text="", usage=TokenUsage())
