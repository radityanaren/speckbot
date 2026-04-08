"""Monologue system - inner thoughts when idle."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from speckbot.bus.events import InboundMessage

# Character limits per channel for outbound monologue messages
# Leave buffer for "💭\n" prefix
CHANNEL_OUTBOUND_LIMITS = {
    "discord": 1900,
    "telegram": 4000,
    "slack": 3000,
    "cli": 5000,
    "default": 2000,
}

if TYPE_CHECKING:
    from speckbot.bus.events import OutboundMessage
    from speckbot.bus.queue import MessageBus
    from speckbot.session.manager import Session, SessionManager


class MonologueSystem:
    """
    Manages inner monologue cycles when the agent is idle.

    Flow:
    1. Idle timer fires after idle_seconds
    2. If previous monologue had <ACTION> → normal chat mode (agent can message user)
    3. Otherwise → monologue mode (private thinking, journal only)
    4. If monologue has <ACTION> → trigger normal chat on NEXT cycle
    """

    def __init__(
        self,
        bus: MessageBus,
        sessions: SessionManager,
        workspace: Path,
        config: dict | None = None,
    ):
        self.bus = bus
        self.sessions = sessions
        self.workspace = workspace
        self._running = False
        self._idle_timer_task: asyncio.Task | None = None
        self._process_callback = None  # Callback to process messages

        # Config
        self._enabled = config.get("enabled", False) if config else False
        self._idle_seconds = (
            (config.get("idle_seconds") or config.get("idleSeconds") or 300) if config else 300
        )
        self._prompt = (
            config.get("prompt", "Hey, been a while — what are you working on?")
            if config
            else "Hey, been a while — what are you working on?"
        )
        self._visible = config.get("visible", True) if config else True

        logger.info(
            "Monologue: enabled={}, idle={}s, visible={}",
            self._enabled,
            self._idle_seconds,
            self._visible,
        )

    def set_process_callback(self, callback) -> None:
        """Set the callback to process messages (AgentLoop._process_message)."""
        self._process_callback = callback

    @property
    def is_running(self) -> bool:
        return self._running

    @is_running.setter
    def is_running(self, value: bool) -> None:
        self._running = value

    def restart_idle_timer(self) -> None:
        """Restart the idle timer - cancels existing timer and starts a new one."""
        if not self._enabled:
            return
        # Cancel existing timer if any
        if self._idle_timer_task and not self._idle_timer_task.done():
            self._idle_timer_task.cancel()
        # Start new timer task
        self._idle_timer_task = asyncio.create_task(self._idle_timer_run())

    async def write_journal(self, entry: str) -> None:
        """Write a journal entry to JOURNAL.md."""
        from speckbot.utils.helpers import current_time_str

        journal_file = self.workspace / "JOURNAL.md"
        current = ""
        if journal_file.exists():
            current = journal_file.read_text(encoding="utf-8") or ""

        timestamp = current_time_str().split(",")[0]
        new_entry = f"- [{timestamp}] {entry}\n"

        if current:
            updated = current.rstrip() + "\n" + new_entry
        else:
            updated = f"# Journal\n{new_entry}"

        journal_file.write_text(updated, encoding="utf-8")
        logger.info("Journal: wrote entry")

    def _build_prompt(self, last_user_message: str = "") -> str:
        """Build the prompt based on mode."""
        last_msg_ctx = f"\nLast user message: {last_user_message}" if last_user_message else ""
        return f"""[SYSTEM - INNER MONOLOGUE]
User(s) has been gone for {last_msg_ctx} seconds.

RULES:
1. DO NOT message user in this answer, they won't see it, you need to call tool for that.
2. This Answer should be for your OWN THOUGHT, NOT USER.
3. If restating previous thought → hard pivot.

ANSWER THIS SHORT AND SIMPLE: {self._prompt}

You CAN use tools but ask for permission to the upstream message using message tool."""

    def _find_recent_session(self) -> tuple[Session | None, str | None]:
        """Find the most recently active session that has recent user activity."""
        if not self.sessions._cache:
            return None, None

        # Find session with most recent messages (prefer active conversations)
        recent_session = None
        recent_key = None
        latest_update = None

        for key, session in self.sessions._cache.items():
            if session.messages:
                # Get the last message timestamp in this session
                last_msg = session.messages[-1] if session.messages else None
                if last_msg:
                    last_time = last_msg.get("timestamp")
                    if last_time:
                        # Parse timestamp
                        from datetime import datetime

                        try:
                            if isinstance(last_time, str):
                                last_time = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
                        except:
                            pass

                        if latest_update is None or (last_time and last_time > latest_update):
                            latest_update = last_time
                            recent_session = session
                            recent_key = key

        return recent_session, recent_key

    async def handle_idle(
        self,
        process_callback,  # AsyncFunction(msg, session_key) -> OutboundMessage | None
    ) -> None:
        """Handle idle timer firing - process the monologue/normal chat."""
        # Find most recent session
        recent_session, recent_key = self._find_recent_session()
        if not recent_session:
            logger.debug("Idle: no session with messages")
            return

        # Parse channel and chat_id from session key
        if recent_key and ":" in recent_key:
            channel, chat_id = recent_key.split(":", 1)
        else:
            channel, chat_id = "cli", recent_key or "default"

        # Extract last user message from session for context
        last_user_msg = ""
        if recent_session and recent_session.messages:
            for msg_data in reversed(recent_session.messages):
                if msg_data.get("role") == "user":
                    content = msg_data.get("content", "")
                    # Strip runtime context tag if present
                    if "[Runtime Context" in content:
                        content = content.split("\n\n", 1)[-1] if "\n\n" in content else content
                    last_user_msg = content.strip()[:200]  # Limit to 200 chars
                    break

        full_prompt = self._build_prompt(last_user_msg)

        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=full_prompt,
            metadata={"is_idle_prompt": True},
            session_key_override=recent_key,
        )

        logger.info(
            "Idle: injecting prompt into session {} (channel={}, chat_id={})",
            recent_key,
            channel,
            chat_id,
        )

        # Process through agent - use silent progress callback in invisible mode
        if self._visible:
            response = await process_callback(msg, recent_key)
        else:
            # In invisible mode, create a silent callback that drops all progress messages
            async def silent_progress(content: str, *, tool_hint: bool = False) -> None:
                pass  # Drop all thoughts/tool hints in invisible mode

            response = await process_callback(msg, recent_key, on_progress=silent_progress)

        # Handle the response
        if response:
            # Always journal the response
            await self.write_journal(response.content)

            # Send to user based on visible setting
            if self._visible:
                # Clean model output tokens from response
                cleaned_content = response.content
                # Remove lines that start with <|
                lines = cleaned_content.split("\n")
                cleaned_lines = [line for line in lines if not line.strip().startswith("<|")]
                cleaned_content = "\n".join(cleaned_lines)

                # Truncate if needed - ONLY for outbound, journal gets full content
                char_limit = CHANNEL_OUTBOUND_LIMITS.get(
                    channel, CHANNEL_OUTBOUND_LIMITS["default"]
                )
                if len(cleaned_content) > char_limit:
                    cleaned_content = cleaned_content[:char_limit] + "\n...(truncated)"

                response.content = f"💭 {cleaned_content}"
                await self.bus.publish_outbound(response)
            else:
                logger.info("Idle: journaled (invisible) to {}", recent_key)

        else:
            # Agent used message tool (no direct response)
            logger.info("Idle: no direct response (agent used message tool)")

    async def _idle_timer_run(self) -> None:
        """The idle timer coroutine - waits then triggers monologue."""
        try:
            await asyncio.sleep(self._idle_seconds)

            if not self._enabled or not self._running:
                return

            if not self._prompt or not self._prompt.strip():
                logger.debug("Idle: prompt is empty, skipping")
                return

            if not self._process_callback:
                logger.warning("Idle: no process callback set, skipping")
                return

            # Handle the idle cycle using the callback
            await self.handle_idle(self._process_callback)

        except asyncio.CancelledError:
            pass  # Expected when user sends a message
        else:
            # Restart timer after completion (unless cancelled)
            if self._enabled and self._running:
                self.restart_idle_timer()

    async def on_user_message(self) -> None:
        """Called when user sends a message."""
        # Just log - nothing to cancel anymore
        pass
