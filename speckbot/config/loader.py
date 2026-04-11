"""Configuration loading utilities."""

import json
import os
import re
from pathlib import Path

import pydantic
from loguru import logger

from speckbot.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".speckbot" / "config.json"


def load_env(env_path: Path) -> dict[str, str]:
    """Load .env file and return as dict.

    Args:
        env_path: Path to .env file

    Returns:
        Dict of {VARIABLE_NAME: value}
    """
    env = {}
    if not env_path.exists():
        return env

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()

    logger.debug(f"Loaded {len(env)} env vars from {env_path}")
    return env


def interpolate_env_vars(data: dict, env_vars: dict[str, str]) -> dict:
    """Recursively replace ${VAR} with env var values.

    Args:
        data: Config dict with potential ${VAR} placeholders
        env_vars: Dict of env var names to values

    Returns:
        Data with placeholders replaced

    Raises:
        ValueError: If a ${VAR} is used but not defined in .env and has no fallback
    """
    missing_vars = []

    if isinstance(data, str):
        # Replace ${VAR} patterns
        pattern = r"\$\{(\w+)\}"

        def replace_var(match):
            var_name = match.group(1)
            if var_name in env_vars:
                return env_vars[var_name]
            # If not found, keep original and track for warning/error
            missing_vars.append(var_name)
            return match.group(0)

        result = re.sub(pattern, replace_var, data)

        # Raise error if missing vars found (fail fast)
        if missing_vars:
            raise ValueError(
                f"Missing .env variables: {', '.join(missing_vars)}. "
                f"Add these to .env file next to config.json, or remove ${'{...}'} from config."
            )

        return result

    elif isinstance(data, dict):
        result = {}
        for key, value in data.items():
            try:
                result[key] = interpolate_env_vars(value, env_vars)
            except ValueError as e:
                raise ValueError(f"Error in config key '{key}': {e}")
        return result

    elif isinstance(data, list):
        result = []
        for i, item in enumerate(data):
            try:
                result.append(interpolate_env_vars(item, env_vars))
            except ValueError as e:
                raise ValueError(f"Error in config list item {i}: {e}")
        return result

    return data


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            # Determine .env path: use agents.env_file_path if set, otherwise default to config directory
            env_file_path = None
            agents_data = data.get("agents", {})
            if agents_data and agents_data.get("env_file_path"):
                env_file_path = Path(agents_data["env_file_path"]).expanduser()

            # Fall back to .env next to config if not specified
            if not env_file_path:
                env_file_path = path.parent / ".env"

            # Load .env and interpolate ${VAR} patterns
            if env_file_path.exists():
                env_vars = load_env(env_file_path)
                data = interpolate_env_vars(data, env_vars)
                logger.debug(f"Loaded .env from {env_file_path}")

            data = _migrate_config(data)
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning(f"Failed to load config from {path}: {e}")
            logger.warning("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
