"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from speckbot.utils.constants import (
    DEFAULT_MODEL,
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
    model: str = DEFAULT_MODEL
    provider: str = "auto"
    max_output_tokens: int = DEFAULT_MAX_TOKENS_AGENT
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    temperature: float = DEFAULT_AGENT_TEMPERATURE
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    reasoning_effort: str | None = None


class AgentsConfig(Base):
    """Agent configuration with heartbeat and monologue."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    # Heartbeat service configuration
    heartbeat_enabled: bool = True
    heartbeat_interval_seconds: int = 30 * 60
    # Monologue system - time-triggered prompt to active session
    monologue_enabled: bool = False
    monologue_idle_seconds: int = 300
    monologue_prompt: str = "Hey, been a while — what are you working on?"
    monologue_visible: bool = True  # True = show in channel, False = journal only


# ==================== DREAM ====================


class DreamConfig(Base):
    """Sleep system - memory cleanup and auto-restart configuration."""

    enabled: bool = False
    max_memory_lines: int = 200
    deduplicate: bool = True
    convert_dates: bool = True
    sleep_interval_hours: int = 24


# ==================== CHANNELS ====================


class ChannelsConfig(Base):
    """Configuration for chat channels."""

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True
    send_tool_hints: bool = False


# ==================== PROVIDERS ====================


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)


# ==================== GATEWAY ====================


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


# ==================== SECURITY ====================


class SecurityConfig(Base):
    """Security detector configuration (BLOCK patterns only)."""

    enabled: bool = False
    patterns: list[str] = Field(
        default_factory=lambda: [
            r"\brm\s+-rf\s+[\/\.]",
            r"\bformat\s+[a-z]:",
            r"\bdel\s+/f\s+/s\s+/q",
            r"\bdd\s+if=",
            r">\s*/dev/",
            r"\bmkfs\.",
            r"\bshutdown\b",
            r"\breboot\b",
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
    """Audio transcription via Groq Whisper."""

    groq_api_key: str = ""


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
    # Shell exec tool
    exec_timeout: int = 60
    exec_path_append: str = ""
    # Transcription
    transcription_groq_api_key: str = ""
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
        )


# ==================== ROOT CONFIG ====================


class Config(BaseSettings):
    """Root configuration for speckbot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    dream: DreamConfig = Field(default_factory=DreamConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name."""
        from speckbot.providers.registry import PROVIDERS

        forced = self.agents.defaults.provider
        if forced != "auto":
            p = getattr(self.providers, forced, None)
            return (p, forced) if p else (None, None)

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        from speckbot.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="SPECKBOT_", env_nested_delimiter="__")


# ==================== BACKWARD COMPATIBILITY ALIASES ====================
# These classes were flattened into ToolsConfig but kept as aliases for imports
class ExecToolConfig(Base):
    """Shell exec tool configuration (deprecated, use ToolsConfig.exec_* fields)."""

    timeout: int = 60
    path_append: str = ""


class WebToolsConfig(Base):
    """Web tools configuration (deprecated, use ToolsConfig.web_* fields)."""

    proxy: str | None = None
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
