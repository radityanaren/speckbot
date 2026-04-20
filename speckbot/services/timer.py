"""Unified timer service - coordinates heartbeat, monologue, and dream sleep timing."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from speckbot.services.heartbeat.service import HeartbeatService
    from speckbot.services.monologue.service import MonologueSystem
    from speckbot.services.dream.service import DreamEngine


def _now_ms() -> int:
    return int(time.time() * 1000)


class UnifiedTimer:
    """
    Unified timer that coordinates all service timing.

    Runs a single 1-second tick loop that:
    - Tracks heartbeat interval and triggers when due
    - Tracks monologue idle time and triggers when idle
    - Tracks dream sleep interval and triggers restart when due
    """

    def __init__(
        self,
        workspace: Path,
        config: dict | None = None,
        heartbeat_service: "HeartbeatService | None" = None,
        monologue_service: "MonologueSystem | None" = None,
        dream_service: "DreamEngine | None" = None,
    ):
        self.workspace = workspace
        self.config = config or {}

        # Services
        self._heartbeat = heartbeat_service
        self._monologue = monologue_service
        self._dream = dream_service

        # Config - heartbeat (support both snake_case and camelCase)
        hb_config = config.get("heartbeat", {}) if config else {}
        self._heartbeat_enabled = hb_config.get("enabled") or hb_config.get(
            "heartbeatEnabled", True
        )
        self._heartbeat_interval = hb_config.get("intervalSeconds") or hb_config.get(
            "interval_seconds", 1800
        )

        # Config - monologue (support both snake_case and camelCase)
        mono_config = config.get("monologue", {}) if config else {}
        self._monologue_enabled = mono_config.get("enabled") or mono_config.get(
            "monologueEnabled", False
        )
        self._monologue_idle = mono_config.get("idleSeconds") or mono_config.get(
            "idle_seconds", 300
        )

        # Config - dream (sleep timer, support both snake_case and camelCase)
        dream_config = config.get("dream", {}) if config else {}
        self._dream_enabled = dream_config.get("enabled") or dream_config.get("dreamEnabled", False)
        self._dream_sleep_hours = dream_config.get("sleepIntervalHours") or dream_config.get(
            "sleep_interval_hours", 24
        )

        # State
        self._running = False
        self._task: asyncio.Task | None = None

        # Counters (in seconds)
        self._heartbeat_counter = 0
        self._monologue_counter = 0
        self._dream_sleep_counter = 0

        logger.info(
            "UnifiedTimer: heartbeat={}s, monologue={}s (enabled={}), dream_sleep={}h",
            self._heartbeat_interval,
            self._monologue_idle,
            self._monologue_enabled,
            self._dream_sleep_hours,
        )

    def set_heartbeat_service(self, service: "HeartbeatService") -> None:
        """Set the heartbeat service."""
        self._heartbeat = service

    def set_monologue_service(self, service: "MonologueSystem") -> None:
        """Set the monologue service."""
        self._monologue = service

    def set_dream_service(self, service: "DreamEngine") -> None:
        """Set the dream service."""
        self._dream = service

    async def start(self) -> None:
        """Start the unified timer."""
        if self._running:
            logger.warning("UnifiedTimer already running")
            return

        self._running = True

        # Run Dream on startup if enabled
        if self._dream_enabled and self._dream:
            logger.info("UnifiedTimer: running Dream on startup...")
            try:
                await self._dream.run()
                logger.info("UnifiedTimer: Dream completed")
            except Exception:
                logger.exception("UnifiedTimer: Dream startup failed")

        self._task = asyncio.create_task(self._run_loop())
        logger.info("UnifiedTimer started")

    def stop(self) -> None:
        """Stop the unified timer."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("UnifiedTimer stopped")

    async def _run_loop(self) -> None:
        """Main timer loop - ticks every 1 second."""
        while self._running:
            try:
                await asyncio.sleep(1)
                if self._running:
                    self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("UnifiedTimer error: {}", e)

    def _tick(self) -> None:
        """Handle each tick - check if services need to run."""
        # Heartbeat tick
        if self._heartbeat_enabled and self._heartbeat:
            self._heartbeat_counter += 1
            if self._heartbeat_counter >= self._heartbeat_interval:
                self._heartbeat_counter = 0
                asyncio.create_task(self._heartbeat_tick())

        # Monologue tick (only if enabled and monologue service exists)
        if self._monologue_enabled and self._monologue:
            self._monologue_counter += 1
            if self._monologue_counter >= self._monologue_idle:
                self._monologue_counter = 0
                asyncio.create_task(self._monologue_tick())

        # Dream sleep tick (auto-restart)
        if self._dream_enabled and self._dream_sleep_hours > 0:
            self._dream_sleep_counter += 1
            sleep_seconds = self._dream_sleep_hours * 3600
            if self._dream_sleep_counter >= sleep_seconds:
                self._dream_sleep_counter = 0
                logger.info("UnifiedTimer: Dream sleep timer triggered, restarting...")
                asyncio.create_task(self._trigger_restart())

    async def _heartbeat_tick(self) -> None:
        """Trigger heartbeat tick."""
        if self._heartbeat:
            try:
                await self._heartbeat._tick()
            except Exception:
                logger.exception("Heartbeat tick failed")

    async def _monologue_tick(self) -> None:
        """Trigger monologue idle."""
        if self._monologue and self._monologue._process_callback:
            try:
                await self._monologue.handle_idle(self._monologue._process_callback)
            except Exception:
                logger.exception("Monologue tick failed")
            finally:
                if self._monologue_enabled:
                    self._monologue_counter = 0
        elif self._monologue:
            self._monologue_counter = 0

    async def _trigger_restart(self) -> None:
        """Trigger process restart after running flush (for Dream)."""
        import os
        from speckbot.session.manager import SessionManager
        from speckbot.utils.helpers import safe_filename, estimate_message_tokens
        import json
        import re

        # If Dream enabled, run flush first before restart
        if self._dream_enabled and self._dream:
            logger.info("Dream: running flush compaction before restart...")

            # Create session manager to access sessions
            sessions_dir = self.workspace / "sessions"
            archive_dir = self.workspace / "archive"

            if sessions_dir.exists():
                # Get all session files
                session_files = list(sessions_dir.glob("*.jsonl"))

                for session_file in session_files:
                    try:
                        session_key = session_file.stem

                        # Load session
                        from speckbot.session.manager import SessionManager
                        sm = SessionManager(self.workspace)
                        session = sm.get_or_create(session_key)

                        if not session.messages and not session.summary_lines:
                            continue

                        # Build timeline (same logic as /flush)
                        timeline = []

                        for m in session.messages:
                            ts = m.get("timestamp", "")
                            if "T" in ts:
                                ts = ts.split("T")[1][:5]
                            elif not ts:
                                ts = "00:00"
                            timeline.append((ts, "message", m))

                        for line in session.summary_lines:
                            match = re.search(r"\[(\d{2}:\d{2})\]", line)
                            ts = match.group(1) if match else "00:00"
                            timeline.append((ts, "summary", line))

                        if not timeline:
                            continue

                        timeline.sort(key=lambda x: x[0])

                        # 90%/10% split
                        total = len(timeline)
                        split_idx = int(total * 0.90)

                        oldest_90 = timeline[:split_idx] if split_idx > 0 else []
                        newest_10 = timeline[split_idx:] if split_idx < total else timeline

                        if not oldest_90:
                            continue

                        # Archive oldest 90% to JSONL
                        oldest_messages = [item[2] for item in oldest_90 if item[1] == "message"]

                        archive_file = archive_dir / f"{safe_filename(session_key)}.jsonl"
                        archive_file.parent.mkdir(parents=True, exist_ok=True)

                        with open(archive_file, "a", encoding="utf-8") as f:
                            for m in oldest_messages:
                                f.write(json.dumps(m, ensure_ascii=False) + "\n")

                        # Rebuild session: keep newest 10% messages only
                        session.messages = [item[2] for item in newest_10 if item[1] == "message"]
                        session.summary_lines = [item[2] for item in newest_10 if item[1] == "summary"]

                        sm.save(session)

                        logger.info("Dream flush: session {} compacted", session_key)

                    except Exception as e:
                        logger.warning("Dream flush failed for {}: {}", session_file, e)
                        continue

            logger.info("Dream: flush done, now restarting speckbot...")

        # Then restart
        await asyncio.sleep(0.3)
        os.execv(sys.executable, [sys.executable, "-m", "speckbot", "gateway"])

    def on_user_message(self) -> None:
        """Called when user sends a message - reset monologue counter."""
        self._monologue_counter = 0
        logger.debug("UnifiedTimer: reset monologue counter on user message")

    def reset_monologue_timer(self) -> None:
        """Reset the monologue idle timer (alias for on_user_message)."""
        self.on_user_message()
