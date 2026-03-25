"""Shared mixins for common functionality across channels and other modules."""

from abc import ABC, abstractmethod
from typing import Any


class GroupPolicyMixin(ABC):
    """Mixin for group channel policy checking.

    Channels that support group policies (like mentions) should inherit from this.
    """

    @property
    @abstractmethod
    def group_policy(self) -> str:
        """Return the group's policy ('open' or 'mention')."""
        ...

    @property
    @abstractmethod
    def bot_user_id(self) -> str | None:
        """Return the bot's user ID for mention detection."""
        ...

    def should_respond_in_group(
        self,
        content: str,
        mentions: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Check if bot should respond in a group based on policy.

        Args:
            content: The message content
            mentions: Optional list of mentioned users (each with 'id' field)

        Returns:
            True if bot should respond, False otherwise.
        """
        # Open policy: always respond
        if self.group_policy == "open":
            return True

        # Mention policy: respond only if bot was mentioned
        if self.group_policy == "mention":
            return self._was_mentioned(content, mentions)

        # Unknown policy: don't respond
        return False

    def _was_mentioned(
        self,
        content: str,
        mentions: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Check if the bot was mentioned in the message.

        Args:
            content: The message content
            mentions: Optional list of mentioned users

        Returns:
            True if bot was mentioned, False otherwise.
        """
        bot_id = self.bot_user_id
        if not bot_id:
            return False

        # Check mentions array
        if mentions:
            for mention in mentions:
                if str(mention.get("id")) == bot_id:
                    return True

        # Check content for mention format <@USER_ID> or <@!USER_ID>
        if f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content:
            return True

        return False


class TypingIndicatorMixin(ABC):
    """Mixin for typing indicator management.

    Provides common structure for managing typing indicators across channels.
    """

    # Subclasses should define this as a dict[str, asyncio.Task]
    _typing_tasks: dict

    @abstractmethod
    async def _send_typing_action(self, target_id: str) -> None:
        """Send a typing/start indicator to the platform.

        Args:
            target_id: The chat/channel ID to send typing indicator to.
        """
        ...

    async def start_typing(self, target_id: str, interval: float = 4.0) -> None:
        """Start sending typing indicators for a target.

        Args:
            target_id: The chat/channel ID
            interval: Seconds between typing indicator sends
        """
        import asyncio

        # Stop any existing typing for this target
        await self.stop_typing(target_id)

        async def typing_loop() -> None:
            try:
                while True:
                    await self._send_typing_action(target_id)
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # Silently stop on error

        task = asyncio.create_task(typing_loop())
        self._typing_tasks[target_id] = task

    async def stop_typing(self, target_id: str) -> None:
        """Stop typing indicators for a target.

        Args:
            target_id: The chat/channel ID
        """
        import asyncio

        task = self._typing_tasks.pop(target_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
