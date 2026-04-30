"""Tool registry for dynamic tool management."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from speckbot.tools.base import Tool

if TYPE_CHECKING:
    from speckbot.agent.security import SecurityService


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(
        self,
        hooks_config: dict[str, Any] | None = None,
        workspace: Path | None = None,
        security: Optional["SecurityService"] = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._security = security  # Use shared security service
        self._hooks_config = hooks_config

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(
        self, name: str, params: dict[str, Any], session_key: str | None = None
    ) -> str:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        # Security check using shared security service
        if self._security and self._security.enabled:
            # Check if tool should be blocked
            block_result = self._security.scan_tool(name, params, session_key)

            if block_result.is_blocked:
                reason = block_result.reason or "matches blocked pattern"
                return f"Error: Tool '{name}' blocked by security. Reason: {reason}"

        try:
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _HINT

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
