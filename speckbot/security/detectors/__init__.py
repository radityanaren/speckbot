"""Security Gateway - orchestrates all security detectors."""

from pathlib import Path
from typing import Any

from loguru import logger

from speckbot.security.detectors.base import DetectorBase, SecurityResult, HookResult
from speckbot.security.detectors.block import BlockDetector


class SecurityGateway:
    """Central security gateway that wraps the AI model.

    Provides BLOCK security:
    - BLOCK: Content/commands that are blocked (credentials, dangerous commands, etc.)

    Usage:
        gateway = SecurityGateway(config)

        # Check user input before AI sees it
        result = gateway.scan_input("user message with sk-...")

        # Check AI output before user sees it
        result = gateway.scan_output("AI response with credentials...")

        # Check tool before execution
        result = gateway.scan_tool("edit_file", {"path": "~/.ssh/id_rsa"})
    """

    def __init__(self, config: dict[str, Any] | None = None, workspace: Path | None = None):
        """Initialize security gateway with configuration.

        Args:
            config: Security configuration dict
            workspace: Path to workspace
        """
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self.workspace = workspace

        # Initialize BLOCK detector with blocked_patterns from config
        blocked_patterns = self.config.get("blocked_patterns", [])
        self.block_detector = BlockDetector(patterns=blocked_patterns)

        logger.info(f"[SecurityGateway] Initialized (enabled={self.enabled})")

    def scan_input(self, text: str) -> SecurityResult:
        """Scan user input before it reaches the AI.

        Args:
            text: User message content

        Returns:
            SecurityResult - BLOCK if dangerous content, ALLOW otherwise
        """
        if not self.enabled:
            return SecurityResult(HookResult.ALLOW)

        return self.block_detector.detect(text=text, context="user_input")

    def scan_output(self, text: str) -> SecurityResult:
        """Scan AI output before it reaches the user.

        Args:
            text: AI response content

        Returns:
            SecurityResult - BLOCK if dangerous content, ALLOW otherwise
        """
        if not self.enabled:
            return SecurityResult(HookResult.ALLOW)

        return self.block_detector.detect(text=text, context="ai_output")

    def scan_tool(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> SecurityResult:
        """Scan tool execution request.

        Args:
            tool_name: Name of the tool to execute
            params: Tool parameters
            session_key: Session key (unused, kept for API compat)

        Returns:
            SecurityResult - BLOCK if dangerous, ALLOW otherwise
        """
        if not self.enabled:
            return SecurityResult(HookResult.ALLOW)

        params = params or {}

        # Check tool parameters against BLOCK patterns
        block_result = self.block_detector.check_params(params, context=f"tool.{tool_name}")
        if block_result.is_blocked:
            return block_result

        # For bash/exec tools, also check the command string
        if tool_name in ("bash", "exec"):
            command = params.get("command", "") or params.get("command_str", "")
            if command:
                block_result = self.block_detector.detect(
                    text=command, context=f"bash_command.{tool_name}"
                )
                if block_result.is_blocked:
                    return block_result

        return SecurityResult(HookResult.ALLOW)

    def scan_tool_output(self, result: str) -> SecurityResult:
        """Scan tool output before it goes back to the AI.

        Filters dangerous content in tool execution results.

        Args:
            result: Tool execution result (output from the tool)

        Returns:
            SecurityResult - BLOCK if dangerous content, ALLOW otherwise
        """
        if not self.enabled:
            return SecurityResult(HookResult.ALLOW)

        return self.block_detector.detect(text=result, context="tool_output")

    def check_confirmation_response(
        self,
        text: str | None,
        session_key: str,
    ) -> SecurityResult:
        """Check user response (no longer used, kept for API compat).

        Returns ALLOW - confirmations are now handled via soft prompts.
        """
        return SecurityResult(HookResult.ALLOW)

    def has_pending_confirmation(self, session_key: str) -> bool:
        """Check if there's a pending confirmation (always False now)."""
        return False

    def get_pending_prompt(self, session_key: str) -> str | None:
        """Get the pending confirmation prompt (always None now)."""
        return None

    def save_state(self) -> None:
        """Save state (no-op now, kept for API compat)."""
        pass


# Convenience function to create gateway
def create_security_gateway(
    config: dict[str, Any] | None = None, workspace: Path | None = None
) -> SecurityGateway:
    """Create and return a SecurityGateway instance."""
    return SecurityGateway(config=config, workspace=workspace)
