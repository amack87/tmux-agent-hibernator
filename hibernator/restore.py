"""Restore workflow: recreate tmux session and resume agent."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hibernator.agents import AGENTS, AgentType, CLAUDE
from hibernator.db import get_session, mark_restored
from hibernator.tmux import new_session, send_keys

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestoreResult:
    success: bool
    session_name: str
    error: Optional[str] = None


def restore_session(
    session_id: Optional[int] = None,
    session_name: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> RestoreResult:
    """Restore a hibernated session.

    Creates a new tmux session, launches the original agent, and pipes the
    saved context file as the initial prompt.
    """
    kwargs = {"db_path": db_path} if db_path else {}
    record = get_session(session_id=session_id, session_name=session_name, **kwargs)

    if record is None:
        return RestoreResult(
            success=False,
            session_name=session_name or str(session_id),
            error="Session not found in database",
        )

    if record["status"] != "hibernated":
        return RestoreResult(
            success=False,
            session_name=record["session_name"],
            error=f"Session status is '{record['status']}', not 'hibernated'",
        )

    context_path = Path(record["context_file_path"])
    if not context_path.exists():
        return RestoreResult(
            success=False,
            session_name=record["session_name"],
            error=f"Context file missing: {context_path}",
        )

    name = record["session_name"]
    working_dir = record["working_directory"]

    # Determine which agent to use for restore
    agent_type_str = record.get("agent_type", "claude")
    try:
        agent_type = AgentType(agent_type_str)
    except ValueError:
        agent_type = AgentType.CLAUDE
    agent = AGENTS.get(agent_type, CLAUDE)

    # Create tmux session in the original working directory
    if not new_session(name, working_dir):
        return RestoreResult(
            success=False,
            session_name=name,
            error="Failed to create tmux session",
        )

    # Use the agent-specific restore command
    restore_cmd = agent.restore_command.format(context_path=context_path)
    send_keys(name, restore_cmd)

    # Mark as restored
    mark_restored(record["id"], **kwargs)

    log.info("Successfully restored %s session %s (id=%d)", agent.display_name, name, record["id"])
    return RestoreResult(success=True, session_name=name)
