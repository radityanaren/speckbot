"""Ask detector - requires user confirmation before tool execution."""

import json
from pathlib import Path
from typing import Any

from loguru import logger

from speckbot.security.detectors.base import DetectorBase, SecurityResult, HookResult


class PendingConfirmation:
    """Represents a pending user confirmation request."""

    def __init__(
        self,
        tool_name: str,
        params: dict[str, Any],
        context: str = "unknown",
        session_key: str | None = None,
        timestamp: str | None = None,
    ):
        self.tool_name = tool_name
        self.params = params
        self.context = context
        self.session_key = session_key
        from datetime import datetime

        self.timestamp = timestamp or datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "params": self.params,
            "context": self.context,
            "session_key": self.session_key,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingConfirmation":
        return cls(
            tool_name=data["tool_name"],
            params=data["params"],
            context=data.get("context", "unknown"),
            session_key=data.get("session_key"),
            timestamp=data.get("timestamp"),
        )


class AskDetector(DetectorBase):
    """Detector that requires user confirmation before tool execution.

    When a tool is in the ask_tools list:
    - Returns ASK result
    - Stores pending confirmation
    - On "yes" response, executes the tool
    - On "no" response, blocks the tool
    - Without response, stays pending indefinitely (survives session restart)
    """

    name = "ask"

    def __init__(
        self,
        ask_tools: list[str] | None = None,
        pending_confirmations: dict[str, PendingConfirmation] | None = None,
    ):
        """Initialize with tools that require confirmation.

        Args:
            ask_tools: List of tool names that require user confirmation
            pending_confirmations: Dict of pending confirmations (session_key -> PendingConfirmation)
        """
        self.ask_tools = ask_tools or []
        self._pending: dict[str, PendingConfirmation] = pending_confirmations or {}
        # Track confirmed tools per session to avoid re-asking after confirmation
        # Key: session_key, Value: set of tool names that were confirmed in this session
        self._confirmed_tools: dict[str, set[str]] = {}

    def detect(self, text: str | None = None, context: str = "unknown", **kwargs) -> SecurityResult:
        """Check if tool requires confirmation or process response.

        Args:
            text: User response text (e.g., "yes", "no")
            context: Context (should include tool context)
            **kwargs: Must include tool_name, session_key

        Returns:
            SecurityResult with ASK if tool needs confirmation,
            ALLOW if user confirmed, BLOCK if user denied
        """
        tool_name = kwargs.get("tool_name")
        session_key = kwargs.get("session_key")

        # If tool_name provided, check if it needs confirmation
        if tool_name:
            if tool_name in self.ask_tools:
                # Store pending confirmation
                params = kwargs.get("params", {})
                pending = PendingConfirmation(
                    tool_name=tool_name,
                    params=params,
                    context=context,
                    session_key=session_key,
                )
                self._pending[session_key] = pending

                # Format params for display
                params_str = self._format_params(params)
                prompt = f"Confirm: {tool_name}({params_str})? [yes/no]"

                logger.info(f"[Security] ASK: {tool_name} requires confirmation")
                return SecurityResult(
                    HookResult.ASK,
                    reason=prompt,
                    details={"session_key": session_key, "tool": tool_name, "params": params},
                )
            return SecurityResult(HookResult.ALLOW)

        # If no tool_name but text provided, check for yes/no response
        if text and session_key:
            pending = self._pending.get(session_key)
            if pending:
                response = text.strip().lower()
                if response in ("yes", "y", "confirm", "ok", "sure", "go", "run"):
                    logger.info(f"[Security] CONFIRMED: {pending.tool_name}")
                    # Return ALLOW with pending info to execute
                    result = SecurityResult(
                        HookResult.ALLOW,
                        reason=f"confirmed: {pending.tool_name}",
                        details=pending.to_dict(),
                    )
                    # Remove from pending after confirmation
                    del self._pending[session_key]
                    return result
                elif response in ("no", "n", "cancel", "deny", "stop"):
                    logger.info(f"[Security] DENIED: {pending.tool_name}")
                    tool = pending.tool_name
                    params = pending.params
                    # Remove from pending
                    del self._pending[session_key]
                    return SecurityResult(
                        HookResult.BLOCK,
                        reason=f"denied: {tool}",
                        details={"tool": tool, "params": params},
                    )
                # Not a yes/no response - keep pending
                return SecurityResult(
                    HookResult.ASK,
                    reason=f"Waiting for confirmation: {pending.tool_name}({self._format_params(pending.params)})? [yes/no]",
                    details={"session_key": session_key, "tool": pending.tool_name},
                )

        return SecurityResult(HookResult.ALLOW)

    def check_tool(self, tool_name: str, params: dict[str, Any] | None = None) -> SecurityResult:
        """Check if a specific tool requires confirmation.

        Args:
            tool_name: Name of the tool
            params: Tool parameters

        Returns:
            SecurityResult with ASK if tool needs confirmation, ALLOW otherwise
        """
        if tool_name in self.ask_tools:
            params_str = self._format_params(params or {})
            prompt = f"Confirm: {tool_name}({params_str})? [yes/no]"
            return SecurityResult(
                HookResult.ASK, reason=prompt, details={"tool": tool_name, "params": params or {}}
            )
        return SecurityResult(HookResult.ALLOW)

    def get_pending(self, session_key: str) -> PendingConfirmation | None:
        """Get pending confirmation for a session."""
        return self._pending.get(session_key)

    def has_pending(self, session_key: str) -> bool:
        """Check if there's a pending confirmation for a session."""
        return session_key in self._pending

    def clear_pending(self, session_key: str) -> bool:
        """Clear pending confirmation for a session. Returns True if cleared."""
        if session_key in self._pending:
            del self._pending[session_key]
            return True
        return False

    def mark_confirmed(self, session_key: str, tool_name: str) -> None:
        """Mark a tool as confirmed for a session to prevent re-asking."""
        if session_key not in self._confirmed_tools:
            self._confirmed_tools[session_key] = set()
        self._confirmed_tools[session_key].add(tool_name)
        logger.info(f"[Security] Marked {tool_name} as confirmed for session {session_key}")

    def was_confirmed(self, session_key: str, tool_name: str) -> bool:
        """Check if a tool was confirmed in this session."""
        return (
            session_key in self._confirmed_tools and tool_name in self._confirmed_tools[session_key]
        )

    def clear_confirmed(self, session_key: str) -> None:
        """Clear confirmed tools for a session."""
        if session_key in self._confirmed_tools:
            del self._confirmed_tools[session_key]

    def load_pending_from_file(self, workspace: Path) -> None:
        """Load pending confirmations from file (survives session restart)."""
        confirmations_file = workspace / ".speckbot" / "pending_confirmations.json"
        if confirmations_file.exists():
            try:
                data = json.loads(confirmations_file.read_text(encoding="utf-8"))
                for session_key, conf_data in data.items():
                    self._pending[session_key] = PendingConfirmation.from_dict(conf_data)
                logger.info(f"[Security] Loaded {len(self._pending)} pending confirmations")
            except Exception as e:
                logger.warning(f"[Security] Failed to load pending confirmations: {e}")

    def save_pending_to_file(self, workspace: Path) -> None:
        """Save pending confirmations to file (survives session restart)."""
        confirmations_file = workspace / ".speckbot" / "pending_confirmations.json"
        try:
            confirmations_file.parent.mkdir(parents=True, exist_ok=True)
            data = {session_key: conf.to_dict() for session_key, conf in self._pending.items()}
            confirmations_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Security] Failed to save pending confirmations: {e}")
