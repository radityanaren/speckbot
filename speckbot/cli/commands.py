"""CLI commands for speckbot."""

import asyncio
import os
import select
import signal
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from speckbot import __logo__, __version__
from speckbot.config.paths import get_workspace_path
from speckbot.config.schema import Config, WebSearchConfig, BashToolConfig
from speckbot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="speckbot",
    help=f"{__logo__} SpeckBot - Personal AI Assistant",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
        # Try termios first (Unix/Linux)
        try:
            import termios

            termios.tcflush(fd, termios.TCIFLUSH)
        except (ImportError, OSError):
            # Fallback: select + read for portability
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready or not os.read(fd, 4096):
                    break
    except Exception:
        pass


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from speckbot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_agent_response_ansi(response: str, render_markdown: bool) -> str:
    """Render agent response to ANSI, safe for prompt_toolkit."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        ansi_console.print()
        ansi_console.print(f"[cyan]{__logo__} SpeckBot[/cyan]")
        ansi_console.print(body)
        ansi_console.print()
    return capture.get()


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} SpeckBot[/cyan]")
    console.print(body)
    console.print()


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""

    def _write() -> None:
        ansi_console = Console(
            force_terminal=True,
            color_system=console.color_system or "standard",
            width=console.width,
        )
        with ansi_console.capture() as capture:
            ansi_console.print(f"  [dim]↳ {text}[/dim]")
        print_formatted_text(ANSI(capture.get()), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(response: str, render_markdown: bool) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""

    def _write() -> None:
        print_formatted_text(ANSI(_render_agent_response_ansi(response, render_markdown)), end="")

    await run_in_terminal(_write)


class _ThinkingSpinner:
    """Spinner wrapper with pause support for clean progress output."""

    def __init__(self, enabled: bool):
        self._spinner = (
            console.status("[dim]SpeckBot is thinking...[/dim]", spinner="dots")
            if enabled
            else None
        )
        self._active = False

    def __enter__(self):
        if self._spinner:
            self._spinner.start()
        self._active = True
        return self

    def __exit__(self, *exc):
        self._active = False
        if self._spinner:
            self._spinner.stop()
        return False

    @contextmanager
    def pause(self):
        """Temporarily stop spinner while printing progress."""
        if self._spinner and self._active:
            self._spinner.stop()
        try:
            yield
        finally:
            if self._spinner and self._active:
                self._spinner.start()


def _print_cli_progress_line(text: str, thinking: _ThinkingSpinner | None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


async def _print_interactive_progress_line(text: str, thinking: _ThinkingSpinner | None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} SpeckBot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """SpeckBot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Initialize SpeckBot configuration and workspace."""
    from speckbot.config.loader import get_config_path, load_config, save_config, set_config_path

    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Create or update config
    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print(
            "  [bold]N[/bold] = refresh config, keeping existing values and adding new fields"
        )
        if typer.confirm("Overwrite?"):
            config = _apply_workspace_override(Config())
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = _apply_workspace_override(load_config(config_path))
            save_config(config, config_path)
            console.print(
                f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)"
            )
    else:
        config = _apply_workspace_override(Config())
        save_config(config, config_path)
        console.print(f"[green]✓[/green] Created config at {config_path}")

    _onboard_plugins(config_path)

    # Create .env file next to config for secrets
    env_path = config_path.parent / ".env"
    if not env_path.exists():
        env_content = """# Add your secrets below:
"""
        env_path.write_text(env_content, encoding="utf-8")
        console.print(f"[green]✓[/green] Created .env template at {env_path}")
    else:
        console.print(f"[dim].env already exists at {env_path}[/dim]")

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    gateway_cmd = "speckbot gateway"
    if config:
        gateway_cmd = gateway_cmd + " --config " + str(config_path)

    console.print(f"\n{__logo__} SpeckBot is ready!")
    console.print("\nNext steps:")
    console.print(f"  1. Add your secrets to [cyan]{env_path}[/cyan]")
    console.print("     Reference them in config.json using ${VAR_NAME}")
    console.print("     Example: 'api_key': '${OPENAI_API_KEY}'")
    console.print("  2. Edit config.json to add Telegram/Discord/Custom channels")
    console.print("  3. Run: [cyan]speckbot gateway[/cyan]")


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from speckbot.bus.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from speckbot.providers.base import GenerationSettings

    # Get provider by name from config
    provider_name = config.get_provider_name()
    p = config.get_provider()

    if not p:
        console.print(f"[red]Error: Provider '{provider_name}' not found in config.[/red]")
        console.print("Add a provider to the 'providers' list in config.json")
        raise typer.Exit(1)

    # Get model from provider config
    model = config.get_model()
    if not model:
        console.print(f"[red]Error: No model configured for provider '{provider_name}'.[/red]")
        console.print("Set 'model' in your provider config")
        raise typer.Exit(1)

    # Get provider type from config (default: "custom")
    provider_type = p.type if p else "custom"

    # Create provider based on type
    if provider_type == "custom":
        # Direct OpenAI-compatible endpoint
        from speckbot.providers.custom_provider import CustomProvider

        provider = CustomProvider(
            api_key=p.api_key or "no-key",
            api_base=p.api_base or "http://localhost:8000/v1",
            default_model=model,
            extra_headers=p.extra_headers,
        )
    elif provider_type == "litellm":
        # LiteLLM provider - handles many backends
        from speckbot.providers.litellm_provider import LiteLLMProvider

        if not p.api_key:
            console.print(
                f"[red]Error: No API key configured for provider '{provider_name}'.[/red]"
            )
            raise typer.Exit(1)

        provider = LiteLLMProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
            extra_headers=p.extra_headers,
            provider_name=provider_name,
        )
    else:
        # Custom provider class - try to import from speckbot.providers
        try:
            from speckbot.providers import CustomProvider as BaseCustomProvider

            # Try to import the custom provider class
            provider_class = getattr(
                __import__(f"speckbot.providers.{provider_type}", fromlist=[provider_type]),
                provider_type,
            )

            provider = provider_class(
                api_key=p.api_key or "no-key",
                api_base=p.api_base,
                default_model=model,
                extra_headers=p.extra_headers,
            )
        except (ImportError, AttributeError) as e:
            console.print(f"[red]Error: Custom provider type '{provider_type}' not found.[/red]")
            console.print(
                f"Available types: 'custom', 'litellm', or Python class in speckbot/providers/"
            )
            raise typer.Exit(1)

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_output_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from speckbot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from speckbot.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the SpeckBot gateway."""
    from speckbot.agent.loop import AgentLoop
    from speckbot.bus.queue import MessageBus
    from speckbot.bus.channels.manager import ChannelManager
    from speckbot.config.paths import get_cron_dir
    from speckbot.services.cron.service import CronService
    from speckbot.services.cron.types import CronJob
    from speckbot.services.heartbeat.service import HeartbeatService
    from speckbot.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)
    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} Starting SpeckBot gateway version {__version__} on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.get_model(),
        max_iterations=config.agents.defaults.max_tool_iterations,
        active_window_tokens=config.agents.defaults.active_window_tokens,
        tool_truncation_percent=config.agents.defaults.tool_truncation_percent,
        tool_result_max_chars=config.agents.defaults.tool_result_max_chars,
        summary_config={
            "enabled": config.agents.defaults.summary_enabled,
            "result_max_chars": config.agents.defaults.summary_result_max_chars,
            "assistant_max_chars": config.agents.defaults.summary_assistant_max_chars,
        },
        web_search_config=WebSearchConfig(
            provider=config.tools.web_search_provider,
            api_key=config.tools.web_search_api_key,
            base_url=config.tools.web_search_base_url,
            max_results=config.tools.web_search_max_results,
        ),
        web_proxy=config.tools.web_proxy,
        exec_config=BashToolConfig(
            timeout=config.tools.exec_timeout,
            path_append=config.tools.exec_path_append,
            bash_path=config.tools.exec_bash_path,
        ),
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        hooks_config=config.security.model_dump() if config.security else None,
        monologue_config={
            "enabled": config.services.monologue_enabled,
            "idle_seconds": config.services.monologue_idle_seconds,
            "prompt": config.services.monologue_prompt,
            "visible": config.services.monologue_visible,
        },
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from speckbot.tools.cron import CronTool
        from speckbot.tools.message import MessageTool
        from speckbot.utils.evaluator import evaluate_response

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            should_notify = await evaluate_response(
                response,
                job.payload.message,
                provider,
                agent.model,
            )
            if should_notify:
                from speckbot.bus.events import OutboundMessage

                await bus.publish_outbound(
                    OutboundMessage(
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to,
                        content=response,
                    )
                )
        return response

    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from speckbot.bus.events import OutboundMessage

        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response)
        )

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_seconds=config.services.heartbeat_interval_seconds,
        enabled=config.services.heartbeat_enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(
        f"[green]✓[/green] Heartbeat: every {config.services.heartbeat_interval_seconds}s"
    )

    if config.services.monologue_enabled:
        console.print(
            f"[green]✓[/green] Idle prompt: after {config.services.monologue_idle_seconds}s idle"
        )

    async def run():
        # UnifiedTimer handles Dream (startup + sleep timer), heartbeat, and monologue
        from speckbot.services.timer import UnifiedTimer

        timer_config = {
            "heartbeat": {
                "enabled": config.services.heartbeat_enabled,
                "intervalSeconds": config.services.heartbeat_interval_seconds,
            },
            "monologue": {
                "enabled": config.services.monologue_enabled,
                "idleSeconds": config.services.monologue_idle_seconds,
            },
            "dream": {
                "enabled": config.services.dream_enabled,
                "sleepIntervalHours": config.services.dream_sleep_interval_hours,
            },
        }

        timer = UnifiedTimer(
            workspace=config.workspace_path,
            config=timer_config,
            heartbeat_service=heartbeat,
            monologue_service=agent.monologue,
            provider=agent.provider,
            model=agent.model,
        )

        # Pass timer to agent so it can reset monologue counter on user messages
        agent._unified_timer = timer

        try:
            await timer.start()
            await cron.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            timer.stop()
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show SpeckBot status."""
    from speckbot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} SpeckBot Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        # Show provider info from new config structure
        active_provider = config.get_provider()
        active_model = config.get_model()
        console.print(f"Provider: {config.agents.defaults.provider}")
        console.print(f"Model: {active_model or '[red]not set[/red]'}")

        # List all configured providers
        for p in config.providers:
            if p.name == config.agents.defaults.provider:
                status = f"[green]✓ (active)[/green]"
            elif p.api_key:
                status = "[green]✓[/green]"
            else:
                status = "[dim]not set[/dim]"
            console.print(f"  {p.name}: {status}")


if __name__ == "__main__":
    app()
