"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import weakref
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
    - HISTORY.md - Date-indexed grep-searchable log of conversations
    """

    # Use constants for thresholds
    _max_failures = MAX_CONSOLIDATION_FAILURES

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.knowledges_dir = ensure_dir(workspace / "knowledges")
        self.projects_dir = ensure_dir(workspace / "projects")
        self.history_file = workspace / "HISTORY.md"
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
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive a selected message chunk into persistent memory."""
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
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
        """Archive messages with guaranteed persistence (retries until raw-dump fallback)."""
        if not messages:
            return True
        for _ in range(self.store._max_failures):
            if await self.consolidate_messages(messages):
                return True
        return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: archive old messages until prompt fits within half the context window."""
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            target = self.context_window_tokens // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < self.context_window_tokens:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            for round_num in range(self._max_rounds):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated : end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                if not await self.consolidate_messages(chunk):
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return
