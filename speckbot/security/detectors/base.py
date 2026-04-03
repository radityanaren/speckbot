"""Security detector base classes."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class HookResult(Enum):
    """Result of a security check."""

    ALLOW = "allow"  # runs normally
    ASK = "ask"  # requires user approval before execution
    BLOCK = "block"  # system blocks (matches patterns)


class SecurityResult:
    """Result of a security detector check."""

    def __init__(self, result: HookResult, reason: str | None = None, details: Any = None):
        self.result = result
        self.reason = reason
        self.details = details

    @property
    def is_blocked(self) -> bool:
        return self.result == HookResult.BLOCK

    @property
    def is_ask(self) -> bool:
        return self.result == HookResult.ASK

    @property
    def is_allowed(self) -> bool:
        return self.result == HookResult.ALLOW


class DetectorBase(ABC):
    """Base class for security detectors."""

    name: str = "base"

    @abstractmethod
    def detect(self, text: str | None = None, context: str = "unknown", **kwargs) -> SecurityResult:
        """Run detection on text or parameters.

        Args:
            text: Text content to check (e.g., user message, command string)
            context: Context description (e.g., "user_input", "tool_output", "bash_command")
            **kwargs: Additional parameters (tool_name, params, etc.)

        Returns:
            SecurityResult with BLOCK, ASK, or ALLOW
        """
        pass

    def _format_params(self, params: dict[str, Any]) -> str:
        """Format tool parameters for display in confirmation."""
        if not params:
            return ""

        parts = []
        for k, v in params.items():
            if isinstance(v, str) and len(v) > 50:
                parts.append(f"{k}='{v[:47]}...'")
            elif isinstance(v, str):
                parts.append(f"{k}='{v}'")
            else:
                parts.append(f"{k}={v}")

        return ", ".join(parts)
