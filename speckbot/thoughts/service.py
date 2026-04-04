"""Thoughts service - continuous inner voice reflection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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

    Every interval (default 1 day), the service:
    1. Asks the LLM to reflect on recent state
    2. LLM decides: JOURNAL / SHARE / SKIP
    3. JOURNAL → append to JOURNAL.md (with auto-trim old entries)
    4. SHARE → evaluate → notify user via channel
    5. SKIP → do nothing
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_seconds: int = 24 * 60 * 60,
        keep_days: int = 7,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_notify = on_notify
        self.interval_seconds = interval_seconds
        self.keep_days = keep_days
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def journal_file(self) -> Path:
        return self.workspace / "JOURNAL.md"

    def _read_journal(self) -> str | None:
        if self.journal_file.exists():
            try:
                return self.journal_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def _write_journal(self, content: str) -> None:
        try:
            self.journal_file.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.error("Failed to write journal: {}", e)

    def _trim_journal(self, content: str) -> str:
        """Trim journal to keep only entries from last N days."""
        lines = content.split("\n")
        if not lines:
            return content

        # Find the cutoff date
        cutoff = datetime.now() - timedelta(days=self.keep_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        # Find entries newer than cutoff
        kept_lines: list[str] = []
        found_recent = False

        for line in lines:
            # Check if this is a date header (## YYYY-MM-DD or ### YYYY-MM-DD)
            stripped = line.strip()
            if stripped.startswith("##") or stripped.startswith("###"):
                # Extract date from the line
                import re

                match = re.search(r"\d{4}-\d{2}-\d{2}", line)
                if match:
                    date_str = match.group(0)
                    try:
                        entry_date = datetime.strptime(date_str, "%Y-%m-%d")
                        if entry_date >= cutoff:
                            found_recent = True
                    except ValueError:
                        pass
            if found_recent:
                kept_lines.append(line)

        # If we removed everything, keep at least the last few lines
        if not kept_lines and lines:
            kept_lines = lines[-20:]

        return "\n".join(kept_lines)

    def _append_journal(self, entry: str) -> None:
        """Append a new entry to the journal with auto-trim."""
        from speckbot.utils.helpers import current_time_str

        current = self._read_journal() or ""

        # Build new entry
        date_str = datetime.now().strftime("%Y-%m-%d")
        timestamp = current_time_str().split(",")[0]  # Just the date part

        new_entry = f"\n## {date_str}\n- [{timestamp}] {entry}\n"

        # Append to existing
        if current:
            # Add separator if needed
            if not current.rstrip().endswith("\n"):
                new_entry = "\n" + new_entry
            updated = current + new_entry
        else:
            # New file
            updated = f"# Journal\n{new_entry}"

        # Trim old entries
        trimmed = self._trim_journal(updated)

        self._write_journal(trimmed)
        logger.info("Thought saved to journal: {}", entry[:50])

    async def _decide(self) -> tuple[str, str]:
        """Ask LLM to decide: JOURNAL / SHARE / SKIP.

        Returns (decision, content).
        """
        from speckbot.utils.helpers import current_time_str

        response = await self.provider.chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": _THOUGHTS_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": f"Current Time: {current_time_str()}\n\nWhat is on your mind? What do you observe or want to share?",
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
                    self._append_journal(content)
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
