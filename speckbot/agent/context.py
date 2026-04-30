"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any

from speckbot.session.memory import MemoryStore
from speckbot.skills import SkillsLoader
from speckbot.utils.helpers import (
    build_assistant_message,
    current_time_str,
    detect_image_mime,
    detect_video_mime,
)


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "MEMORY.md", "JOURNAL.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    # Hardcoded file descriptions - added BEFORE reading each file
    _BOOTSTRAP_DESCRIPTIONS = {
        "AGENTS.md": "File: Your instructions and behavioral guidelines.",
        "MEMORY.md": "File: Index of saved knowledges (knowledges/) and projects (projects/). Use save_knowledge and save_project tools to save.",
        "JOURNAL.md": "File: Your private inner monologue journal entries.",
    }

    def __init__(self, workspace: Path, security=None):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self.security = security  # Shared security service
        self.monologue_enabled = False  # Set by AgentLoop if enabled
        self.tool_result_max_chars = 10_000  # Default, set by AgentLoop

    def set_monologue_config(self, enabled: bool, idle_seconds: int = 300) -> None:
        """Configure monologue system for identity prompt."""
        self.monologue_enabled = enabled
        self.monologue_idle_seconds = idle_seconds

    def set_tool_result_max_chars(self, max_chars: int) -> None:
        """Set the max characters for tool result truncation."""
        self.tool_result_max_chars = max_chars

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        journal_entries: int = 10,
        context_summary: str = "",
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity()]

        # Add context summary if available (from conveyor belt)
        if context_summary:
            parts.append(context_summary)

        bootstrap = self._load_bootstrap_files(journal_entries=journal_entries)
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}\n")
            parts.append("Use read_file to load specific memories as needed.")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        # Only show the relevant platform based on detected system
        if system == "Windows":
            platform_info = "Windows: prefer file tools or cmd commands."
        else:
            platform_info = "POSIX (macOS/Linux): standard shell tools available."

        # Monologue section - only if enabled
        monologue_section = ""
        if self.monologue_enabled:
            monologue_section = f"""### Monologue
- When idle ({self.monologue_idle_seconds}s), expects monologue, answer it for your own mind, not for the user
- If you want to message the user, use 'message' tool
"""
        else:
            monologue_section = """### Monologue
- Disabled

"""

        return f"""# You are a helpful AI agent.

## Runtime
{runtime}

## Workspace
{workspace_path}

{monologue_section}

## Platform
- {platform_info}

## Security
- Dangerous commands/patterns are blocked.
- If output shows "[Output filtered by security]", it was blocked - don't retry.

## Tool Result Limits
- Tool results exceeding {self.tool_result_max_chars:,} characters will be truncated.
- Design queries to fit within this limit when possible.

## CRITICAL: Current Date and Time
- The current date is **April 2026** (not 2024, not alternate timeline).
- Today's date: **April 19, 2026** (Sunday).
- When searching the web for current information, search for **April 2026** data, NOT past years.
- If user asks about "current" or "recent" events, assume they mean April 2026.
- **This is NOT a hypothetical scenario - it is April 2026 right now.**

## Saving Memories
When user wants to save something:
1. Ask: knowledge or project?
2. Ask: topic name (e.g., 'anjir-hidayat-research')
3. Ask: file name (e.g., 'analysis', 'notes')
4. Call save_knowledge(topic, content, file_type) or save_project(topic, content, file_type)
IMPORTANT: You must provide the 'content' parameter - summarize the relevant conversation into clean markdown.

## Guidelines
- Read before edit. Re-read after write.
- Don't assume files exist.
- Ask if unclear.

Reply directly. Use 'message' tool only for chat."""

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        user_id: str | None = None,
        username: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str()}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        # Include user info if available (for group chats to distinguish users)
        if user_id:
            user_info = f"User ID: {user_id}"
            if username:
                user_info += f" (@{username})"
            lines.append(user_info)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self, journal_entries: int = 10) -> str:
        """Load bootstrap files from workspace with entry limits."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if not file_path.exists():
                continue

            content = file_path.read_text(encoding="utf-8")
            description = self._BOOTSTRAP_DESCRIPTIONS.get(filename, f"File: {filename}")

            # For JOURNAL.md, only load last N entries
            if filename == "JOURNAL.md" and content and journal_entries > 0:
                content = self._limit_journal_entries(content, max_entries=journal_entries)

            parts.append(f"## {filename}\n{description}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def _limit_journal_entries(self, content: str, max_entries: int = 20) -> str:
        """Limit journal to last N entries (each line starting with '- [' is an entry)."""
        lines = content.split("\n")
        entries: list[str] = []
        current_entry: list[str] = []

        for line in lines:
            # Journal entries start with "- ["
            if line.startswith("- ["):
                if current_entry:
                    entries.append("\n".join(current_entry))
                    current_entry = []
                current_entry.append(line)
            elif current_entry:
                current_entry.append(line)

        # Add the last entry if any
        if current_entry:
            entries.append("\n".join(current_entry))

        # Return last N entries
        limited = entries[-max_entries:] if entries else []
        return "\n".join(limited)

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        hooks_config: dict[str, Any] | None = None,
        session_key: str | None = None,
        skip_security: bool = False,
        user_id: str | None = None,
        username: str | None = None,
        journal_entries: int = 10,
        context_summary: str = "",
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""

        # Use shared security service if available
        security = self.security

        # Scan user message for blocked content
        if not skip_security and security and security.enabled:
            block_result = security.scan_input(current_message)
            if block_result.is_blocked:
                return [
                    {
                        "role": "system",
                        "content": "Error: Your message was blocked by security filters. It contains restricted patterns. Please rephrase and try again.",
                    },
                ]

        runtime_ctx = self._build_runtime_context(channel, chat_id, user_id, username)
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        return [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names, journal_entries=journal_entries, context_summary=context_summary
                ),
            },
            *history,
            {"role": current_role, "content": merged},
        ]

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images and videos."""
        if not media:
            return text

        images = []
        videos = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or detect_video_mime(raw) or mimetypes.guess_type(path)[0]

            if mime and mime.startswith("image/"):
                b64 = base64.b64encode(raw).decode()
                images.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                        "_meta": {"path": str(p)},
                    }
                )
            elif mime and mime.startswith("video/"):
                b64 = base64.b64encode(raw).decode()
                videos.append(
                    {
                        "type": "video",
                        "video": {"url": f"data:{mime};base64,{b64}"},
                        "_meta": {"path": str(p)},
                    }
                )

        content_parts = []
        if videos:
            content_parts.extend(videos)
        if images:
            content_parts.extend(images)
        if text:
            content_parts.append({"type": "text", "text": text})

        if not content_parts:
            return text
        return content_parts

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result}
        )
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(
            build_assistant_message(
                content,
                tool_calls=tool_calls,
                reasoning_content=reasoning_content,
                thinking_blocks=thinking_blocks,
            )
        )
        return messages
