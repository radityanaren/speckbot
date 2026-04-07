"""Monologue system - inner thoughts when idle."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from speckbot.bus.events import InboundMessage

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
        self._next_is_normal: bool = False  # If True, next idle is normal chat (from ACTION)
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

    def _build_prompt(self, is_normal_chat: bool) -> str:
        """Build the prompt based on mode."""
        if is_normal_chat:
            return f"""The user has been idle for {self._idle_seconds} seconds.
This is a normal chat turn - your response will be visible to the user.
You can check in on them, share something, or just say hi.
You are free to use any tools if needed."""
        else:
            return f"""[SYSTEM - INNER MONOLOGUE]
You have been idle for {self._idle_seconds} seconds.
This message is automatic. The user cannot see your response.

RULES:
1. You are thinking privately. Be honest, not performative.
2. Check your last thought. If you are repeating it → change direction completely.
3. Check the user's last message. Are they gone? Are they coming back?
4. Do NOT message the user. Do NOT use tools. Just think.

THEN ANSWER THIS:
{self._prompt}

IF anything came up worth acting on, end with:
<ACTION> what you want to do and why </ACTION>
you would be able to call tools on the next monologue"""

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

    def _clean_output(self, content: str) -> str:
        """Remove all <tag> patterns from output."""
        # Remove any <...> tags
        cleaned = re.sub(r"<[^>]+>", "", content).strip()
        return cleaned

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

        # Check if this should be a normal chat (triggered by ACTION from previous monologue)
        is_normal_chat = self._next_is_normal
        if is_normal_chat:
            self._next_is_normal = False  # Reset flag

        full_prompt = self._build_prompt(is_normal_chat)

        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=full_prompt,
            metadata={"is_idle_prompt": True, "is_normal_chat": is_normal_chat},
            session_key_override=recent_key,
        )

        logger.info(
            "Idle: injecting prompt into session {} (channel={}, chat_id={})",
            recent_key,
            channel,
            chat_id,
        )

        # Process through agent
        logger.info("Idle: calling process_callback...")
        response = await process_callback(msg, recent_key)
        logger.info("Idle: process_callback returned: {}", response)

        # Handle the response
        if response:
            has_action_tag = "<ACTION>" in response.content
            clean_content = response.content.replace("<ACTION>", "").strip()
            clean_content = self._clean_output(clean_content)

            # Always journal
            await self.write_journal(clean_content)

            logger.info(
                "Idle: is_normal_chat={}, has_action={}, visible={}, response_empty={}",
                is_normal_chat,
                has_action_tag,
                self._visible,
                not bool(clean_content),
            )

            # Decide what to send to user
            if is_normal_chat:
                # Normal chat - response goes directly to user
                response.content = clean_content
                await self.bus.publish_outbound(response)
                logger.info("Idle: normal chat response sent to {}", recent_key)

            elif has_action_tag:
                # ACTION tag found - send to user with ⚡ + markdown
                response.content = f"⚡\n```\n{clean_content}\n```"
                await self.bus.publish_outbound(response)
                # Set flag for next cycle
                self._next_is_normal = True
                logger.info("Idle: ACTION sent (⚡), will trigger normal chat on next idle")

            elif self._visible:
                # Thought visible to user
                response.content = f"💭\n```\n{clean_content}\n```"
                await self.bus.publish_outbound(response)
                logger.info("Idle: journaled and sent thought to {}", recent_key)

            else:
                # Journal only (invisible)
                logger.info("Idle: journaled thought (invisible) to {}", recent_key)

        else:
            # Agent used message tool (no direct response)
            if is_normal_chat:
                logger.info("Idle: agent sent message via tool (normal chat) to {}", recent_key)
            else:
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
        """Called when user sends a message - cancel pending normal chat."""
        if self._next_is_normal:
            logger.info("User message received, cancelling pending normal chat")
            self._next_is_normal = False
