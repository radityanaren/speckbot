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
            # Skip messages that were archived as "skip" - they stay in RAM, not summarized
            if msg.get("_archived_as") == "skip":
                continue

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


# =============================================================================
# Message Segmentation for Segmented Conveyor Belt
# =============================================================================


def segment_messages(
    messages: list[dict[str, Any]],
) -> list[tuple[int, int, str]]:
    """Segment messages into conv/tool/skip blocks for segmented summarization.

    Simple explicit marker approach: _is_skip is set at message creation time
    in loop.py when assistant responds directly after tool results.

    Args:
        messages: List of message dicts to segment

    Returns:
        List of (start_idx, end_idx, segment_type) tuples.
        - 'conv': conversation block (user + assistant without tool_calls)
        - 'tool': tool call block (assistant with tool_calls + tool results)
        - 'skip': assistant content AFTER tool result (marked with _is_skip)
    """
    if not messages:
        return []

    segments: list[tuple[int, int, str]] = []
    i = 0

    while i < len(messages):
        role = messages[i].get("role", "")
        tool_calls = messages[i].get("tool_calls", [])
        is_skip = messages[i].get("_is_skip", False)

        if role == "assistant" and tool_calls:
            # Tool call block: assistant with tool_calls + all following tool results
            start = i
            j = i + 1
            while j < len(messages):
                next_role = messages[j].get("role", "")
                if next_role != "tool":
                    break
                j += 1
            segments.append((start, j, "tool"))
            i = j
        elif role == "assistant" and is_skip:
            # Explicit skip marker - assistant responds to tool results
            segments.append((i, i + 1, "skip"))
            i += 1
        elif role == "assistant" and not tool_calls:
            # Fallback for old messages without _is_skip - check backwards
            # Did this assistant follow a tool result?
            is_after_tool = False
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "tool":
                    is_after_tool = True
                    break
            if is_after_tool:
                segments.append((i, i + 1, "skip"))
                i += 1
                continue
            # Normal assistant - part of conversation until next message boundary
            start = i
            j = i + 1
            while j < len(messages):
                next_role = messages[j].get("role", "")
                next_tool_calls = messages[j].get("tool_calls", [])
                # Stop at tool or assistant with tool_calls
                if next_role == "tool" or (
                    next_role == "assistant" and next_tool_calls
                ):
                    break
                j += 1
            segments.append((start, j, "conv"))
            i = j
        elif role == "user":
            # User block extends until: tool, assistant with tool_calls, OR _is_skip marker
            # This ensures the user question goes WITH the tool block, not into conv
            start = i
            j = i + 1
            while j < len(messages):
                next_role = messages[j].get("role", "")
                next_tool_calls = messages[j].get("tool_calls", [])
                next_is_skip = messages[j].get("_is_skip", False)
                # Stop at tool result OR assistant with tool_calls OR skip assistant
                if next_role == "tool" or (
                    next_role == "assistant" and next_tool_calls
                ) or next_is_skip:
                    break
                j += 1
            segments.append((start, j, "conv"))
            i = j
        else:
            # Other roles (tool, system, etc.) - skip
            i += 1

    # DEBUG: Log segments result
    logger.debug(
        "segment_messages result: {}",
        [(start, end, stype) for start, end, stype in segments],
    )

    return segments


def _archive_tool_blocks(session: Session, sessions) -> bool:
    """Step 1: Archive MOST RECENT tool call + its result.

    Archives the MOST RECENT tool call block (assistant with tool_calls + following tool results).
    This preserves conversation in session longer.

    Returns True if archiving happened, False otherwise.

    Does NOT archive:
    - user messages
    - assistant responses (including _is_skip)
    """
    import json
    from pathlib import Path

    # Find ALL assistant with tool_calls (NOT just unarchived ones)
    all_tool_call_indices = []
    for i, msg in enumerate(session.messages):
        role = msg.get("role", "")
        tool_calls = msg.get("tool_calls", [])

        if role == "assistant" and tool_calls:
            all_tool_call_indices.append(i)

    if not all_tool_call_indices:
        return False  # No tool calls to archive

    # Get the MOST RECENT tool call block (last one)
    idx = all_tool_call_indices[-1]
    tool_call_msg = session.messages[idx]
    tool_call_tokens = estimate_message_tokens(tool_call_msg)

    # Find ALL tool results that follow this tool call (should capture ALL of them!)
    block_indices = [idx]  # Include the assistant message
    for j in range(idx + 1, len(session.messages)):
        if session.messages[j].get("role") == "tool":
            block_indices.append(j)
        else:
            break  # Stop at next non-tool message

    # Get actual messages in block (with full content!)
    block_messages = [session.messages[i] for i in block_indices]
    total_tokens = sum(estimate_message_tokens(msg) for msg in block_messages)

    # Debug log what's being archived
    for bi, msg in zip(block_indices, block_messages):
        role = msg.get("role", "unknown")
        content_len = len(msg.get("content", "")) if isinstance(msg.get("content"), str) else 0
        content_preview = str(msg.get("content", ""))[:50]
        msg_tokens = estimate_message_tokens(msg)
        logger.debug("  [{}] role={}, content_len={}, tokens={}", bi, role, content_len, msg_tokens)

    logger.info(
        "Step1: archiving {} messages (total {} tokens) from session",
        len(block_indices),
        total_tokens,
    )

    # Summarize the tool block (call + result)
    extractor = MessageSummaryExtractor(SummaryConfig())
    summary = extractor.extract(block_messages)
    if summary:
        marker = "[TOOL:] "
        summary_lines = summary.split("\n")
        summary_lines[0] = marker + summary_lines[0]
        for line in summary_lines:
            if line.strip():
                session.append_summary(line)

    # Write tool block to JSONL archive
    from speckbot.utils.helpers import safe_filename
    archive_file = sessions.archive_dir / f"{safe_filename(session.key)}.jsonl"
    with open(archive_file, "a", encoding="utf-8") as f:
        for msg in block_messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    # REMOVE the tool block from session (all at once, reverse order to preserve indices)
    for i in reversed(block_indices):
        session.messages.pop(i)

    # Track in session metadata
    session.metadata["last_archived_role"] = "assistant"

    # Save session
    sessions.save(session)

    logger.info("Archived tool block ({} messages, {} tokens removed)", len(block_indices), total_tokens)
    return True


def _extract_timestamp_from_summary(line: str) -> str:
    """Extract timestamp from summary line like '[TOOL:] [11:09] ...' or '[CONV:] [11:08] ...'."""
    import re

    # Match patterns like [11:09] or [09:40] or [23:59]
    match = re.search(r"\[(\d{2}:\d{2})\]", line)
    if match:
        return match.group(1)
    return "00:00"  # Default


def _archive_all_with_hardclip(session: Session, sessions, active_tokens: int, estimated: int) -> None:
    """Step 2: Clip OLDEST LINE regardless of source (summary OR message).

    Clips from BOTH summary_lines AND messages, always removing the oldest
    line based on timestamp. This ensures context stays fresh.

    Args:
        estimated: The FULL prompt token count (from maybe_archive_by_tokens) - MUST USE THIS
                  to ensure we're measuring the same thing as the threshold check.

    This is the LAST RESORT - should only be reached if Step1 didn't free enough space.
    """
    import json
    from pathlib import Path

    # Target: active_window_tokens + 5% (lazy overage allowed)
    target_tokens = int(active_tokens * 1.05)

    # CRITICAL: Use the passed estimated value, NOT recalculated!
    # This ensures we're using the same measurement as the threshold check
    current_tokens = estimated
    if current_tokens <= target_tokens:
        return  # Already under threshold

    # Build combined timeline: (timestamp, source, content)
    timeline = []

    # Add messages with timestamps
    for msg in session.messages:
        ts = msg.get("timestamp", "")
        if "T" in ts:
            ts = ts.split("T")[1][:5]
        elif not ts:
            ts = "00:00"
        timeline.append((ts, "message", msg))

    # Add summary lines with timestamps
    for line in session.summary_lines:
        ts = _extract_timestamp_from_summary(line)
        timeline.append((ts, "summary", line))

    # Sort by timestamp (oldest first)
    timeline.sort(key=lambda x: x[0])

    # Determine which items to remove (oldest first)
    # ALWAYS clip until we reach the target threshold
    items_to_remove = []
    tokens_removed = 0

    # Calculate actual message tokens before removing
    actual_msg_tokens = sum(estimate_message_tokens(m) for m in session.messages)

    for ts, source, content in timeline:
        items_to_remove.append((ts, source, content))
        if source == "message":
            msg_token_count = estimate_message_tokens(content)
            tokens_removed += msg_token_count
            # Check actual message tokens - this is what we're trying to reduce
            actual_msg_tokens -= msg_token_count
            # Stop when actual messages are under target (or close to it)
            if actual_msg_tokens <= target_tokens:
                break

    if not items_to_remove:
        return

    if not items_to_remove:
        return

    # Write removed messages to JSONL archive
    for ts, source, content in items_to_remove:
        if source == "message":
            from speckbot.utils.helpers import safe_filename
            archive_file = sessions.archive_dir / f"{safe_filename(session.key)}.jsonl"
            with open(archive_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(content, ensure_ascii=False) + "\n")

    # Rebuild session.messages - keep items NOT in remove list
    removed_contents = set(id(c) for _, _, c in items_to_remove)
    session.messages = [m for m in session.messages if id(m) not in removed_contents]

    # Rebuild session.summary_lines - keep items NOT in remove list
    removed_summaries = set(c for _, source, c in items_to_remove if source == "summary")
    session.summary_lines = [s for s in session.summary_lines if s not in removed_summaries]

    # Save session
    sessions.save(session)

    logger.info(
        "Step2: clipped {} oldest lines, {} message tokens (target: {} tokens)",
        len(items_to_remove),
        tokens_removed,
        target_tokens,
    )


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
    ) -> list[tuple[int, int, str]]:
        """Pick consolidation boundaries using segmented approach.

        Returns a list of (start_idx, end_idx, segment_type) tuples for archiving.
        Each segment is summarized with appropriate markers.
        - 'conv': conversation summary with [conv:] marker
        - 'tool': tool call summary with [tool:] marker
        - 'skip': assistant after tool, NOT archived (kept in RAM)
        """
        start_idx = session.last_archived
        if start_idx >= len(session.messages) or tokens_to_remove <= 0:
            return []

        # Get unprocessed messages
        unarchived = session.messages[start_idx:]
        if not unarchived:
            return []

        # Segment messages - uses _is_skip marker set at message creation
        segments = segment_messages(unarchived)

        # Calculate token counts and build boundaries
        boundaries: list[tuple[int, int, str]] = []
        accumulated_tokens = 0
        boundary_end = start_idx

        for seg_start, seg_end, seg_type in segments:
            seg_tokens = 0
            for i in range(seg_start, seg_end):
                if start_idx + i < len(session.messages):
                    msg = session.messages[start_idx + i]
                    # Don't count skip messages for archival
                    if seg_type != "skip":
                        seg_tokens += estimate_message_tokens(msg)

            accumulated_tokens += seg_tokens
            actual_start = boundary_end
            actual_end = start_idx + seg_end

            if accumulated_tokens >= tokens_to_remove:
                boundaries.append((actual_start, actual_end, seg_type))
                break
            else:
                boundaries.append((actual_start, actual_end, seg_type))
                boundary_end = actual_end

        return boundaries

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

        The agent reads ALL messages (see session.get_history()).
        Archiving is triggered when estimated > active_window_tokens.
        """
        if not session.messages or self.active_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            # Headroom is just for logging - threshold is active_window_tokens
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return

            # Check if initial context (system + tools + bootstrap) already exceeds threshold
            if session.key not in self._checked_initial_overflow:
                self._checked_initial_overflow.add(session.key)
                if estimated > self.active_window_tokens:
                    logger.warning(
                        "Conveyor belt: Initial context ({} tokens) exceeds active_window_tokens "
                        "({} tokens, {}% headroom). Consider increasing active_window_tokens or "
                        "reducing context_headroom. Processing will continue with degraded context.",
                        estimated,
                        self.active_window_tokens,
                        self.context_headroom,
                    )

            # Calculate thresholds
            # Step 1: triggers at (active_window_tokens - context_headroom%)
            # e.g., context_headroom=20 means Step 1 triggers at 80% of active_window_tokens
            step1_threshold = int(self.active_window_tokens * (100 - self.context_headroom) / 100)
            # Step 2: active_window_tokens + 5% - true last resort with lazy overage
            step2_threshold = int(self.active_window_tokens * 1.05)

            # Log current state for debugging
            logger.debug(
                "Conveyor belt check {}: estimated={}, thresholds: step1={} ({}%), step2={} (105%)",
                session.key,
                estimated,
                step1_threshold,
                100 - self.context_headroom,
                step2_threshold,
            )

            # Step 1: Soft clip tool blocks only (triggers at context_headroom%)
            if estimated > step1_threshold:
                logger.info(
                    "Conveyor belt Step1 {}: {}/{} (tool blocks)",
                    session.key,
                    estimated,
                    step1_threshold,
                )
                # Only archive tool call messages
                _archive_tool_blocks(session, self.sessions)

            # Re-estimate AFTER Step 1 - use NEW count for Step 2 check
            estimated, _ = self.estimate_session_prompt_tokens(session)

            # Step 2: Check with UPDATED estimated value (after Step 1 processed)
            if estimated > step2_threshold:
                logger.info(
                    "Conveyor belt Step2 {}: {}/{} (last resort at {}%)",
                    session.key,
                    estimated,
                    step2_threshold,
                    105,
                )
                # Archive everything remaining + hard clip
                # CRITICAL: Pass estimated value so it uses the SAME measurement as threshold check
                _archive_all_with_hardclip(session, self.sessions, self.active_window_tokens, estimated)
            # If under both thresholds, no archiving needed - done
