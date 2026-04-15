"""Session management for conversation history."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from speckbot.config.paths import get_legacy_sessions_dir
from speckbot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in memory and archives overflow to JSONL.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_archived: int = 0  # Number of messages already archived
    summary_so_far: str = ""  # Accumulated context summary for conveyor belt

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {"role": role, "content": content, "timestamp": datetime.now().isoformat(), **kwargs}
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def append_summary(self, new_summary: str) -> None:
        """Append new summary segment to accumulated summary."""
        if new_summary:
            if self.summary_so_far:
                self.summary_so_far += "\n" + new_summary
            else:
                self.summary_so_far = new_summary
        self.updated_at = datetime.now()

    def get_context_summary(self) -> str:
        """Get the formatted context summary for system prompt."""
        if not self.summary_so_far:
            return ""
        return f"<context-summary>\n{self.summary_so_far}\n</context-summary>"

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

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unarchived messages for LLM input, aligned to a legal tool-call boundary."""
        unarchived = self.messages[self.last_archived :]
        sliced = unarchived[-max_messages:]

        # Drop leading non-user messages to avoid starting mid-turn when possible.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                sliced = sliced[i:]
                break

        # Some providers reject orphan tool results if the matching assistant
        # tool_calls message fell outside the fixed-size history window.
        start = self._find_legal_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
            for key in ("tool_calls", "tool_call_id", "name"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_archived = 0
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
            metadata = {}
            created_at = None
            last_archived = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            datetime.fromisoformat(data["created_at"])
                            if data.get("created_at")
                            else None
                        )
                        last_archived = data.get("last_archived", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_archived=last_archived,
                summary_so_far=data.get("summary_so_far", ""),
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_archived": session.last_archived,
                "summary_so_far": session.summary_so_far,
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def archive_session(self, session: Session) -> tuple[list[dict[str, Any]], str]:
        """
        Archive old messages to JSONL file for a session.

        Args:
            session: Session to archive messages from

        Returns:
            Tuple of (archived messages, archive note for summary)
        """
        unarchived = session.messages[session.last_archived :]
        if not unarchived:
            return [], ""

        msg_count = len(unarchived)

        # Create archive file path
        archive_file = self.archive_dir / f"{safe_filename(session.key)}.jsonl"

        # Append to archive file
        with open(archive_file, "a", encoding="utf-8") as f:
            for msg in unarchived:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        # Update marker
        session.last_archived = len(session.messages)

        # Create archive note for summary
        archive_note = f"[--:--] [{msg_count} messages archived - see archive for more]"
        return unarchived, archive_note

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
