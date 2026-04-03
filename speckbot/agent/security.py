"""Security service - shared security gateway for the agent."""

from pathlib import Path
from typing import Any

from speckbot.security import SecurityGateway


class SecurityService:
    """Shared security service for the entire agent.

    This ensures security state (pending confirmations) is shared across
    all components that need security checks (context builder, tool registry).

    Single responsibility: security enforcement.
    """

    def __init__(self, config: dict[str, Any] | None = None, workspace: Path | None = None):
        self._gateway = None
        self._config = config
        self._workspace = workspace
        if config and config.get("enabled"):
            self._gateway = SecurityGateway(config=config, workspace=workspace)

    @property
    def enabled(self) -> bool:
        return self._gateway is not None and self._gateway.enabled

    @property
    def gateway(self) -> SecurityGateway | None:
        return self._gateway

    def scan_input(self, text: str) -> Any:
        """Scan user input before it reaches the AI."""
        if not self._gateway:
            from speckbot.security import SecurityResult, HookResult

            return SecurityResult(HookResult.ALLOW)
        return self._gateway.scan_input(text)

    def scan_output(self, text: str) -> Any:
        """Scan AI output before it reaches the user."""
        if not self._gateway:
            from speckbot.security import SecurityResult, HookResult

            return SecurityResult(HookResult.ALLOW)
        return self._gateway.scan_output(text)

    def scan_tool(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> Any:
        """Scan tool execution request."""
        if not self._gateway:
            from speckbot.security import SecurityResult, HookResult

            return SecurityResult(HookResult.ALLOW)
        return self._gateway.scan_tool(tool_name, params, session_key)

    def check_confirmation_response(
        self,
        text: str | None,
        session_key: str,
    ) -> Any:
        """Check user response to confirmation prompt."""
        if not self._gateway:
            from speckbot.security import SecurityResult, HookResult

            return SecurityResult(HookResult.ALLOW)
        return self._gateway.check_confirmation_response(text, session_key)

    def has_pending_confirmation(self, session_key: str) -> bool:
        """Check if there's a pending confirmation for a session."""
        if not self._gateway:
            return False
        return self._gateway.has_pending_confirmation(session_key)

    def get_pending_prompt(self, session_key: str) -> str | None:
        """Get the pending confirmation prompt for a session."""
        if not self._gateway:
            return None
        return self._gateway.get_pending_prompt(session_key)

    def save_state(self) -> None:
        """Save pending confirmations to file."""
        if self._gateway:
            self._gateway.save_state()
