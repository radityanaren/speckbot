"""Session management for conversation history."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from speckbot.config.paths import get_legacy_sessions_dir
from speckbot.utils.helpers import ensure_dir, safe_filename, estimate_message_tokens


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in memory and archives overflow to JSONL.
    Conveyor belt uses recursive summarization - old messages are summarized
    and progressively compressed to maintain context fidelity.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_archived: int = 0  # Number of messages already archived
    summary_lines: list[str] = field(default_factory=list)  # Summary as list of lines
    _max_summary_lines: int = (
        99999  # Disabled compression - each turn block stays separate for readability
    )

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {"role": role, "content": content, "timestamp": datetime.now().isoformat(), **kwargs}
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def append_summary(self, new_summary: str) -> None:
        """Append new summary lines to accumulated summary with recursive compression."""
        if not new_summary:
            return

        # Add new lines
        new_lines = new_summary.strip().split("\n")
        self.summary_lines.extend(new_lines)

        # DISABLED: Recursive compression - each turn block stays separate for readability
        # while len(self.summary_lines) > self._max_summary_lines:
        #     self._compress_oldest_lines()

        self.updated_at = datetime.now()

    def _compress_oldest_lines(self) -> None:
        """Compress oldest 3 lines into one hybrid summary line."""
        if len(self.summary_lines) < 3:
            return

        # Take oldest 3 lines
        to_compress = self.summary_lines[:3]
        compressed = self._hybrid_compress(to_compress)

        # Replace oldest 3 with compressed single line
        self.summary_lines = [compressed] + self.summary_lines[3:]

    def _hybrid_compress(self, lines: list[str]) -> str:
        """Hybrid compression: template + keywords, output ~100 chars."""
        # Extract key information from lines
        parts: list[str] = []

        for line in lines:
            # Skip archive markers for compression
            if "[N messages archived" in line:
                continue
            # Skip block markers - each turn block stays separate
            if "[CONV:]" in line or "[TOOL:]" in line:
                continue

            # Extract role and key content
            if "USER:" in line:
                # Extract first 60 chars of user message
                import re

                match = re.search(r'USER:\s*"([^"]*)"', line)
                if match:
                    user_text = match.group(1)[:60]
                    parts.append(f"usr:{user_text}")
            elif "TOOL" in line:
                # Extract tool name
                import re

                match = re.search(r"TOOL\s+(\w+)", line)
                if match:
                    parts.append(f"tool:{match.group(1)}")
            elif "ASST:" in line:
                # Extract first 40 chars of assistant response
                import re

                match = re.search(r'ASST:\s*"([^"]*)"', line)
                if match:
                    asst_text = match.group(1)[:40]
                    parts.append(f"asst:{asst_text}")
            elif "*calls tools:" in line:
                # Extract tool names
                import re

                match = re.search(r"\*calls tools:\s*(.+)", line)
                if match:
                    parts.append(f"calls:{match.group(1)}")

        # Join with space, truncate to ~100 chars
        result = " ".join(parts)
        if len(result) > 100:
            result = result[:97] + "..."
        return f"[{len(lines)} msgs: {result}]"

    def get_context_summary(self) -> str:
        """Get the formatted context summary for system prompt."""
        if not self.summary_lines:
            return ""
        return f"<context-summary>\n" + "\n".join(self.summary_lines) + "\n</context-summary>"

    @staticmethod
    def _find_legal_start(messages: list[dict[str, Any]]) -> int:
        """Find first index where every tool result has a matching assistant tool_call."""
        declared: set[str] = set()
        start = 0
        for i, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    start = i + 1
                    declared.clear()
                    for prev in messages[start : i + 1]:
                        if prev.get("role") == "assistant":
                            for tc in prev.get("tool_calls") or []:
                                if isinstance(tc, dict) and tc.get("id"):
                                    declared.add(str(tc["id"]))
        return start

    def get_history(
        self, max_messages: int = 500, active_window_tokens: int = 0
    ) -> list[dict[str, Any]]:
        """Return messages for LLM input, aligned to a legal tool-call boundary.

        Includes HARD CLIPPING: drops oldest messages if over token limit.
        """
        # Read ALL messages from session (no last_archived filtering)
        all_msgs = self.messages[-max_messages:]

        # Drop leading non-user messages to avoid starting mid-turn when possible.
        for i, message in enumerate(all_msgs):
            if message.get("role") == "user":
                all_msgs = all_msgs[i:]
                break

        # Some providers reject orphan tool results if the matching assistant
        # tool_calls message fell outside the fixed-size history window.
        start = self._find_legal_start(all_msgs)
        if start:
            all_msgs = all_msgs[start:]

        out: list[dict[str, Any]] = []
        for message in all_msgs:
            # Filter out skip messages - bot shouldn't see answers that follow tool calls
            if message.get("_is_skip"):
                continue

            entry: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
            for key in ("tool_calls", "tool_call_id", "name"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        # HARD CLIPPING: Drop oldest messages until under limit
        if active_window_tokens > 0:
            while (
                len(out) > 1
                and sum(estimate_message_tokens(msg) for msg in out) > active_window_tokens
            ):
                out = out[1:]  # Drop oldest

        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_archived = 0
        self.summary_lines = []
        self.updated_at = datetime.now()

    @staticmethod
    def read_archive(
        archive_dir: Path, session_key: str, offset: int = 0, limit: int = 15
    ) -> list[dict[str, Any]]:
        """
        Read archived messages from JSONL file.

        Args:
            archive_dir: Directory containing archive files
            session_key: Session key to read
            offset: Number of entries to skip (pagination)
            limit: Maximum entries to return (default 15)

        Returns:
            List of archived messages (max 15)
        """
        archive_file = archive_dir / f"{safe_filename(session_key)}.jsonl"

        if not archive_file.exists():
            return []

        messages = []
        with open(archive_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # Apply pagination
        start = offset
        end = offset + limit
        return messages[start:end]


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    Archives are stored separately in the archive/ subdirectory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.archive_dir = ensure_dir(self.workspace / "archive")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.speckbot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            summary_lines = []
            metadata = {}
            created_at = None
            last_archived = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)
                    data_type = data.get("_type")

                    if data_type == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            datetime.fromisoformat(data["created_at"])
                            if data.get("created_at")
                            else None
                        )
                        last_archived = data.get("last_archived", 0)
                    elif data_type == "summary":
                        # Each summary line is separate entry
                        summary_lines.append(data.get("content", ""))
                    elif data.get("role"):
                        # Has role = message (old format)
                        data.pop("_archived_as", None)
                        messages.append(data)
                    elif data_type == "message":
                        messages.append(data)
                    else:
                        # Legacy format - treat as message
                        data.pop("_archived_as", None)
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_archived=last_archived,
                summary_lines=summary_lines,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk.

        Each summary line is saved as a separate JSON line (like messages).
        This ensures each visual line is a separate array element in JSON.
        """
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_archived": session.last_archived,
                "summary_lines_count": len(session.summary_lines),  # Store count for verification
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")

            # Save each summary line as SEPARATE JSON line (like messages)
            for line in session.summary_lines:
                f.write(json.dumps({"_type": "summary", "content": line}, ensure_ascii=False) + "\n")

            # Save messages
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def archive_session(self, session: Session) -> tuple[list[dict[str, Any]], str]:
        """
        Archive old messages to JSONL file for a session.

        Messages are REMOVED from session.messages after archiving to JSONL.
        JSONL is the source of truth for archived messages.

        Args:
            session: Session to archive messages from

        Returns:
            Tuple of (archived messages, archive note for summary)
        """
        # Archive messages from beginning up to last_archived index
        to_archive = session.messages[: session.last_archived]

        if not to_archive:
            return [], ""

        msg_count = len(to_archive)

        # Store last message role in metadata for skip detection on reload
        last_archived_msg = to_archive[-1]
        session.metadata["last_archived_role"] = last_archived_msg.get("role")

        # Create archive file path
        archive_file = self.archive_dir / f"{safe_filename(session.key)}.jsonl"

        # Append to archive file
        with open(archive_file, "a", encoding="utf-8") as f:
            for msg in to_archive:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        # REMOVE messages from session.messages (not just mark) - shallow copy keeps dict refs
        remaining = session.messages[session.last_archived :]
        # Clear _archived_as markers from remaining messages for next round
        for msg in remaining:
            msg.pop("_archived_as", None)
        session.messages = remaining
        session.last_archived = 0  # Reset marker since messages are removed

        # Create archive note for summary
        archive_note = f"[--:--] [{msg_count} messages archived - see archive for more]"
        return to_archive, archive_note

    def read_archive(
        self, session_key: str, offset: int = 0, limit: int = 15
    ) -> list[dict[str, Any]]:
        """
        Read archived messages from JSONL file.

        Args:
            session_key: Session key to read
            offset: Number of entries to skip (pagination)
            limit: Maximum entries to return (default 15)

        Returns:
            List of archived messages (max 15)
        """
        return Session.read_archive(self.archive_dir, session_key, offset, limit)

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append(
                                {
                                    "key": key,
                                    "created_at": data.get("created_at"),
                                    "updated_at": data.get("updated_at"),
                                    "path": str(path),
                                }
                            )
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
