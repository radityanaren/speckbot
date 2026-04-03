"""Tool registry for dynamic tool management."""

from typing import Any

from speckbot.agent.tools.base import Tool
from speckbot.agent.hooks import HookEngine, HookResult


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self, hooks_config: dict[str, Any] | None = None):
        self._tools: dict[str, Tool] = {}
        self._hooks = HookEngine(hooks_config) if hooks_config else None

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

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        # Hooks check - system-level security before execution
        if self._hooks and self._hooks.enabled:
            hook_result = self._hooks.check(name, params)
            if hook_result == HookResult.BLOCK:
                return f"Error: Tool '{name}' blocked by security hooks."
            elif hook_result == HookResult.ASK:
                return f"Error: Tool '{name}' requires user confirmation before execution. Please confirm and retry."

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
