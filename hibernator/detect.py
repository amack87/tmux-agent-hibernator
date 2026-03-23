"""Session discovery, status detection, and idle tracking."""

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from hibernator.agents import AgentDefinition, AgentType, identify_agent
from hibernator.config import IDLE_STATE_PATH
from hibernator.tmux import PaneInfo, capture_pane, list_panes


class SessionStatus(Enum):
    WORKING = "working"
    NEEDS_INPUT = "needs_input"
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AgentProcess:
    pid: int
    tty: str
    session_uuid: Optional[str]
    command: str
    agent: AgentDefinition


@dataclass(frozen=True)
class AgentSession:
    process: AgentProcess
    pane: PaneInfo
    status: SessionStatus


def discover_agent_processes() -> list[AgentProcess]:
    """Find all running AI agent processes across all supported agents."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,tty,args"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    processes = []
    for line in result.stdout.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line:
            continue

        agent = identify_agent(line)
        if agent is None:
            continue

        parts = line.split(None, 2)
        if len(parts) < 3:
            continue

        pid = int(parts[0])
        tty = parts[1]
        command = parts[2]

        # Extract session UUID if the agent defines a pattern
        session_uuid = None
        if agent.session_id_pattern:
            uuid_match = re.search(agent.session_id_pattern, command)
            session_uuid = uuid_match.group(1) if uuid_match else None

        processes.append(AgentProcess(
            pid=pid,
            tty=tty,
            session_uuid=session_uuid,
            command=command,
            agent=agent,
        ))

    return processes


def map_processes_to_panes(
    processes: list[AgentProcess],
    panes: list[PaneInfo],
) -> list[AgentSession]:
    """Join processes to tmux panes on TTY."""
    # Build TTY -> pane lookup (strip /dev/ prefix from pane TTY)
    tty_to_pane: dict[str, PaneInfo] = {}
    for pane in panes:
        short_tty = pane.tty.replace("/dev/", "")
        tty_to_pane[short_tty] = pane

    sessions = []
    for proc in processes:
        pane = tty_to_pane.get(proc.tty)
        if pane is None:
            continue
        sessions.append(AgentSession(
            process=proc,
            pane=pane,
            status=SessionStatus.UNKNOWN,
        ))

    return sessions


def check_pane_status(pane_id: str, agent: AgentDefinition) -> SessionStatus:
    """Determine the status of an agent session by inspecting pane content."""
    content = capture_pane(pane_id, lines=20)
    if content is None:
        return SessionStatus.UNKNOWN

    lines = content.splitlines()
    if not lines:
        return SessionStatus.UNKNOWN

    # Strip empty trailing lines
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return SessionStatus.UNKNOWN

    # Pass 1: Check for active work indicators (highest priority)
    for line in lines:
        lower = line.lower().strip()
        if any(indicator in lower for indicator in agent.working_indicators):
            return SessionStatus.WORKING
        # Generic progress indicators
        if any(word in lower for word in (
            "running", "compiling", "building", "installing",
            "downloading", "fetching", "searching",
        )):
            if "%" in lower or "..." in lower:
                return SessionStatus.WORKING

    # Pass 2: Check for input-needed indicators
    for line in lines:
        lower = line.lower().strip()
        if any(indicator in lower for indicator in agent.input_indicators):
            return SessionStatus.NEEDS_INPUT
        if re.search(r'\(y/n\)|\[y/n\]|\(yes/no\)', lower):
            return SessionStatus.NEEDS_INPUT

    # Pass 3: Check for agent-specific idle indicators
    for line in lines:
        lower = line.strip().lower()
        if any(indicator in lower for indicator in agent.idle_indicators):
            return SessionStatus.IDLE

    # Pass 4: Check for prompt character
    has_prompt = False
    prompt_line_idx = -1
    for i, line in enumerate(lines):
        if re.search(agent.prompt_pattern, line):
            has_prompt = True
            prompt_line_idx = i

    if not has_prompt:
        # No prompt visible — likely working or scrolled
        return SessionStatus.WORKING

    # Has prompt — check if there's a question above it
    for line in reversed(lines[:prompt_line_idx]):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("?"):
            return SessionStatus.NEEDS_INPUT
        break

    return SessionStatus.IDLE


def discover_sessions() -> list[AgentSession]:
    """Discover all AI agent sessions with their current status."""
    processes = discover_agent_processes()
    if not processes:
        return []

    panes = list_panes()
    if not panes:
        return []

    sessions = map_processes_to_panes(processes, panes)

    # Enrich with status
    return [
        AgentSession(
            process=s.process,
            pane=s.pane,
            status=check_pane_status(s.pane.pane_id, s.process.agent),
        )
        for s in sessions
    ]


# ---------------------------------------------------------------------------
# Idle tracking (persists across daemon runs)
# ---------------------------------------------------------------------------

@dataclass
class IdleState:
    content_hash: str
    first_idle_at: float
    last_checked: float


class IdleTracker:
    """Track idle duration across daemon runs."""

    def __init__(self, state_path: Path = IDLE_STATE_PATH):
        self._state_path = state_path
        self._states: dict[str, IdleState] = {}
        self._load()

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            for pane_id, entry in data.items():
                self._states[pane_id] = IdleState(
                    content_hash=entry["content_hash"],
                    first_idle_at=entry["first_idle_at"],
                    last_checked=entry["last_checked"],
                )
        except (json.JSONDecodeError, KeyError, TypeError):
            self._states = {}

    def save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            pane_id: {
                "content_hash": s.content_hash,
                "first_idle_at": s.first_idle_at,
                "last_checked": s.last_checked,
            }
            for pane_id, s in self._states.items()
        }
        self._state_path.write_text(json.dumps(data, indent=2))

    def update(self, pane_id: str, content: str, status: SessionStatus) -> Optional[float]:
        """Update tracking for a pane. Returns seconds idle, or None if not idle."""
        now = time.time()

        if status != SessionStatus.IDLE:
            self._states.pop(pane_id, None)
            return None

        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        existing = self._states.get(pane_id)
        if existing is None or existing.content_hash != content_hash:
            # New idle or content changed — start fresh tracking
            self._states[pane_id] = IdleState(
                content_hash=content_hash,
                first_idle_at=now,
                last_checked=now,
            )
            return 0.0

        # Same content, still idle — update last_checked
        self._states[pane_id] = IdleState(
            content_hash=content_hash,
            first_idle_at=existing.first_idle_at,
            last_checked=now,
        )
        return now - existing.first_idle_at

    def is_eligible(self, pane_id: str, threshold_minutes: int) -> bool:
        state = self._states.get(pane_id)
        if state is None:
            return False
        elapsed = time.time() - state.first_idle_at
        return elapsed >= threshold_minutes * 60

    def remove(self, pane_id: str) -> None:
        self._states.pop(pane_id, None)

    def cleanup(self, active_pane_ids: set[str]) -> None:
        """Remove entries for panes that no longer exist."""
        stale = set(self._states.keys()) - active_pane_ids
        for pane_id in stale:
            del self._states[pane_id]
