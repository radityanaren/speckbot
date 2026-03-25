"""Central command definitions for SpeckBot.

All bot commands are defined here in one place. Channels and the agent
import from here instead of duplicating command strings.
"""

from dataclasses import dataclass


# =============================================================================
# Command Definitions
# =============================================================================


@dataclass
class Command:
    """A bot command definition."""

    name: str  # e.g., "/stop"
    description: str  # e.g., "Stop the current task"
    help_text: str  # e.g., "/stop — Stop the current task"


# All commands - one source of truth
COMMANDS: dict[str, Command] = {
    "stop": Command(
        name="/stop",
        description="Stop the current task",
        help_text="/stop — Stop the current task",
    ),
    "restart": Command(
        name="/restart",
        description="Restart SpeckBot",
        help_text="/restart — Restart the bot",
    ),
    "memories": Command(
        name="/memories",
        description="Show saved memories",
        help_text="/memories — Show saved memories",
    ),
    "help": Command(
        name="/help",
        description="Show available commands",
        help_text="/help — Show available commands",
    ),
}


# =============================================================================
# Help Text
# =============================================================================

# Telegram Bot Commands (for /setcommands API)
TELEGRAM_BOT_COMMANDS = [
    ("stop", "Stop the current task"),
    ("restart", "Restart SpeckBot"),
    ("memories", "Show saved memories"),
    ("help", "Show available commands"),
]


def get_help_text() -> str:
    """Get the help text for display."""
    lines = ["🐜 SpeckBot commands:"]
    for cmd in COMMANDS.values():
        lines.append(cmd.help_text)
    return "\n".join(lines)


def get_command_by_name(name: str) -> Command | None:
    """Get a command by its name (with or without slash)."""
    # Remove leading slash if present
    key = name.lstrip("/")
    return COMMANDS.get(key)


def get_supported_commands() -> list[str]:
    """Get list of supported command names."""
    return list(COMMANDS.keys())
