"""Tool registry for dynamic tool management."""

from pathlib import Path
from typing import Any

from speckbot.agent.tools.base import Tool
from speckbot.security import SecurityGateway, HookResult


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self, hooks_config: dict[str, Any] | None = None, workspace: Path | None = None):
        self._tools: dict[str, Tool] = {}
        self._security = (
            SecurityGateway(config=hooks_config, workspace=workspace) if hooks_config else None
        )
        self._hooks_config = hooks_config
        self._workspace = workspace

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    def set_workspace(self, workspace: Path) -> None:
        """Set workspace path for security gateway persistence."""
        self._workspace = workspace
        if self._security and self._hooks_config:
            self._security = SecurityGateway(config=self._hooks_config, workspace=workspace)

    async def execute(
        self, name: str, params: dict[str, Any], session_key: str | None = None
    ) -> str:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        # Security gateway check - system-level security before execution
        if self._security and self._security.enabled:
            # First check for pending confirmation that was just confirmed
            if session_key:
                confirm_result = self._security.check_confirmation_response(None, session_key)
                if confirm_result.is_allowed and confirm_result.details:
                    # User just confirmed - execute the tool
                    pass  # Continue to execution
                elif confirm_result.is_blocked:
                    # User denied
                    return f"Error: Tool '{name}' denied by user."
                elif confirm_result.is_ask:
                    # Still waiting - return the prompt
                    prompt = confirm_result.reason or f"Confirm: {name}? [yes/no]"
                    return f"Error: {prompt}"

            # Check if tool needs confirmation or should be blocked
            block_result = self._security.scan_tool(name, params, session_key)

            if block_result.is_blocked:
                reason = block_result.reason or "matches blocked pattern"
                return f"Error: Tool '{name}' blocked by security. Reason: {reason}"

            if block_result.is_ask:
                prompt = (
                    block_result.reason
                    or f"Confirm: {name}({self._security.block_detector._format_params(params)})? [yes/no]"
                )
                return f"Error: {prompt}"

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
