"""Security Gateway - orchestrates all security detectors."""

from pathlib import Path
from typing import Any

from loguru import logger

from speckbot.security.detectors.base import DetectorBase, SecurityResult, HookResult
from speckbot.security.detectors.block import BlockDetector
from speckbot.security.detectors.ask import AskDetector, PendingConfirmation


class SecurityGateway:
    """Central security gateway that wraps the AI model.

    Provides two-layer security:
    - BLOCK: Content/commands that are blocked (credentials, dangerous commands, etc.)
    - ASK: Tools that require user confirmation before execution

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
            config: Hooks configuration dict from config.json
            workspace: Path to workspace for saving pending confirmations
        """
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self.workspace = workspace

        # Initialize BLOCK detector with patterns from config
        # Support both old format (block.patterns) and new format (patterns directly)
        block_config = self.config.get("block", {})
        patterns = block_config.get("patterns", [])
        if not patterns:
            # New format: patterns at top level
            patterns = self.config.get("patterns", [])
        self.block_detector = BlockDetector(patterns=patterns)

        # Initialize ASK detector with tools from config (optional - for backward compat)
        ask_config = self.config.get("ask", {})
        if isinstance(ask_config, list):
            ask_tools = ask_config
        else:
            ask_tools = ask_config.get("tools", []) if ask_config else []
        self.ask_detector = AskDetector(ask_tools=ask_tools)

        # Load pending confirmations from file if workspace provided
        if workspace:
            self.ask_detector.load_pending_from_file(workspace)

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
            session_key: Session key for pending confirmation tracking

        Returns:
            SecurityResult - ASK if needs confirmation, BLOCK if dangerous, ALLOW otherwise
        """
        if not self.enabled:
            return SecurityResult(HookResult.ALLOW)

        params = params or {}

        # First check if tool was already confirmed in this session
        # If so, skip ASK and execute directly (fixes confirmation loop)
        if session_key and self.ask_detector.was_confirmed(session_key, tool_name):
            logger.info(
                f"[SecurityGateway] Tool '{tool_name}' already confirmed, allowing execution"
            )
            return SecurityResult(HookResult.ALLOW)

        # First check if tool needs confirmation (ASK)
        ask_result = self.ask_detector.check_tool(tool_name, params)
        if ask_result.is_ask:
            # Store pending confirmation
            if session_key:
                self.ask_detector._pending[session_key] = PendingConfirmation(
                    tool_name=tool_name,
                    params=params,
                    context="tool_execution",
                    session_key=session_key,
                )
                # Save to file for persistence
                if self.workspace:
                    self.ask_detector.save_pending_to_file(self.workspace)
            return ask_result

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
        """Check user response to confirmation prompt.

        Args:
            text: User's response (e.g., "yes", "no")
            session_key: Session key to identify pending confirmation

        Returns:
            SecurityResult - ALLOW if confirmed, BLOCK if denied, ASK if still pending
        """
        if not self.enabled:
            return SecurityResult(HookResult.ALLOW)

        # Check if there's a pending confirmation
        pending = self.ask_detector.get_pending(session_key)
        if not pending:
            return SecurityResult(HookResult.ALLOW)

        # Process yes/no response
        response = (text or "").strip().lower() if text else ""

        if response in ("yes", "y", "confirm", "ok", "sure", "go", "run"):
            # User confirmed - execute the tool
            logger.info(f"[SecurityGateway] User confirmed: {pending.tool_name}")

            # Mark the tool as confirmed so it won't re-ask when re-executed
            self.ask_detector.mark_confirmed(session_key, pending.tool_name)

            # Clear pending and return ALLOW with tool info
            self.ask_detector.clear_pending(session_key)
            if self.workspace:
                self.ask_detector.save_pending_to_file(self.workspace)

            return SecurityResult(
                HookResult.ALLOW,
                reason=f"confirmed: {pending.tool_name}",
                details=pending.to_dict(),
            )
        elif response in ("no", "n", "cancel", "deny", "stop"):
            # User denied
            logger.info(f"[SecurityGateway] User denied: {pending.tool_name}")

            self.ask_detector.clear_pending(session_key)
            if self.workspace:
                self.ask_detector.save_pending_to_file(self.workspace)

            return SecurityResult(
                HookResult.BLOCK,
                reason=f"denied: {pending.tool_name}",
                details={"tool": pending.tool_name, "params": pending.params},
            )
        else:
            # Not a yes/no response - keep pending
            params_str = self.ask_detector._format_params(pending.params)
            return SecurityResult(
                HookResult.ASK,
                reason=f"Waiting for confirmation: {pending.tool_name}({params_str})? [yes/no]",
                details={"session_key": session_key, "tool": pending.tool_name},
            )

    def has_pending_confirmation(self, session_key: str) -> bool:
        """Check if there's a pending confirmation for a session."""
        return self.ask_detector.has_pending(session_key)

    def get_pending_prompt(self, session_key: str) -> str | None:
        """Get the pending confirmation prompt for a session."""
        pending = self.ask_detector.get_pending(session_key)
        if pending:
            params_str = self.ask_detector._format_params(pending.params)
            return f"Confirm: {pending.tool_name}({params_str})? [yes/no]"
        return None

    def save_state(self) -> None:
        """Save pending confirmations to file."""
        if self.workspace:
            self.ask_detector.save_pending_to_file(self.workspace)


# Convenience function to create gateway
def create_security_gateway(
    config: dict[str, Any] | None = None, workspace: Path | None = None
) -> SecurityGateway:
    """Create and return a SecurityGateway instance."""
    return SecurityGateway(config=config, workspace=workspace)
