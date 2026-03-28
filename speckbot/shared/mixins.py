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

    @abstractmethod
    def _was_mentioned(
        self,
        content: str,
        mentions: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Check if the bot was mentioned in the message.

        Must be implemented by each channel since mention formats differ.

        Args:
            content: The message content
            mentions: Optional list of mentioned users

        Returns:
            True if bot was mentioned, False otherwise.
        """
        ...
