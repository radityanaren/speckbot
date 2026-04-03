"""System-level security hooks and content scanning."""

import re
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger


class HookResult(Enum):
    """Result of a hook check."""

    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"
    CONFIRM = "confirm"


class ContentSecurityResult(Enum):
    """Result of content security scan."""

    CLEAN = "clean"
    WARNING = "warning"
    BLOCK = "block"


class HookEngine:
    """
    System-level security hooks.

    Provides enforcement BEFORE tool execution - no AI involvement.
    OS-agnostic pattern matching using regex.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self.blocked_patterns = self.config.get("blocked_patterns", [])
        self.warn_patterns = self.config.get("warn_patterns", [])
        self.confirm_tools = self.config.get("confirm_tools", [])
        self.audit_log = self.config.get("audit_log")

    def check(self, tool_name: str, params: dict[str, Any]) -> HookResult:
        """Check if tool execution should be allowed."""
        if not self.enabled:
            return HookResult.ALLOW

        # Check if tool requires confirmation
        if tool_name in self.confirm_tools:
            return HookResult.CONFIRM

        # For bash/exec tools, check command patterns
        if tool_name in ("bash", "exec"):
            command = params.get("command", "")
            if not command:
                command = params.get("command_str", "")

            # Check blocked patterns
            if self._matches_any_pattern(command, self.blocked_patterns):
                self._audit_log(tool_name, "BLOCKED", command)
                return HookResult.DENY

            # Check warn patterns
            if self._matches_any_pattern(command, self.warn_patterns):
                self._audit_log(tool_name, "WARN", command)
                return HookResult.WARN

        self._audit_log(tool_name, "ALLOW", str(params)[:200])
        return HookResult.ALLOW

    def _matches_any_pattern(self, text: str, patterns: list[str]) -> bool:
        """Check if text matches any pattern (case-insensitive)."""
        if not text or not patterns:
            return False
        for pattern in patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except re.error:
                logger.warning("Invalid regex pattern: {}", pattern)
        return False

    def _audit_log(self, tool: str, action: str, details: str) -> None:
        """Log action to audit file if configured."""
        if not self.audit_log:
            return
        try:
            from datetime import datetime

            log_path = Path(self.audit_log)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().isoformat()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {action} {tool}: {details}\n")
        except Exception as e:
            logger.warning("Failed to write audit log: {}", e)


class ContentSecurity:
    """
    System-level content scanning for prompt injection, credentials, etc.

    Uses regex patterns - no AI involvement. Scans before content reaches the AI.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self.scan_user_input = self.config.get("scan_user_input", True)
        self.scan_tool_output = self.config.get("scan_tool_output", True)
        self.scan_external_content = self.config.get("scan_external_content", True)
        self.blocked_injection_patterns = self.config.get("blocked_injection_patterns", [])
        self.credential_patterns = self.config.get("credential_patterns", [])
        self.audit_log = self.config.get("audit_log")

    def scan(self, text: str, context: str = "unknown") -> ContentSecurityResult:
        """Scan text for security issues."""
        if not self.enabled or not text:
            return ContentSecurityResult.CLEAN

        result = ContentSecurityResult.CLEAN

        # Check prompt injection patterns
        if self._matches_any_pattern(text, self.blocked_injection_patterns):
            self._audit_log("INJECTION", context, text[:200])
            return ContentSecurityResult.BLOCK

        # Check credential patterns
        if self._matches_any_pattern(text, self.credential_patterns):
            self._audit_log("CREDENTIAL", context, text[:200])
            # Block credentials - they belong in config.json, not chat
            return ContentSecurityResult.BLOCK

        return result

    def scan_with_details(
        self, text: str, context: str = "unknown"
    ) -> tuple[ContentSecurityResult, list[str]]:
        """Scan and return result plus matched issues."""
        if not self.enabled or not text:
            return ContentSecurityResult.CLEAN, []

        issues = []

        # Check injection patterns
        for pattern in self.blocked_injection_patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    issues.append(f"injection:{pattern}")
            except re.error:
                pass

        # Check credential patterns
        for pattern in self.credential_patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    issues.append(f"credential:{pattern}")
            except re.error:
                pass

        if "injection:" in str(issues):
            self._audit_log("INJECTION", context, text[:200])
            return ContentSecurityResult.BLOCK, issues
        elif "credential:" in str(issues):
            self._audit_log("CREDENTIAL", context, text[:200])
            return ContentSecurityResult.BLOCK, issues

        return ContentSecurityResult.CLEAN, []

    def _matches_any_pattern(self, text: str, patterns: list[str]) -> bool:
        """Check if text matches any pattern (case-insensitive)."""
        if not text or not patterns:
            return False
        for pattern in patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except re.error:
                logger.warning("Invalid regex pattern: {}", pattern)
        return False

    def _audit_log(self, issue_type: str, context: str, snippet: str) -> None:
        """Log security issue to audit file if configured."""
        if not self.audit_log:
            return
        try:
            from datetime import datetime

            log_path = Path(self.audit_log)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().isoformat()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {issue_type} ({context}): {snippet[:200]}\n")
        except Exception as e:
            logger.warning("Failed to write security audit log: {}", e)


def create_hooks(config: dict[str, Any] | None = None) -> HookEngine:
    """Create HookEngine from config dict."""
    return HookEngine(config)


def create_content_security(config: dict[str, Any] | None = None) -> ContentSecurity:
    """Create ContentSecurity from config dict."""
    return ContentSecurity(config)
