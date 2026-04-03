"""Block detector - checks text/commands/params against patterns."""

import re
from typing import Any

from loguru import logger

from speckbot.security.detectors.base import DetectorBase, SecurityResult, HookResult


class BlockDetector(DetectorBase):
    """Detector that blocks content matching configured patterns.

    Patterns can match:
    - User messages (credentials, sensitive content)
    - Shell commands (dangerous commands like rm -rf /)
    - File paths (sensitive system files)
    - Tool parameters (any text that matches patterns)
    """

    name = "block"

    def __init__(self, patterns: list[str] | None = None):
        """Initialize with patterns from config.

        Args:
            patterns: List of regex patterns to block. Empty list = no blocking.
        """
        self.patterns = patterns or []

    def detect(self, text: str | None = None, context: str = "unknown", **kwargs) -> SecurityResult:
        """Check text against block patterns.

        Args:
            text: Content to check (user message, command, etc.)
            context: Context description

        Returns:
            SecurityResult with BLOCK if match found, ALLOW otherwise
        """
        if not text:
            return SecurityResult(HookResult.ALLOW)

        # Check against all patterns
        matched_patterns = self._check_patterns(text)
        if matched_patterns:
            reason = f"blocked pattern: {matched_patterns[0]}"
            logger.warning(f"[Security] BLOCK in {context}: {matched_patterns[0]}")
            return SecurityResult(HookResult.BLOCK, reason=reason, details=matched_patterns)

        return SecurityResult(HookResult.ALLOW)

    def _check_patterns(self, text: str) -> list[str]:
        """Check text against all patterns, return list of matches."""
        matched = []
        for pattern in self.patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    matched.append(pattern)
            except re.error as e:
                logger.warning(f"Invalid regex pattern in block detector: {pattern} - {e}")
        return matched

    def check_params(self, params: dict[str, Any], context: str = "unknown") -> SecurityResult:
        """Check all parameter values against block patterns.

        Args:
            params: Tool parameters to check
            context: Context description

        Returns:
            SecurityResult with BLOCK if any param value matches
        """
        if not params:
            return SecurityResult(HookResult.ALLOW)

        for key, value in params.items():
            if isinstance(value, str):
                result = self.detect(text=value, context=f"{context}.{key}")
                if result.is_blocked:
                    return result
            elif isinstance(value, dict):
                # Recursively check nested dicts
                result = self.check_params(value, context=f"{context}.{key}")
                if result.is_blocked:
                    return result
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        result = self.detect(text=item, context=f"{context}.{key}")
                        if result.is_blocked:
                            return result

        return SecurityResult(HookResult.ALLOW)
