"""Central command definitions for SpeckBot.

All bot commands are defined here in one place. Channels and the agent
import from here instead of duplicating command strings.
"""

# Single source of truth: command_name -> description
_COMMANDS = {
    "stop": "Stop the current task",
    "restart": "Restart SpeckBot",
    "flush": "Compact oldest 90% via LLM",
    "memories": "Show saved memories",
    "help": "Show available commands",
}


def get_help_text() -> str:
    """Get the help text for display."""
    lines = ["🐜 SpeckBot commands:"]
    for cmd_name, description in _COMMANDS.items():
        lines.append(f"/{cmd_name} — {description}")
    return "\n".join(lines)


def get_command_by_name(name: str) -> str | None:
    """Get a command description by its name (with or without slash)."""
    key = name.lstrip("/")
    return _COMMANDS.get(key)


def get_supported_commands() -> list[str]:
    """Get list of supported command names."""
    return list(_COMMANDS.keys())


# Telegram Bot Commands (for /setcommands API)
# Dynamically derived from _COMMANDS to avoid duplication
TELEGRAM_BOT_COMMANDS = list(_COMMANDS.items())
