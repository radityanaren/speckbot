"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from speckbot.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    normalize_path_component,
)
from speckbot.utils.constants import MAX_CONSOLIDATION_FAILURES, MAX_CONSOLIDATION_ROUNDS

if TYPE_CHECKING:
    from speckbot.providers.base import LLMProvider
    from speckbot.session.manager import Session, SessionManager


# =============================================================================
# Context Summary for Conveyor Belt
# =============================================================================


@dataclass
class SummaryConfig:
    """Configuration for message summarization."""

    enabled: bool = True
    user_max_chars: int = 100
    tool_max_chars: int = 80
    result_max_chars: int = 100
    assistant_max_chars: int = 150


class MessageSummaryExtractor:
    """Extracts compact summaries from messages for context preservation."""

    def __init__(self, config: SummaryConfig | None = None):
        self.config = config or SummaryConfig()

    def extract(self, messages: list[dict[str, Any]]) -> str:
        """Extract summary from a list of messages.

        Args:
            messages: List of message dicts to summarize

        Returns:
            Formatted summary string for context
        """
        if not messages or not self.config.enabled:
            return ""

        lines: list[str] = []

        for msg in messages:
            role = msg.get("role", "")
            timestamp = self._extract_timestamp(msg)
            summary_line = self._summarize_message(msg, role, timestamp)
            if summary_line:
                lines.append(summary_line)

        return "\n".join(lines)

    def _extract_timestamp(self, msg: dict) -> str:
        """Extract HH:MM from message timestamp."""
        ts = msg.get("timestamp", "")
        if not ts:
            return ""
        try:
            # Handle ISO format: 2026-04-14T19:45:35.316
            if "T" in ts:
                time_part = ts.split("T")[1].split(".")[0]
                return time_part[:5]  # HH:MM
            return ts[:5]
        except Exception:
            return ""

    def _summarize_message(self, msg: dict, role: str, timestamp: str) -> str:
        """Summarize a single message based on role."""
        ts = f"[{timestamp}]" if timestamp else "[--:--]"

        if role == "user":
            return self._summarize_user(msg, ts)
        elif role == "assistant":
            return self._summarize_assistant(msg, ts)
        elif role == "tool":
            return self._summarize_tool_result(msg, ts)
        return ""

    def _summarize_user(self, msg: dict, ts: str) -> str:
        """Summarize user message."""
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multimodal - extract text
            text_parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text_parts.append(c.get("text", ""))
            content = " ".join(text_parts)

        if not content:
            return ""

        # Strip runtime context tag if present
        content = re.sub(r"\[Runtime Context.*?\]\s*", "", content, flags=re.DOTALL).strip()

        # Truncate to limit
        truncated, was_truncated = self._truncate(content, self.config.user_max_chars)
        truncation_note = " [TRUNCATED]" if was_truncated else ""

        return f'{ts} USER: "{truncated}..."{truncation_note}'

    def _summarize_assistant(self, msg: dict, ts: str) -> str:
        """Summarize assistant message."""
        content = msg.get("content", "")

        # Check for tool calls
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            tool_names = [tc.get("function", {}).get("name", "unknown") for tc in tool_calls]
            return f"{ts} ASST: *calls tools: {', '.join(tool_names)}"

        if not content:
            return f"{ts} ASST: *(no content)"

        # Truncate response
        truncated, was_truncated = self._truncate(content, self.config.assistant_max_chars)
        truncation_note = " [TRUNCATED]" if was_truncated else ""

        return f'{ts} ASST: "{truncated}..."{truncation_note}'

    def _summarize_tool_result(self, msg: dict, ts: str) -> str:
        """Summarize tool result message."""
        tool_name = msg.get("name", "unknown")
        content = msg.get("content", "")

        # Format tool call info
        result_line = f"{ts} TOOL {tool_name} →"

        if not content:
            return f"{result_line} *(no result)*"

        # Truncate result
        truncated, was_truncated = self._truncate(content, self.config.result_max_chars)
        truncation_note = " [TRUNCATED]" if was_truncated else ""

        return f'{result_line} "{truncated}..."{truncation_note}'

    def _truncate(self, text: str, limit: int) -> tuple[str, bool]:
        """Truncate text to limit, return (truncated, was_truncated)."""
        if len(text) <= limit:
            return text, False
        return text[:limit], True


# Tool for consolidation (internal LLM call) - just save_memory
_CONSOLIDATION_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                },
                "required": ["history_entry"],
            },
        },
    },
]

# Tools for the agent to use during normal conversation
_AGENT_MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_knowledge",
            "description": "Save knowledge to a topic folder. Topics are broad (e.g., 'macroeconomy-2026'), files within can be specific (e.g., 'analysis.md', 'notes.md').",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Broad topic/folder name for this knowledge (e.g., 'macroeconomy-2026', 'trading-strategy'). Use lowercase, hyphens allowed.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The knowledge to save. Can be multiple paragraphs.",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Filename without .md (e.g., 'analysis', 'notes', 'summary'). Choose based on content type. Default: 'notes'",
                        "default": "notes",
                    },
                },
                "required": ["topic", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_project",
            "description": "Save project info to a topic folder. Topics are broad project names, files within describe specific aspects (e.g., 'strategy.md', 'setup.md').",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Project/folder name (e.g., 'trading-bot', 'speckbot'). Use lowercase, hyphens allowed.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The project information to save. Can be multiple paragraphs.",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Filename without .md (e.g., 'strategy', 'setup', 'notes'). Choose based on content type. Default: 'notes'",
                        "default": "notes",
                    },
                },
                "required": ["topic", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "List all saved knowledges and projects.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# For backwards compatibility
_SAVE_MEMORY_TOOL = _CONSOLIDATION_TOOL + _AGENT_MEMORY_TOOLS


# Tool name to handler method mapping
_MEMORY_TOOL_HANDLERS = {
    "save_memory": "_handle_save_memory",
    "save_knowledge": "_handle_save_knowledge",
    "save_project": "_handle_save_project",
    "list_memories": "_handle_list_memories",
}


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(m in text for m in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """
    Three-layer memory system:
    - knowledges/ - Factual/technical knowledge (individual .md files per topic)
    - projects/ - Project-specific context (individual .md files per project)
    """

    # Use constants for thresholds
    _max_failures = MAX_CONSOLIDATION_FAILURES

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.knowledges_dir = ensure_dir(workspace / "knowledges")
        self.projects_dir = ensure_dir(workspace / "projects")
        self._consecutive_failures = 0

    # === Knowledge Management ===

    def list_knowledges(self) -> list[str]:
        """List all knowledge topic names (folder names)."""
        if not self.knowledges_dir.exists():
            return []
        return sorted([d.name for d in self.knowledges_dir.iterdir() if d.is_dir()])

    def list_knowledge_files(self, topic: str) -> list[str]:
        """List all files in a knowledge topic folder."""
        topic_dir = self.knowledges_dir / topic
        if not topic_dir.exists():
            return []
        return sorted([f.stem for f in topic_dir.iterdir() if f.is_file() and f.suffix == ".md"])

    def get_knowledge(self, topic: str, file_type: str = "notes") -> str | None:
        """Get knowledge content from a specific file in a topic folder."""
        path = self.knowledges_dir / topic / f"{file_type}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def save_knowledge(self, topic: str, content: str, file_type: str = "notes") -> None:
        """Save knowledge to a topic folder with descriptive filename."""
        folder = ensure_dir(self.knowledges_dir / topic)
        path = folder / f"{file_type}.md"
        path.write_text(content, encoding="utf-8")
        logger.info("Saved knowledge: {}/{}", topic, file_type)

    def knowledge_exists(self, topic: str, file_type: str = "notes") -> bool:
        """Check if a knowledge file exists in a topic folder."""
        return (self.knowledges_dir / topic / f"{file_type}.md").exists()

    # === Project Management ===

    def list_projects(self) -> list[str]:
        """List all project topic names (folder names)."""
        if not self.projects_dir.exists():
            return []
        return sorted([d.name for d in self.projects_dir.iterdir() if d.is_dir()])

    def list_project_files(self, topic: str) -> list[str]:
        """List all files in a project topic folder."""
        topic_dir = self.projects_dir / topic
        if not topic_dir.exists():
            return []
        return sorted([f.stem for f in topic_dir.iterdir() if f.is_file() and f.suffix == ".md"])

    def get_project(self, topic: str, file_type: str = "notes") -> str | None:
        """Get project content from a specific file in a topic folder."""
        path = self.projects_dir / topic / f"{file_type}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def save_project(self, topic: str, content: str, file_type: str = "notes") -> None:
        """Save project info to a topic folder with descriptive filename."""
        folder = ensure_dir(self.projects_dir / topic)
        path = folder / f"{file_type}.md"
        path.write_text(content, encoding="utf-8")
        logger.info("Saved project: {}/{}", topic, file_type)

    def project_exists(self, topic: str, file_type: str = "notes") -> bool:
        """Check if a project file exists in a topic folder."""
        return (self.projects_dir / topic / f"{file_type}.md").exists()

    # === History (legacy support) ===

    def read_long_term(self) -> str:
        """Legacy method - returns empty string for backwards compatibility."""
        return ""

    def write_long_term(self, content: str) -> None:
        """Legacy method - no-op for backwards compatibility."""
        pass

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        """Build memory context for the agent."""
        parts = []

        # Hardcoded descriptions - added BEFORE listing
        parts.append("# Memory System\n")
        parts.append(
            "knowledges/ - Folder for factual/technical knowledge. Use save_knowledge tool to save."
        )
        parts.append(
            "projects/ - Folder for project-specific context. Use save_project tool to save.\n"
        )

        # List available knowledges
        knowledges = self.list_knowledges()
        if knowledges:
            parts.append("## Knowledges\n")
            for topic in knowledges:
                files = self.list_knowledge_files(topic)
                if files:
                    parts.append(f"- {topic}: {', '.join(files)}")
                else:
                    parts.append(f"- {topic}")
            parts.append("\nUse read_file to load: `{workspace}/knowledges/<topic>/<file>.md`")

        # List available projects
        projects = self.list_projects()
        if projects:
            parts.append("\n## Projects\n")
            for topic in projects:
                files = self.list_project_files(topic)
                if files:
                    parts.append(f"- {topic}: {', '.join(files)}")
                else:
                    parts.append(f"- {topic}")
            parts.append("\nUse read_file to load: `{workspace}/projects/<topic>/<file>.md`")

        return "\n".join(parts) if parts else ""

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = (
                f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            )
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into HISTORY.md."""
        if not messages:
            return True

        prompt = f"""Process this conversation and call the save_memory tool with a summary.

## Conversation to Process
{self._format_messages(messages)}"""

        chat_messages = [
            {
                "role": "system",
                "content": "You are a memory consolidation agent. Call the save_memory tool with a summary of the conversation.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_CONSOLIDATION_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(response.content):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_CONSOLIDATION_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            if "history_entry" not in args:
                logger.warning("Memory consolidation: save_memory payload missing history_entry")
                return self._fail_or_raw_archive(messages)

            entry = args["history_entry"]

            if entry is None:
                logger.warning("Memory consolidation: history_entry is null")
                return self._fail_or_raw_archive(messages)

            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages)

            self.append_history(entry)

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    # === Tool Handlers ===

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        """Handle memory tool calls from the agent."""
        handler_name = _MEMORY_TOOL_HANDLERS.get(tool_name)
        if handler_name is None:
            return f"Unknown tool: {tool_name}"

        handler = getattr(self, handler_name, None)
        if handler is None:
            return f"Handler not found: {handler_name}"

        try:
            return handler(args)
        except Exception as e:
            logger.exception("Error handling tool call {}", tool_name)
            return f"Error: {e}"

    def _handle_save_memory(self, args: dict) -> str:
        """Handle save_memory tool call."""
        entry = args.get("history_entry", "")
        if not entry:
            return "Error: history_entry is required"
        self.append_history(_ensure_text(entry))
        return "Saved to history."

    def _handle_save_knowledge(self, args: dict) -> str:
        """Handle save_knowledge tool call."""
        topic = args.get("topic", "")
        content = args.get("content", "")
        file_type = args.get("file_type", "notes")
        if not topic or not content:
            return "Error: topic and content are required"
        topic = normalize_path_component(topic)
        file_type = normalize_path_component(file_type)
        self.save_knowledge(topic, content, file_type)
        return f"Saved to knowledges/{topic}/{file_type}.md"

    def _handle_save_project(self, args: dict) -> str:
        """Handle save_project tool call."""
        topic = args.get("topic", "")
        content = args.get("content", "")
        file_type = args.get("file_type", "notes")
        if not topic or not content:
            return "Error: topic and content are required"
        topic = normalize_path_component(topic)
        file_type = normalize_path_component(file_type)
        self.save_project(topic, content, file_type)
        return f"Saved to projects/{topic}/{file_type}.md"

    def _handle_list_memories(self, args: dict) -> str:
        """Handle list_memories tool call."""
        knowledges = self.list_knowledges()
        projects = self.list_projects()

        parts = []
        if knowledges:
            parts.append(f"Knowledges ({len(knowledges)}):")
            for topic in knowledges:
                files = self.list_knowledge_files(topic)
                files_str = ", ".join(files) if files else "(empty)"
                parts.append(f"  • {topic}: {files_str}")
        else:
            parts.append("Knowledges: (none)")

        if projects:
            parts.append(f"Projects ({len(projects)}):")
            for topic in projects:
                files = self.list_project_files(topic)
                files_str = ", ".join(files) if files else "(empty)"
                parts.append(f"  • {topic}: {files_str}")
        else:
            parts.append("Projects: (none)")

        return "\n".join(parts)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._max_failures:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to HISTORY.md without LLM summarization."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n{self._format_messages(messages)}"
        )
        logger.warning("Memory consolidation degraded: raw-archived {} messages", len(messages))


# =============================================================================
# Memory Tools for Agent
# =============================================================================


class SaveKnowledgeTool:
    """Tool for saving knowledge to workspace. Fetches from conversation memory."""

    def __init__(self, store: MemoryStore, sessions: "SessionManager"):
        from speckbot.agent.tools.base import Tool

        self._store = store
        self._sessions = sessions
        self._tool = _SaveKnowledgeToolImpl(store, sessions)

    @property
    def name(self) -> str:
        return "save_knowledge"

    @property
    def description(self) -> str:
        return (
            "Save knowledge to a topic folder. IMPORTANT: The 'content' parameter should be derived "
            "from the CURRENT CONVERSATION MEMORY. Ask the user what topic/folder to save under, "
            "then fetch the relevant conversation content to save."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Broad topic/folder name (e.g., 'anjir-hidayat-research'). Use lowercase with hyphens.",
                },
                "file_type": {
                    "type": "string",
                    "description": "Filename without .md (e.g., 'analysis', 'notes'). Default: 'notes'",
                    "default": "notes",
                },
                "session_key": {
                    "type": "string",
                    "description": "The current session key to fetch conversation from (e.g., 'discord:123').",
                },
            },
            "required": ["topic", "session_key"],
        }

    async def execute(self, topic: str, file_type: str = "notes", session_key: str = "") -> str:
        """
        Execute save_knowledge tool.

        Fetches conversation from session memory and saves to knowledge folder.
        """
        try:
            return await self._tool.save_knowledge(topic, file_type, session_key)
        except Exception as e:
            logger.exception("Error in save_knowledge tool")
            return f"Error saving knowledge: {e}"


class _SaveKnowledgeToolImpl:
    """Internal implementation that fetches from session memory."""

    def __init__(self, store: MemoryStore, sessions: "SessionManager"):
        self._store = store
        self._sessions = sessions

    async def save_knowledge(self, topic: str, file_type: str, session_key: str) -> str:
        """
        Save knowledge by fetching conversation from session.

        The actual content to save should come from the current conversation history.
        This tool fetches the relevant messages from the session.
        """
        if not topic:
            return "Error: topic is required"

        if not session_key:
            return "Error: session_key is required to fetch conversation"

        # Fetch conversation from session memory
        session = self._sessions.get_or_create(session_key)
        messages = session.messages

        if not messages:
            return "Error: No conversation found in session memory"

        # Build content from conversation
        content = self._build_content_from_messages(messages)

        if not content:
            return "Error: Could not extract content from conversation"

        # Save to knowledge folder
        self._store.save_knowledge(topic, content, file_type or "notes")

        logger.info("Saved knowledge: {}/{}", topic, file_type)
        return f"Saved to knowledges/{topic}/{file_type or 'notes'}.md"

    def _build_content_from_messages(self, messages: list[dict[str, Any]]) -> str:
        """Build knowledge content from conversation messages."""
        lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")

            if not content:
                continue

            # Format timestamp
            ts = ""
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp)
                    ts = dt.strftime("%H:%M")
                except Exception:
                    pass

            # Extract content based on role
            if role == "user":
                lines.append(f"**User** ({ts}): {content}")
            elif role == "assistant":
                # Check for tool calls
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tool_names = [
                        tc.get("function", {}).get("name", "unknown") for tc in tool_calls
                    ]
                    lines.append(f"**Assistant** ({ts}): *calls tools: {', '.join(tool_names)}*")
                else:
                    # Truncate long responses
                    display = content[:500] + "..." if len(content) > 500 else content
                    lines.append(f"**Assistant** ({ts}): {display}")
            elif role == "tool":
                tool_name = msg.get("name", "tool")
                # Truncate tool results
                display = content[:200] + "..." if len(content) > 200 else content
                lines.append(f"*[{tool_name} result]: {display}*")

        return "\n\n".join(lines)


class SaveProjectTool:
    """Tool for saving project info to workspace."""

    def __init__(self, store: MemoryStore, sessions: "SessionManager"):
        self._store = store
        self._sessions = sessions
        self._tool = _SaveProjectToolImpl(store, sessions)

    @property
    def name(self) -> str:
        return "save_project"

    @property
    def description(self) -> str:
        return (
            "Save project info to a topic folder. IMPORTANT: The content should be derived "
            "from the CURRENT CONVERSATION MEMORY. Ask the user what project/folder to save under, "
            "then fetch the relevant conversation content to save."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Project/folder name (e.g., 'my-bot-project'). Use lowercase with hyphens.",
                },
                "file_type": {
                    "type": "string",
                    "description": "Filename without .md (e.g., 'strategy', 'setup'). Default: 'notes'",
                    "default": "notes",
                },
                "session_key": {
                    "type": "string",
                    "description": "The current session key to fetch conversation from (e.g., 'discord:123').",
                },
            },
            "required": ["topic", "session_key"],
        }

    async def execute(self, topic: str, file_type: str = "notes", session_key: str = "") -> str:
        """Execute save_project tool."""
        try:
            return await self._tool.save_project(topic, file_type, session_key)
        except Exception as e:
            logger.exception("Error in save_project tool")
            return f"Error saving project: {e}"


class _SaveProjectToolImpl:
    """Internal implementation that fetches from session memory."""

    def __init__(self, store: MemoryStore, sessions: "SessionManager"):
        self._store = store
        self._sessions = sessions

    async def save_project(self, topic: str, file_type: str, session_key: str) -> str:
        """Save project by fetching conversation from session."""
        if not topic:
            return "Error: topic is required"

        if not session_key:
            return "Error: session_key is required to fetch conversation"

        session = self._sessions.get_or_create(session_key)
        messages = session.messages

        if not messages:
            return "Error: No conversation found in session memory"

        content = self._build_content_from_messages(messages)

        if not content:
            return "Error: Could not extract content from conversation"

        self._store.save_project(topic, content, file_type or "notes")

        logger.info("Saved project: {}/{}", topic, file_type)
        return f"Saved to projects/{topic}/{file_type or 'notes'}.md"

    def _build_content_from_messages(self, messages: list[dict[str, Any]]) -> str:
        """Build project content from conversation messages."""
        lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")

            if not content:
                continue

            ts = ""
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp)
                    ts = dt.strftime("%H:%M")
                except Exception:
                    pass

            if role == "user":
                lines.append(f"**User** ({ts}): {content}")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tool_names = [
                        tc.get("function", {}).get("name", "unknown") for tc in tool_calls
                    ]
                    lines.append(f"**Assistant** ({ts}): *calls tools: {', '.join(tool_names)}*")
                else:
                    display = content[:500] + "..." if len(content) > 500 else content
                    lines.append(f"**Assistant** ({ts}): {display}")

        return "\n\n".join(lines)


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    # Use constants for limits
    _max_rounds = MAX_CONSOLIDATION_ROUNDS

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        active_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        context_headroom: int = 20,
        summary_config: SummaryConfig | None = None,
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.active_window_tokens = active_window_tokens
        self.context_headroom = context_headroom  # Headroom percentage for safety buffer
        self.summary_config = summary_config or SummaryConfig()
        self.summary_extractor = MessageSummaryExtractor(self.summary_config)
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._checked_initial_overflow: set[str] = set()  # Track checked sessions

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive messages (no LLM needed - conveyor belt uses raw JSONL)."""
        return True

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_archived
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = session.key.split(":", 1) if ":" in session.key else (None, None)
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive messages to JSONL (no LLM needed)."""
        # Messages are archived via SessionManager.archive_session
        # This is a no-op placeholder since archiving happens in maybe_archive_by_tokens
        return True

    async def maybe_archive_by_tokens(self, session: Session) -> None:
        """
        Archive old messages until prompt fits within active_window_tokens.
        Uses conveyor belt: messages are archived to JSONL, user reads on-demand.

        Safety: Uses context_headroom to calculate effective threshold, giving
        a buffer for estimation errors and preventing overflow crashes.
        """
        if not session.messages or self.active_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            # Apply headroom: effective threshold = (1 - headroom%) of active_window_tokens
            # This gives a safety buffer for estimation errors
            effective_threshold = int(self.active_window_tokens * (1 - self.context_headroom / 100))
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return

            # Check if initial context (system + tools + bootstrap) already exceeds threshold
            if session.key not in self._checked_initial_overflow:
                self._checked_initial_overflow.add(session.key)
                if estimated > effective_threshold:
                    logger.warning(
                        "Conveyor belt: Initial context ({} tokens) exceeds effective threshold "
                        "({} tokens, {}% headroom). Consider increasing active_window_tokens or "
                        "reducing context_headroom. Processing will continue with degraded context.",
                        estimated,
                        effective_threshold,
                        self.context_headroom,
                    )

            # If still under effective threshold, no archiving needed
            if estimated < effective_threshold:
                logger.debug(
                    "Conveyor belt idle {}: {}/{} ({}% headroom)",
                    session.key,
                    estimated,
                    effective_threshold,
                    self.context_headroom,
                )
                return

            # Archive messages until under effective threshold
            for round_num in range(self._max_rounds):
                if estimated <= effective_threshold:
                    return

                # Find boundary at user turn
                boundary = self.pick_consolidation_boundary(
                    session, max(1, estimated - effective_threshold)
                )
                if boundary is None:
                    logger.debug(
                        "Conveyor belt: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_archived : end_idx]
                if not chunk:
                    return

                logger.info(
                    "Conveyor belt round {} for {}: archiving {} msgs, {}/{}",
                    round_num,
                    session.key,
                    len(chunk),
                    estimated,
                    effective_threshold,
                )

                # UPDATE the marker before archiving
                session.last_archived = end_idx

                # Extract summary BEFORE archiving (to preserve context)
                summary = self.summary_extractor.extract(chunk)

                # Archive to JSONL and get archive note
                archived, archive_note = self.sessions.archive_session(session)

                # Append summary to session's accumulated summary
                if summary:
                    session.append_summary(summary)
                if archive_note:
                    session.append_summary(archive_note)

                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return
