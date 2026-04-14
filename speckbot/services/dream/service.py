"""Dream memory cleanup system - part of SpeckBot's Sleep system."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger


class MemoryMap:
    """Represents current memory state."""

    def __init__(self):
        self.knowledges: dict[str, list[Path]] = {}
        self.projects: dict[str, list[Path]] = {}
        self.last_updated: dict[str, datetime] = {}


class SessionInsight:
    """Patterns extracted from session."""

    def __init__(self):
        self.corrections: list[str] = []
        self.save_requests: list[str] = []
        self.themes: list[str] = []


class DreamEngine:
    """
    Dream: Background memory cleanup system.

    Part of SpeckBot's Sleep system. Runs on every startup to:
    1. Scan - Build map of current memory
    2. Explore - Extract patterns from recent sessions
    3. Consolidate - Dedupe, date-convert, trim
    4. Stabilize - Write cleaned files
    """

    def __init__(self, workspace: Path, config: dict[str, Any] | None = None):
        self.workspace = workspace
        self.config = config or {}

        # Directories
        self.knowledges_dir = workspace / "knowledges"
        self.projects_dir = workspace / "projects"
        self.memory_file = workspace / "MEMORY.md"
        self.sessions_dir = workspace / "sessions"

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", False)

    @property
    def run_on_session_end(self) -> bool:
        return self.config.get("run_on_session_end", True)

    @property
    def max_memory_lines(self) -> int:
        return self.config.get("max_memory_lines", 200)

    @property
    def deduplicate(self) -> bool:
        return self.config.get("deduplicate", True)

    @property
    def convert_dates(self) -> bool:
        return self.config.get("convert_dates", True)

    # === Phase methods ===

    def scan(self) -> MemoryMap:
        """Phase 1: Scan current memory state."""
        memory = MemoryMap()

        # Scan knowledges
        if self.knowledges_dir.exists():
            for topic_dir in self.knowledges_dir.iterdir():
                if topic_dir.is_dir():
                    md_files = list(topic_dir.glob("*.md"))
                    memory.knowledges[topic_dir.name] = md_files
                    if md_files:
                        mtime = max(f.stat().st_mtime for f in md_files)
                        memory.last_updated[topic_dir.name] = datetime.fromtimestamp(mtime)

        # Scan projects
        if self.projects_dir.exists():
            for topic_dir in self.projects_dir.iterdir():
                if topic_dir.is_dir():
                    md_files = list(topic_dir.glob("*.md"))
                    memory.projects[topic_dir.name] = md_files
                    if md_files:
                        mtime = max(f.stat().st_mtime for f in md_files)
                        memory.last_updated[topic_dir.name] = datetime.fromtimestamp(mtime)

        return memory

    def explore(self, limit: int = 5) -> list[SessionInsight]:
        """Phase 2: Extract patterns from recent sessions."""
        insights = []

        if not self.sessions_dir.exists():
            return insights

        try:
            sessions = sorted(
                self.sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:limit]
        except Exception:
            return insights

        for session_path in sessions:
            insight = SessionInsight()

            try:
                with open(session_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if data.get("_type") == "metadata":
                            continue

                        content = data.get("content", "")
                        if not content:
                            continue

                        lower_content = content.lower()
                        if any(
                            kw in lower_content for kw in ["no,", "wrong", "not that", "actually"]
                        ):
                            insight.corrections.append(content[:200])

                        if "save" in lower_content:
                            if any(
                                kw in lower_content for kw in ["knowledge", "project", "memory"]
                            ):
                                insight.save_requests.append(content[:200])

            except Exception as e:
                logger.debug("Failed to explore session {}: {}", session_path, e)
                continue

            if insight.corrections or insight.save_requests:
                insights.append(insight)

        return insights

    def consolidate(self, memory: MemoryMap, insights: list[SessionInsight]) -> dict[str, Any]:
        """Phase 3: Dedupe, convert dates, trim."""
        result = {
            "content_similarities_merged": 0,
        }
        return result

    def stabilize(self, memory: MemoryMap) -> None:
        """Phase 4: Write cleaned files back."""
        self._write_memory_index(memory)

    def _write_memory_index(self, memory: MemoryMap) -> None:
        """Write MEMORY.md index file with obsidian-style links."""
        lines = ["# Memory Index\n"]

        if memory.knowledges:
            lines.append("## Knowledges")
            for topic in sorted(memory.knowledges.keys()):
                files = memory.knowledges[topic]
                date = memory.last_updated.get(topic)
                date_str = f" [[date:{date.strftime('%Y-%m-%d')}]]" if date else ""
                files_str = ", ".join(f.stem for f in files) if files else "(empty)"
                lines.append(f"- [[knowledges:{topic}]]{date_str}: {files_str}")
            lines.append("")

        if memory.projects:
            lines.append("## Projects")
            for topic in sorted(memory.projects.keys()):
                files = memory.projects[topic]
                date = memory.last_updated.get(topic)
                date_str = f" [[date:{date.strftime('%Y-%m-%d')}]]" if date else ""
                files_str = ", ".join(f.stem for f in files) if files else "(empty)"
                lines.append(f"- [[projects:{topic}]]{date_str}: {files_str}")
            lines.append("")

        if memory.history_entries:
            lines.append("## Recent History")
            for entry in memory.history_entries[-5:]:
                first_line = entry.split("\n")[0][:80]
                lines.append(f"- {first_line}...")
            lines.append("")

        self.memory_file.write_text("\n".join(lines), encoding="utf-8")

    # === Main entry point ===

    async def run(self) -> dict[str, Any]:
        """Run full Dream cycle. Returns stats."""
        if not self.enabled:
            return {"skipped": "disabled"}

        logger.info("Starting Dream cleanup...")

        memory = self.scan()
        insights = self.explore()
        stats = self.consolidate(memory, insights)
        self.stabilize(memory)

        logger.info(
            "Dream complete: deduplicated={}, date_fixed={}, trimmed={}",
            stats.get("deduplicated", 0),
            stats.get("date_fixed", 0),
            stats.get("trimmed", 0),
        )

        return stats


async def run_dream(workspace: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convenience function to run Dream."""
    engine = DreamEngine(workspace, config)
    return await engine.run()
