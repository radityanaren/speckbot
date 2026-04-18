"""Memory tools for saving knowledge and projects."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from speckbot.session.manager import SessionManager


class SaveKnowledgeTool:
    """Tool for saving knowledge to workspace. Fetches from conversation memory."""

    def __init__(self, store: "MemoryStore", sessions: "SessionManager"):
        self._store = store
        self._sessions = sessions
        self._tool = _SaveKnowledgeToolImpl(store, sessions)

    @property
    def name(self) -> str:
        return "save_knowledge"

    @property
    def description(self) -> str:
        return (
            "Save knowledge to a topic folder. IMPORTANT: The content is automatically derived "
            "from the conversation history. Just provide topic and filename."
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
        """Execute save_knowledge tool - fetches conversation from session and saves."""
        try:
            return await self._tool.save_knowledge(topic, file_type, session_key)
        except Exception as e:
            logger.exception("Error in save_knowledge tool")
            return f"Error saving knowledge: {e}"


class _SaveKnowledgeToolImpl:
    """Internal implementation that fetches from session memory."""

    def __init__(self, store: "MemoryStore", sessions: "SessionManager"):
        self._store = store
        self._sessions = sessions

    async def save_knowledge(self, topic: str, file_type: str, session_key: str) -> str:
        """Save knowledge by fetching conversation from session."""
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
            elif role == "tool":
                tool_name = msg.get("name", "tool")
                display = content[:200] + "..." if len(content) > 200 else content
                lines.append(f"*[{tool_name} result]: {display}*")

        return "\n\n".join(lines)


class SaveProjectTool:
    """Tool for saving project info to workspace."""

    def __init__(self, store: "MemoryStore", sessions: "SessionManager"):
        self._store = store
        self._sessions = sessions
        self._tool = _SaveProjectToolImpl(store, sessions)

    @property
    def name(self) -> str:
        return "save_project"

    @property
    def description(self) -> str:
        return (
            "Save project info to a topic folder. IMPORTANT: The content is automatically derived "
            "from the conversation history. Just provide topic and filename."
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

    def __init__(self, store: "SessionManager", sessions: "MemoryStore"):
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


# Import helper for type hints
class MemoryStore:
    """Type stub for MemoryStore."""

    pass
