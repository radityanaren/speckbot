"""Thoughts service - continuous inner voice reflection using JSONL sessions."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from speckbot.providers.base import LLMProvider

_THOUGHTS_SYSTEM_PROMPT = """You are SpeckBot's inner voice. At regular intervals, you reflect on recent events, conversations, and your own state.

Decide what to do with your thought:
- If it's something the user would want to know → respond: SHARE: <what to tell them>
- If it's just a quick internal note about your observations → respond: JOURNAL: <your note>
- If nothing noteworthy → respond: SKIP

Be concise but insightful. If sharing, make sure it's actually interesting/useful to the user."""


class ThoughtsService:
    """
    Continuous thoughts service that runs in the background.

    Uses JSONL sessions like regular conversations:
    1. Reads recent session context (what was agent working on?)
    2. Asks LLM to reflect on recent state
    3. LLM decides: JOURNAL / SHARE / SKIP
    4. JOURNAL → write to sessions/thoughts.jsonl (like chatting with itself)
    5. SHARE → evaluate → notify user via channel
    6. SKIP → do nothing
    7. Auto-trim to keep last N messages
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_seconds: int = 24 * 60 * 60,
        max_messages: int = 10,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_notify = on_notify
        self.interval_seconds = interval_seconds
        self.max_messages = max_messages
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def sessions_dir(self) -> Path:
        return self.workspace / "sessions"

    @property
    def session_file(self) -> Path:
        return self.sessions_dir / "thoughts.jsonl"

    def _ensure_sessions_dir(self) -> None:
        """Ensure sessions directory exists."""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _read_session(self) -> list[dict[str, Any]]:
        """Read thoughts session messages."""
        if not self.session_file.exists():
            return []
        try:
            messages = []
            with open(self.session_file, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        messages.append(json.loads(line))
            return messages
        except Exception as e:
            logger.warning("Failed to read thoughts session: {}", e)
            return []

    def _write_message(self, role: str, content: str) -> None:
        """Write a message to the thoughts session."""
        self._ensure_sessions_dir()
        entry = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            with open(self.session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to write thoughts message: {}", e)

    def _trim_session(self) -> None:
        """Trim session to keep only last N messages (not including system prompt)."""
        messages = self._read_session()
        if len(messages) <= self.max_messages:
            return

        # Keep last N messages
        trimmed = messages[-self.max_messages :]

        # Rewrite the file
        try:
            with open(self.session_file, "w", encoding="utf-8") as f:
                for msg in trimmed:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            logger.info("Trimmed thoughts session to {} messages", self.max_messages)
        except Exception as e:
            logger.error("Failed to trim thoughts session: {}", e)

    def _get_recent_context(self, limit: int = 5) -> str:
        """Get context from recent regular sessions (what was agent working on?)."""
        if not self.sessions_dir.exists():
            return ""

        # Get all session files except thoughts
        session_files = []
        for f in self.sessions_dir.glob("*.jsonl"):
            if f.name != "thoughts.jsonl":
                session_files.append(f)

        if not session_files:
            return ""

        # Sort by modification time, most recent first
        session_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        # Read last N sessions
        context_parts = []
        for session_file in session_files[:3]:  # Last 3 sessions
            try:
                messages = []
                with open(session_file, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            messages.append(json.loads(line))

                # Get last few messages from this session
                recent = messages[-limit:] if len(messages) > limit else messages
                for msg in recent:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if content and role in ("user", "assistant"):
                        # Truncate long content
                        if len(content) > 200:
                            content = content[:200] + "..."
                        context_parts.append(f"[{role}]: {content}")
            except Exception:
                continue

        return "\n".join(context_parts[-10:])  # Last 10 context lines total

    async def _decide(self) -> tuple[str, str]:
        """Ask LLM to decide: JOURNAL / SHARE / SKIP.

        Returns (decision, content).
        """
        from speckbot.utils.helpers import current_time_str

        # Get recent context from other sessions
        context = self._get_recent_context()

        user_message = f"Current Time: {current_time_str()}"

        if context:
            user_message += f"\n\nRecent conversations:\n{context}"

        user_message += "\n\nWhat is on your mind? What do you observe or want to share?"

        response = await self.provider.chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": _THOUGHTS_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": user_message,
                },
            ],
            model=self.model,
        )

        content = (response.content or "").strip()

        # Parse decision
        if content.upper().startswith("SHARE:"):
            return "SHARE", content[6:].strip()
        elif content.upper().startswith("JOURNAL:"):
            return "JOURNAL", content[7:].strip()
        elif content.upper().startswith("SKIP"):
            return "SKIP", ""
        else:
            # Default to journal if unclear
            return "JOURNAL", content

    async def start(self) -> None:
        """Start the thoughts service."""
        if not self.enabled:
            logger.info("Thoughts service disabled")
            return
        if self._running:
            logger.warning("Thoughts service already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Thoughts service started (every {}s)", self.interval_seconds)

    async def _run_loop(self) -> None:
        """Main thoughts loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Thoughts service error: {}", e)

    def stop(self) -> None:
        """Stop the thoughts service."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Thoughts service stopped")

    async def _tick(self) -> None:
        """Execute a single thoughts tick."""
        from speckbot.utils.evaluator import evaluate_response

        logger.info("Thoughts service: thinking...")

        try:
            decision, content = await self._decide()

            if decision == "JOURNAL":
                if content:
                    # Write to session like a conversation with itself
                    self._write_message("user", "Reflect: " + content)
                    # Trim old messages
                    self._trim_session()
                logger.info("Thought: journaled")

            elif decision == "SHARE":
                if content:
                    # Evaluate if it's worth notifying
                    should_notify = await evaluate_response(
                        content,
                        "thoughts",
                        self.provider,
                        self.model,
                    )
                    if should_notify and self.on_notify:
                        logger.info("Thought: sharing with user")
                        await self.on_notify(content)
                    else:
                        logger.info("Thought: share silenced by evaluator")
                else:
                    logger.info("Thought: skip (empty)")

            else:  # SKIP
                logger.info("Thought: skipped")

        except Exception:
            logger.exception("Thoughts service tick failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a thoughts tick."""
        await self._tick()
        return None
