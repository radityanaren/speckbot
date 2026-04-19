"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from speckbot.agent.context import ContextBuilder
from speckbot.agent.memory import MemoryConsolidator
from speckbot.agent.skills import BUILTIN_SKILLS_DIR
from speckbot.agent.subagent import SubagentManager
from speckbot.agent.tools.cron import CronTool
from speckbot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from speckbot.agent.tools.message import MessageTool
from speckbot.agent.tools.registry import ToolRegistry
from speckbot.agent.tools.bash import BashTool
from speckbot.agent.tools.spawn import SpawnTool
from speckbot.agent.tools.web import WebFetchTool, WebSearchTool
from speckbot.bus.events import InboundMessage, OutboundMessage
from speckbot.bus.queue import MessageBus
from speckbot.providers.base import LLMProvider
from speckbot.services.cron import CronService
from speckbot.services.monologue import MonologueSystem
from speckbot.services.timer import UnifiedTimer
from speckbot.session.manager import Session, SessionManager
from speckbot.utils.constants import TOOL_RESULT_MAX_CHARS

if TYPE_CHECKING:
    from speckbot.config.schema import ChannelsConfig, ExecToolConfig, WebSearchConfig


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = TOOL_RESULT_MAX_CHARS

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        active_window_tokens: int = 65_536,
        context_headroom: int = 20,
        tool_result_max_chars: int = 10_000,
        summary_config: dict | None = None,
        web_search_config: WebSearchConfig | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        hooks_config: dict[str, Any] | None = None,
        monologue_config: dict | None = None,
        unified_timer=None,  # For resetting monologue counter on user messages
    ):
        from speckbot.agent.memory import SummaryConfig
        from speckbot.config.schema import ExecToolConfig, WebSearchConfig

        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.active_window_tokens = active_window_tokens
        self.context_headroom = context_headroom
        self.tool_result_max_chars = tool_result_max_chars
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        # No more presets - history/journal handled by conveyor belt

        # Create shared security service first
        from speckbot.agent.security import SecurityService

        self.security = SecurityService(hooks_config, workspace)

        # Pending confirmation state - one at a time, blocks all LLM input until resolved
        self._pending_confirmation: dict[
            str, dict
        ] = {}  # session_key -> {tool_name, params, tool_call_id}

        self.context = ContextBuilder(workspace, security=self.security)
        self.sessions = session_manager or SessionManager(workspace)

        # Initialize monologue system after sessions
        self.monologue = MonologueSystem(
            bus=bus,
            sessions=self.sessions,
            workspace=workspace,
            config=monologue_config,
            agent=self,  # Pass self for pending confirmation check
        )

        # Configure context with monologue settings for identity prompt
        self.context.set_monologue_config(
            enabled=self.monologue._enabled,
            idle_seconds=self.monologue._idle_seconds,
        )
        # Configure tool result max chars for the agent's knowledge
        self.context.set_tool_result_max_chars(self.tool_result_max_chars)

        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        # Unified timer reference for resetting monologue on user messages
        self._unified_timer = unified_timer

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._processing_lock = asyncio.Lock()
        self.tools = ToolRegistry(hooks_config, workspace=workspace, security=self.security)

        # Create summary config for conveyor belt
        if summary_config is None:
            summary_config = {}
        mc_summary_config = SummaryConfig(
            enabled=summary_config.get("enabled", True),
            user_max_chars=summary_config.get("user_max_chars", 100),
            tool_max_chars=summary_config.get("tool_max_chars", 80),
            result_max_chars=summary_config.get("result_max_chars", 100),
            assistant_max_chars=summary_config.get("assistant_max_chars", 150),
        )

        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            active_window_tokens=active_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            context_headroom=context_headroom,
            summary_config=mc_summary_config,
        )
        self._register_default_tools()

        # Set the process callback for monologue system (after tools is initialized)
        self.monologue.set_process_callback(self._process_message)

        # Initialize message handler (separated logic from orchestration)
        self._message_handler = MessageHandler(self)

    # =============================================================================
    # Pending Confirmation Management (Security: Ask System)
    # =============================================================================

    def set_pending_confirmation(
        self, session_key: str, tool_name: str, params: dict, tool_call_id: str = None
    ) -> None:
        """Set a pending confirmation for a tool. Only one at a time."""
        self._pending_confirmation = {
            session_key: {
                "tool_name": tool_name,
                "params": params,
                "tool_call_id": tool_call_id,
            }
        }
        logger.info(f"[Security] Pending confirmation set for {tool_name} in session {session_key}")

    def get_pending_confirmation(self, session_key: str) -> dict | None:
        """Get pending confirmation for a session."""
        return self._pending_confirmation.get(session_key)

    def clear_pending_confirmation(self, session_key: str) -> None:
        """Clear pending confirmation for a session."""
        if session_key in self._pending_confirmation:
            del self._pending_confirmation[session_key]
            logger.info(f"[Security] Pending confirmation cleared for session {session_key}")

    def has_pending_confirmation(self, session_key: str) -> bool:
        """Check if there's a pending confirmation for a session."""
        return session_key in self._pending_confirmation

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(
            ReadFileTool(
                workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read
            )
        )
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(
            BashTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
                bash_path=self.exec_config.bash_path,
            )
        )
        self.tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from speckbot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        session_key: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self.tools.get_definitions()

            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=self.model,
            )

            if response.has_tool_calls:
                if on_progress:
                    # Content already cleaned by provider
                    thought = response.content
                    if thought:
                        await on_progress(thought)
                    tool_hint = self._tool_hint(response.tool_calls)
                    await on_progress(tool_hint, tool_hint=True)

                tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])

                    # SECURITY: Check if tool requires confirmation (ASK system)
                    # Get ask_tools from security config
                    ask_tools = []
                    if self.security and self.security.enabled:
                        from speckbot.config.schema import SecurityConfig

                        # Check config for ask_tools
                        if hasattr(self.security, "_config") and self.security._config:
                            ask_tools = self.security._config.get("ask_tools", [])

                    # If tool requires confirmation, ask user INSTANTLY (no LLM)
                    if tool_call.name in ask_tools:
                        # Set pending confirmation (blocks all LLM input)
                        self.set_pending_confirmation(
                            session_key=session_key,
                            tool_name=tool_call.name,
                            params=tool_call.arguments,
                            tool_call_id=tool_call.id,
                        )

                        # Format params for display
                        params_str = ", ".join(f"{k}={v}" for k, v in tool_call.arguments.items())
                        # Send confirmation prompt DIRECTLY to user via bus (INSTANT, no LLM)
                        from speckbot.bus.events import OutboundMessage

                        # Extract channel/chat from session_key
                        if session_key and ":" in session_key:
                            ch, ch_id = session_key.split(":", 1)
                        else:
                            ch, ch_id = "cli", session_key or "default"

                        confirm_msg = OutboundMessage(
                            channel=ch,
                            chat_id=ch_id,
                            content=f"⚠️ Please confirm: {tool_call.name}({params_str})? Reply with 'yes' or 'no'",
                        )
                        await self.bus.publish_outbound(confirm_msg)

                        # Return empty result to LLM (don't wait for response)
                        result = "⏳ Waiting for user confirmation..."
                    else:
                        # Execute normally
                        result = await self.tools.execute(
                            tool_call.name, tool_call.arguments, session_key=session_key
                        )

                    # Security: Scan tool output before AI sees it
                    if self.security and self.security.enabled:
                        scan_result = self.security.scan_tool_output(result)
                        if scan_result.is_blocked:
                            pattern_info = scan_result.reason or "unknown pattern"
                            logger.warning(
                                f"⚠️ WARNING: Tool '{tool_call.name}' output matched blocked pattern: {pattern_info}. Data may have leaked."
                            )
                            result = "[Output filtered by security]"

                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # Content already cleaned by provider
                clean = response.content
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        self.monologue.is_running = True
        # Note: monologue timer is now handled by UnifiedTimer, not internal timer
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                # Wait indefinitely for next message (timer handles monologue timing)
                msg = await self.bus.consume_inbound()
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            cmd = msg.content.strip().lower()
            if cmd == "/stop":
                await self._handle_stop(msg)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(
                    lambda t, k=msg.session_key: self._active_tasks.get(k, [])
                    and self._active_tasks[k].remove(t)
                    if t in self._active_tasks.get(k, [])
                    else None
                )

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
            )
        )

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        await asyncio.sleep(0.3)
        os.execv(sys.executable, [sys.executable, "-m", "speckbot"] + sys.argv[1:])

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock. Restart idle timer after saving to session."""
        async with self._processing_lock:
            # If user sends a message, cancel any pending normal chat from ACTION
            await self.monologue.on_user_message()

            try:
                response = await self._process_message(msg)

                if response is not None:
                    # SECURITY: Scan AI output before sending to user
                    if self.security and self.security.enabled:
                        scan_result = self.security.scan_output(response.content or "")
                        if scan_result.is_blocked:
                            logger.warning("[Security] Blocking AI output with sensitive content")
                            response.content = "[BLOCKED - sensitive content]"
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=msg.metadata or {},
                        )
                    )
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    )
                )
            finally:
                # Reset monologue counter on user message (via UnifiedTimer if available)
                if hasattr(self, "_unified_timer"):
                    self._unified_timer.reset_monologue_timer()
                elif self.monologue:
                    self.monologue.restart_idle_timer()

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        self.monologue.is_running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response.

        Delegates to MessageHandler for the actual processing logic.
        AgentLoop is the orchestrator, MessageHandler does the work.
        """
        return await self._message_handler.process(msg, session_key, on_progress)

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context

            # MARK SKIP: Explicitly mark assistant response AFTER tool results
            # This is used by the conveyor belt to skip summarization
            if role == "assistant" and session.messages:
                last_msg = session.messages[-1]
                if last_msg.get("role") == "tool":
                    entry["_is_skip"] = True

            if (
                role == "tool"
                and isinstance(content, str)
                and len(content) > self.tool_result_max_chars
            ):
                entry["content"] = content[: self.tool_result_max_chars] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(
                    ContextBuilder._RUNTIME_CONTEXT_TAG
                ):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if (
                            c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                        ):
                            continue  # Strip runtime context from multimodal messages
                        if c.get("type") == "image_url" and c.get("image_url", {}).get(
                            "url", ""
                        ).startswith("data:image/"):
                            path = (c.get("_meta") or {}).get("path", "")
                            placeholder = f"[image: {path}]" if path else "[image]"
                            filtered.append({"type": "text", "text": placeholder})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(
            msg, session_key=session_key, on_progress=on_progress
        )
        return response.content if response else ""

    # =============================================================================


# MessageHandler - Handles message processing logic (extracted from _process_message)
# =============================================================================


class MessageHandler:
    """
        Handles the logic for processing messages.

        Separated from AgentLoop to keep the orchestrator clean.
    AgentLoop dispatches, MessageHandler does the work.
    """

    def __init__(self, agent: "AgentLoop"):
        self._agent = agent

    async def process(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # Get session key for confirmation check
        key = session_key or msg.session_key

        # SECURITY: Check if waiting for confirmation - block ALL non-user input
        pending = self._agent.get_pending_confirmation(key)

        # Also check all pending confirmations
        if not pending:
            # Try checking with msg.chat_id directly
            alt_key = f"{msg.channel}:{msg.chat_id}" if msg.channel and msg.chat_id else None
            if alt_key and alt_key != key:
                pending = self._agent.get_pending_confirmation(alt_key)
                if pending:
                    key = alt_key  # Use the alternative key
                    logger.debug(f"[Security Debug] Using alt key: {alt_key}")

        if pending:
            logger.debug(f"[Security Debug] Pending found for key={key}, sender_id={msg.sender_id}")

            # Only accept direct user messages (channel != "system")
            # Block: system messages (monologue/subagent) - let Discord users through
            if msg.channel == "system":
                logger.debug("[Security] Blocking system input while waiting for confirmation")
                return None

            # Handle user response
            response = msg.content.strip().lower()
            logger.debug(f"[Security Debug] User response: {response}")

            if response == "yes":
                # Execute the pending tool
                tool_name = pending["tool_name"]
                params = pending["params"]
                tool_call_id = pending.get("tool_call_id")

                logger.info(f"[Security] User confirmed: {tool_name}")
                result = await self._agent.tools.execute(tool_name, params, session_key=key)

                # Send result to user
                response_msg = OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"✅ Executed {tool_name}: {result}",
                )
                await self._agent.bus.publish_outbound(response_msg)

                # Clear pending confirmation
                self._agent.clear_pending_confirmation(key)
                return None

            elif response == "no":
                # Tell AI the tool was cancelled, send simple message to user
                tool_name = pending["tool_name"]
                tool_call_id = pending.get("tool_call_id")
                logger.info(f"[Security] User denied: {tool_name}")
                self._agent.clear_pending_confirmation(key)

                # Send cancellation message to user
                user_msg = OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"❌ Cancelled: {tool_name} was not executed.",
                )
                await self._agent.bus.publish_outbound(user_msg)

                # Inject cancellation message into session so AI knows
                session = self._agent.sessions.get_or_create(key)
                if session:
                    session.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": "your tool call has been cancelled by user",
                        }
                    )
                return None
            else:
                # User said something other than yes/no - ask again
                params_str = ", ".join(f"{k}={v}" for k, v in pending["params"].items())
                response_msg = OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"⚠️ Please confirm: {pending['tool_name']}({params_str})? Reply with 'yes' or 'no'",
                )
                await self._agent.bus.publish_outbound(response_msg)
                return None

        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            return await self._handle_system_message(msg)

        # Slash commands
        cmd = msg.content.strip().lower()
        if slash_response := self._handle_slash_command(msg, cmd):
            return slash_response

        # Normal user message
        return await self._handle_user_message(msg, session_key, on_progress)

    async def _handle_system_message(self, msg: InboundMessage) -> OutboundMessage:
        """Handle messages from subagents (channel='system')."""
        channel, chat_id = msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        logger.info("Processing system message from {}", msg.sender_id)

        key = f"{channel}:{chat_id}"
        session = self._agent.sessions.get_or_create(key)
        history = session.get_history(
            max_messages=10,
            active_window_tokens=self._agent.active_window_tokens,
        )
        # Get context summary from conveyor belt
        context_summary = session.get_context_summary()

        # Subagent results should be assistant role, other system messages use user role
        current_role = "assistant" if msg.sender_id == "subagent" else "user"
        messages = self._agent.context.build_messages(
            history=history,
            current_message=msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            session_key=msg.session_key,
            user_id=msg.user_id,
            username=msg.username,
            journal_entries=10,  # Load last 10 journal entries
            context_summary=context_summary,
        )

        final_content, _, all_msgs = await self._agent._run_agent_loop(
            messages, session_key=msg.session_key
        )

        self._save_turn(session, all_msgs, 1 + len(history))
        self._agent.sessions.save(session)
        self._agent._schedule_background(
            self._agent.memory_consolidator.maybe_archive_by_tokens(session)
        )

        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content or "Background task completed.",
        )

    def _handle_slash_command(self, msg: InboundMessage, cmd: str) -> OutboundMessage | None:
        """Handle slash commands: /new, /help, /memories."""
        key = msg.session_key
        session = self._agent.sessions.get_or_create(key)

        if cmd == "/new":
            snapshot = session.messages[session.last_consolidated :]
            session.clear()
            self._agent.sessions.save(session)
            self._agent.sessions.invalidate(session.key)

            if snapshot:
                self._agent._schedule_background(
                    self._agent.memory_consolidator.archive_messages(snapshot)
                )

            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content="New session started."
            )

        if cmd == "/help":
            from speckbot.agent.definitions import get_help_text

            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=get_help_text(),
            )

        if cmd == "/memories":
            store = self._agent.memory_consolidator.store
            knowledges = store.list_knowledges()
            projects = store.list_projects()

            lines = ["🐜 SpeckBot Memory", ""]

            if knowledges:
                lines.append("📚 Knowledges:")
                for topic in knowledges:
                    files = store.list_knowledge_files(topic)
                    if files:
                        lines.append(f"  • {topic} ({', '.join(files)})")
                    else:
                        lines.append(f"  • {topic}")
            else:
                lines.append("📚 Knowledges: (empty)")

            lines.append("")

            if projects:
                lines.append("📁 Projects:")
                for topic in projects:
                    files = store.list_project_files(topic)
                    if files:
                        lines.append(f"  • {topic} ({', '.join(files)})")
                    else:
                        lines.append(f"  • {topic}")
            else:
                lines.append("📁 Projects: (empty)")

            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="\n".join(lines),
            )

        return None  # Not a slash command

    async def _handle_user_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Handle normal user messages."""
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self._agent.sessions.get_or_create(key)

        # Run memory consolidation before processing
        # Safety Layer 2: Don't let archiving errors crash the request
        try:
            await self._agent.memory_consolidator.maybe_archive_by_tokens(session)
        except Exception:
            logger.exception("Memory consolidation failed, continuing anyway")

        # Set tool context
        self._agent._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self._agent.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # Build context with history (default 10 messages)
        history = session.get_history(
            max_messages=10,
            active_window_tokens=self._agent.active_window_tokens,
        )
        # Get context summary from conveyor belt
        context_summary = session.get_context_summary()
        initial_messages = self._agent.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key=msg.session_key,
            user_id=msg.user_id,
            username=msg.username,
            journal_entries=10,  # Load last 10 journal entries
            context_summary=context_summary,
        )

        # Progress callback
        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            await self._agent.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    progress_type="tool_hint" if tool_hint else "thought",
                )
            )

        # Run the agent
        final_content, _, all_msgs = await self._agent._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            session_key=session.key if session else None,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Save to session
        self._save_turn(session, all_msgs, 1 + len(history))
        self._agent.sessions.save(session)
        self._agent._schedule_background(
            self._agent.memory_consolidator.maybe_archive_by_tokens(session)
        )

        # Check if message was sent via tool (don't duplicate)
        if (
            (mt := self._agent.tools.get("message"))
            and isinstance(mt, MessageTool)
            and mt._sent_in_turn
        ):
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context

            # MARK SKIP: Explicitly mark assistant response AFTER tool results
            if role == "assistant" and session.messages:
                last_msg = session.messages[-1]
                if last_msg.get("role") == "tool":
                    entry["_is_skip"] = True

            if (
                role == "tool"
                and isinstance(content, str)
                and len(content) > self._agent.tool_result_max_chars
            ):
                entry["content"] = (
                    content[: self._agent.tool_result_max_chars] + "\n... (truncated)"
                )
            elif role == "user":
                if isinstance(content, str) and content.startswith(
                    ContextBuilder._RUNTIME_CONTEXT_TAG
                ):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if (
                            c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                        ):
                            continue  # Strip runtime context from multimodal messages
                        if c.get("type") == "image_url" and c.get("image_url", {}).get(
                            "url", ""
                        ).startswith("data:image/"):
                            path = (c.get("_meta") or {}).get("path", "")
                            placeholder = f"[image: {path}]" if path else "[image]"
                            filtered.append({"type": "text", "text": placeholder})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()
