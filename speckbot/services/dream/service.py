"""Dream: Memory index builder for SpeckBot.

On startup, Dream scans knowledges/projects and rebuilds MEMORY.md index.
The old compaction logic has moved to /flush and timer.py.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


class MemoryMap:
    """Represents current memory state."""

    def __init__(self):
        self.knowledges: dict[str, list[Path]] = {}
        self.projects: dict[str, list[Path]] = {}
        self.last_updated: dict[str, datetime] = {}


class DreamEngine:
    """
    Dream: Memory index builder for SpeckBot.

    Runs on startup to rebuild MEMORY.md index from knowledges/projects.
    Knowledges are scanned from workspace/knowledges/.
    Projects are scanned from projects_root/ via SPECKBOT.md markers.
    """

    def __init__(self, workspace: Path, config: dict[str, Any] | None = None):
        self.workspace = workspace
        self.config = config or {}

        # Directories
        self.knowledges_dir = workspace / "knowledges"
        projects_root_str = config.get("projects_root", "") if config else ""
        self.projects_root = Path(projects_root_str).expanduser() if projects_root_str else None
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

        # Scan projects via SPECKBOT.md markers in projects_root
        if self.projects_root and self.projects_root.exists():
            for sp_path in self.projects_root.rglob("SPECKBOT.md"):
                project_name = str(sp_path.parent.relative_to(self.projects_root))
                memory.projects[project_name] = [sp_path]
                memory.last_updated[project_name] = datetime.fromtimestamp(sp_path.stat().st_mtime)

        return memory

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
                date = memory.last_updated.get(topic)
                date_str = f" [[date:{date.strftime('%Y-%m-%d')}]]" if date else ""
                lines.append(f"- [[projects:{topic}]]{date_str}")
            lines.append("")

        self.memory_file.write_text("\n".join(lines), encoding="utf-8")

    # === Main entry point ===

    async def run(self) -> dict[str, Any]:
        """Run Dream: scan and rebuild MEMORY.md index."""
        if not self.enabled:
            return {"skipped": "disabled"}

        logger.info("Dream: scanning memory...")

        memory = self.scan()
        self.stabilize(memory)

        logger.info("Dream: MEMORY.md index updated")

        return {"status": "memory_index_updated"}


async def run_dream(workspace: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convenience function to run Dream."""
    engine = DreamEngine(workspace, config)
    return await engine.run()
