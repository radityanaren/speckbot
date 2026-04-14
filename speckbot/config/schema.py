"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from speckbot.utils.constants import (
    DEFAULT_MAX_TOKENS_AGENT,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_AGENT_TEMPERATURE,
    DEFAULT_MAX_TOOL_ITERATIONS,
)


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ==================== AGENT ====================


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.speckbot/workspace"
    provider: str = "provider_a"  # Must reference a provider name from providers list
    max_output_tokens: int = DEFAULT_MAX_TOKENS_AGENT
    active_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    context_headroom: int = 20  # Headroom percentage for conveyor belt safety buffer
    tool_result_max_chars: int = 10_000  # Max characters for tool result truncation
    temperature: float = DEFAULT_AGENT_TEMPERATURE
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    reasoning_effort: str | None = None
    # Path to .env file for secrets (defaults to config.json directory)
    env_file_path: str | None = Field(
        default=None,
        description="Path to .env file containing secrets. Defaults to config.json directory if not set.",
    )


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


# ==================== SERVICES ====================


class ServicesConfig(Base):
    """Services configuration - unified config for all services."""

    # Heartbeat service
    heartbeat_enabled: bool = True
    heartbeat_interval_seconds: int = 30 * 60
    # Monologue service
    monologue_enabled: bool = False
    monologue_idle_seconds: int = 300
    monologue_prompt: str = "Hey, been a while — what are you working on?"
    monologue_visible: bool = True
    # Cron service
    cron_enabled: bool = True
    # Dream (memory cleanup + auto-restart)
    dream_enabled: bool = False
    dream_max_memory_lines: int = 200
    dream_deduplicate: bool = True
    dream_convert_dates: bool = True
    dream_sleep_interval_hours: int = 24


# ==================== CHANNELS ====================


class ChannelsConfig(Base):
    """Configuration for chat channels."""

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True
    send_tool_hints: bool = False


# ==================== PROVIDERS ====================


class CustomProvider(Base):
    """One custom provider entry - user defines name, API details, and default model."""

    name: str = ""  # User sets: "provider_a", "nvidia", "work-gpt", etc.
    type: str = (
        "custom"  # Provider type: "custom" (OpenAI-compatible), "litellm", or custom class name
    )
    api_key: str = ""
    api_base: str | None = None
    model: str = ""  # Default model for this provider
    extra_headers: dict[str, str] | None = None


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


# ==================== SECURITY ====================


class SecurityConfig(Base):
    """Security detector configuration (BLOCK patterns and ASK confirmations)."""

    enabled: bool = False
    blocked_patterns: list[str] = Field(
        default_factory=list,
        description="List of regex patterns to block. Add your own patterns as needed.",
    )
    # Tools that require user confirmation before execution
    ask_tools: list[str] = Field(
        default_factory=lambda: [
            "edit_file",
            "write_file",
            "exec",
            "mcp_playwright_browser_fill_form",
        ]
    )
    audit_log: str | None = None


# ==================== TOOLS ====================


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"
    api_key: str = ""
    base_url: str = ""
    max_results: int = 5


class TranscriptionConfig(Base):
    """
    Static transcription configuration - separate from providers list.

    This is a static config (not scalable like custom providers).
    Uses LiteLLM to support multiple transcription backends:
    openai, azure, deepgram, groq, fireworks_ai, mistral, ovhcloud, vertex_ai, gemini
    """

    # API Configuration - can be different from main providers
    api_key: str = ""
    api_base: str | None = None
    # Model - auto-routes to correct provider based on model string
    # Examples: "whisper-1", "groq/whisper-large-v3", "deepgram/nova-2"
    model: str = "whisper-1"
    extra_headers: dict[str, str] | None = None


class MCPServerConfig(Base):
    """MCP server connection configuration."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    tool_timeout: int = 30
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])


def _default_mcp_servers() -> dict[str, MCPServerConfig]:
    """Default MCP servers."""
    return {
        "playwright": MCPServerConfig(
            command="npx",
            args=["@playwright/mcp@latest"],
            enabled_tools=["*"],
        )
    }


class ToolsConfig(Base):
    """Tools configuration."""

    # Web tools
    web_proxy: str | None = None
    web_search_provider: str = "brave"
    web_search_api_key: str = ""
    web_search_base_url: str = ""
    web_search_max_results: int = 5
    # Bash exec tool
    exec_timeout: int = 60
    exec_path_append: str = ""
    exec_bash_path: str | None = (
        None  # Custom bash path (e.g., "C:\Program Files\Git\bin\bash.exe")
    )
    # MCP
    restrict_to_workspace: bool = False
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=_default_mcp_servers)

    @property
    def web(self) -> "WebToolsConfig":
        """Return WebToolsConfig for backward compatibility."""
        return WebToolsConfig(
            proxy=self.web_proxy,
            search=WebSearchConfig(
                provider=self.web_search_provider,
                api_key=self.web_search_api_key,
                base_url=self.web_search_base_url,
                max_results=self.web_search_max_results,
            ),
        )

    @property
    def exec(self) -> "ExecToolConfig":
        """Return ExecToolConfig for backward compatibility."""
        return ExecToolConfig(
            timeout=self.exec_timeout,
            path_append=self.exec_path_append,
            bash_path=self.exec_bash_path,
        )

    @property
    def transcription(self) -> "TranscriptionConfig":
        """Return TranscriptionConfig for backward compatibility."""
        # This property is deprecated - use root Config.transcription instead
        return TranscriptionConfig()


# ==================== ROOT CONFIG ====================


class Config(BaseSettings):
    """Root configuration for speckbot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: list[CustomProvider] = Field(
        default_factory=lambda: [CustomProvider(name="provider_a")]
    )
    # Static transcription config - separate from providers list
    # Uses LiteLLM to support multiple backends (openai, azure, deepgram, groq, etc.)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def get_provider(self) -> "CustomProvider | None":
        """Get provider by name from agents.defaults.provider."""
        provider_name = self.agents.defaults.provider
        for p in self.providers:
            if p.name == provider_name:
                return p
        return None

    def get_provider_name(self) -> str | None:
        """Get provider name from agents.defaults.provider."""
        return self.agents.defaults.provider

    def get_api_key(self) -> str | None:
        """Get API key from the configured provider."""
        p = self.get_provider()
        return p.api_key if p else None

    def get_api_base(self) -> str | None:
        """Get API base URL from the configured provider."""
        p = self.get_provider()
        return p.api_base if p else None

    def get_model(self) -> str | None:
        """Get model from the configured provider."""
        p = self.get_provider()
        return p.model if p else None

    def get_extra_headers(self) -> dict[str, str] | None:
        """Get extra headers from the configured provider."""
        p = self.get_provider()
        return p.extra_headers if p else None

    model_config = ConfigDict(env_prefix="SPECKBOT_", env_nested_delimiter="__")


# ==================== BACKWARD COMPATIBILITY ALIASES ====================
# These classes were flattened into ToolsConfig but kept as aliases for imports
class ExecToolConfig(Base):
    """Shell exec tool configuration (deprecated, use ToolsConfig.exec_* fields)."""

    timeout: int = 60
    path_append: str = ""
    bash_path: str | None = None


class WebToolsConfig(Base):
    """Web tools configuration (deprecated, use ToolsConfig.web_* fields)."""

    proxy: str | None = None
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class MonologueConfig(Base):
    """Monologue system configuration (deprecated, use AgentsConfig monologue_* fields)."""

    enabled: bool = False
    idle_seconds: int = 300
    prompt: str = "Hey, been a while — what are you working on?"
    visible: bool = True


class HeartbeatConfig(Base):
    """Heartbeat service configuration (deprecated, use AgentsConfig heartbeat_* fields)."""

    enabled: bool = True
    interval_seconds: int = 30 * 60
