"""Hibernation workflow: capture context, record, terminate."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hibernator.config import (
    CONTEXT_DIR,
    CONTEXT_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    CONTEXT_STABLE_CHECKS,
    ensure_dirs,
)
from hibernator.db import record_hibernation
from hibernator.detect import AgentSession, SessionStatus, check_pane_status
from hibernator.tmux import capture_pane, kill_session, send_long_text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HibernationResult:
    success: bool
    session_name: str
    context_path: Optional[str] = None
    error: Optional[str] = None


def _generate_context_path(session_name: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name)
    return CONTEXT_DIR / f"{safe_name}_{timestamp}.md"


def _wait_for_response(
    pane_id: str,
    agent,
    timeout: int,
    poll_interval: int,
) -> Optional[str]:
    """Wait for the agent to finish responding, then capture the pane output.

    Detects completion by watching for the pane to return to idle status.
    """
    deadline = time.time() + timeout
    # Give the agent a moment to start processing
    time.sleep(5)

    last_content = ""
    stable_count = 0

    while time.time() < deadline:
        time.sleep(poll_interval)

        content = capture_pane(pane_id, lines=200) or ""

        # Check if agent has finished (returned to idle prompt)
        status = check_pane_status(pane_id, agent)
        if status == SessionStatus.IDLE:
            return content

        # Also check for stability (content stopped changing)
        if content == last_content and content:
            stable_count += 1
            if stable_count >= (30 // poll_interval) and len(content) > 200:
                return content
        else:
            stable_count = 0
            last_content = content

    # Timeout — return whatever we have
    return capture_pane(pane_id, lines=200)


def _extract_context(pane_content: str, prompt_text: str, agent) -> str:
    """Extract the agent's response from the captured pane content.

    Removes the prompt we sent and any trailing prompt characters,
    keeping just the agent's context output.
    """
    lines = pane_content.splitlines()

    # Find where the response starts (after our hibernation prompt)
    response_start = 0
    for i, line in enumerate(lines):
        if "being hibernated" in line.lower() or "output a detailed" in line.lower():
            response_start = i + 1
            break

    # Find where the response ends (before the prompt reappears)
    response_end = len(lines)
    import re
    for i in range(len(lines) - 1, response_start, -1):
        line = lines[i].strip()
        if re.search(agent.prompt_pattern, line):
            response_end = i
        elif any(ind in line.lower() for ind in agent.idle_indicators):
            response_end = i
        elif line:
            break

    context_lines = lines[response_start:response_end]
    # Strip leading/trailing empty lines
    while context_lines and not context_lines[0].strip():
        context_lines.pop(0)
    while context_lines and not context_lines[-1].strip():
        context_lines.pop()

    return "\n".join(context_lines)


def hibernate_session(
    session: AgentSession,
    db_path: Optional[Path] = None,
) -> HibernationResult:
    """Execute the full hibernation workflow for a session."""
    ensure_dirs()
    name = session.pane.session_name
    agent = session.process.agent

    # Double-check status before proceeding
    current_status = check_pane_status(session.pane.pane_id, agent)
    if current_status != SessionStatus.IDLE:
        return HibernationResult(
            success=False,
            session_name=name,
            error=f"Session status changed to {current_status.value}, aborting",
        )

    # Build and send the agent-specific hibernation prompt
    prompt = agent.hibernation_prompt
    log.info("Sending hibernation prompt to %s session %s", agent.display_name, name)

    if not send_long_text(session.pane.pane_id, prompt):
        return HibernationResult(
            success=False,
            session_name=name,
            error="Failed to send hibernation prompt via tmux",
        )

    # Wait for agent to respond
    log.info("Waiting for %s to output context...", agent.display_name)
    pane_content = _wait_for_response(
        session.pane.pane_id,
        agent,
        CONTEXT_TIMEOUT_SECONDS,
        POLL_INTERVAL_SECONDS,
    )

    if not pane_content:
        return HibernationResult(
            success=False,
            session_name=name,
            error=f"No response captured from {agent.display_name}",
        )

    # Extract the context from pane output
    context = _extract_context(pane_content, prompt, agent)

    if len(context) < 50:
        return HibernationResult(
            success=False,
            session_name=name,
            error=f"Context too short ({len(context)} chars), likely incomplete",
        )

    # Write context to file
    context_path = _generate_context_path(name)
    context_path.write_text(f"# Hibernation Context: {name}\n\n{context}\n")

    log.info("Saved context (%d chars) to %s", len(context), context_path)

    # Record in database
    kwargs = {"db_path": db_path} if db_path else {}
    row_id = record_hibernation(
        session_name=name,
        pane_id=session.pane.pane_id,
        working_dir=session.pane.working_dir,
        context_path=str(context_path),
        agent_type=agent.agent_type.value,
        session_uuid=session.process.session_uuid,
        command=session.process.command,
        **kwargs,
    )
    log.info("Recorded hibernation id=%d for session %s", row_id, name)

    # Kill the tmux session
    if not kill_session(name):
        log.warning("Failed to kill tmux session %s (may have already exited)", name)

    log.info("Successfully hibernated %s session %s", agent.display_name, name)
    return HibernationResult(
        success=True,
        session_name=name,
        context_path=str(context_path),
    )
