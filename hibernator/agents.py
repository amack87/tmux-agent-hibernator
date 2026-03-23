"""Agent definitions for supported AI coding tools.

Each agent defines how to discover its processes, detect its status in a tmux
pane, send a hibernation prompt, and restore a session.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AgentType(Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"


@dataclass(frozen=True)
class AgentDefinition:
    """Configuration for a supported AI agent."""

    agent_type: AgentType
    display_name: str

    # Process discovery: regex patterns matched against `ps -eo pid,tty,args`
    # At least one must match for a process to be considered this agent.
    process_patterns: tuple[str, ...]

    # Substrings in the ps line that disqualify a match (helpers, MCP servers, etc.)
    process_excludes: tuple[str, ...]

    # Pane content analysis — each is a list of lowercase substrings.
    working_indicators: tuple[str, ...]
    input_indicators: tuple[str, ...]
    idle_indicators: tuple[str, ...]

    # Regex that matches the agent's prompt character (e.g. ❯ for Claude)
    prompt_pattern: str

    # The text sent to the agent to request a context dump before hibernation.
    hibernation_prompt: str

    # Shell command template to restore a session.
    # {context_path} is replaced with the absolute path to the saved context file.
    restore_command: str

    # How to extract session IDs from the process args (optional regex).
    session_id_pattern: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

CLAUDE = AgentDefinition(
    agent_type=AgentType.CLAUDE,
    display_name="Claude Code",
    process_patterns=(
        r"(?:^|/|\s)claude(?:\s|$)",
    ),
    process_excludes=(
        "grep", "hibernator", "mcp", "--print", "knowledge-graph",
    ),
    working_indicators=(
        "esc to interrupt",
    ),
    input_indicators=(
        "esc to cancel",
    ),
    idle_indicators=(
        "accept edits on",
        "? for shortcuts",
    ),
    prompt_pattern=r"❯",
    hibernation_prompt=(
        "You are being hibernated to save resources. "
        "Output a detailed context summary so a future session can continue your work. "
        "Include: 1) Task description and goals, "
        "2) Files involved (full paths), "
        "3) Progress so far, "
        "4) Next steps remaining, "
        "5) Important decisions/context that would be lost, "
        "6) Git branch and uncommitted changes you know about, "
        "7) Working directory. "
        "Do NOT use any tools. Do NOT ask questions. Just output the context as plain text right now."
    ),
    restore_command="cat '{context_path}' | claude",
    session_id_pattern=r"(?:--session-id|--resume)\s+([a-f0-9-]+)",
)

CODEX = AgentDefinition(
    agent_type=AgentType.CODEX,
    display_name="Codex",
    process_patterns=(
        r"(?:^|/|\s)codex(?:\s|$)",
    ),
    process_excludes=(
        "grep", "hibernator",
    ),
    working_indicators=(
        "running",
        "executing",
        "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",  # spinner chars
    ),
    input_indicators=(
        "approve",
        "(y/n)",
        "[y/n]",
        "apply changes",
    ),
    idle_indicators=(
        "what can i help",
        "type a message",
    ),
    prompt_pattern=r"❯|>",
    hibernation_prompt=(
        "You are being hibernated to save resources. "
        "Output a detailed context summary so a future session can continue your work. "
        "Include: 1) Task description and goals, "
        "2) Files involved (full paths), "
        "3) Progress so far, "
        "4) Next steps remaining, "
        "5) Important decisions/context that would be lost, "
        "6) Git branch and uncommitted changes you know about, "
        "7) Working directory. "
        "Do NOT make any changes. Do NOT ask questions. Just output the context as plain text right now."
    ),
    restore_command="cat '{context_path}' | codex",
    session_id_pattern=None,
)

CURSOR = AgentDefinition(
    agent_type=AgentType.CURSOR,
    display_name="Cursor Agent",
    process_patterns=(
        r"(?:^|/|\s)cursor(?:\s|$)",
    ),
    process_excludes=(
        "grep", "hibernator", "cursor-helper", "cursor-gpu",
    ),
    working_indicators=(
        "generating",
        "thinking",
    ),
    input_indicators=(
        "accept",
        "reject",
        "(y/n)",
    ),
    idle_indicators=(
        "type a message",
        "ask anything",
    ),
    prompt_pattern=r"❯|>|\$",
    hibernation_prompt=(
        "You are being hibernated to save resources. "
        "Output a detailed context summary so a future session can continue your work. "
        "Include: 1) Task description and goals, "
        "2) Files involved (full paths), "
        "3) Progress so far, "
        "4) Next steps remaining, "
        "5) Important decisions/context that would be lost, "
        "6) Git branch and uncommitted changes you know about, "
        "7) Working directory. "
        "Do NOT make any changes. Do NOT ask questions. Just output the context as plain text right now."
    ),
    restore_command="cat '{context_path}' | cursor",
    session_id_pattern=None,
)

# Registry: all supported agents, keyed by AgentType
AGENTS: dict[AgentType, AgentDefinition] = {
    AgentType.CLAUDE: CLAUDE,
    AgentType.CODEX: CODEX,
    AgentType.CURSOR: CURSOR,
}

ALL_AGENTS: tuple[AgentDefinition, ...] = tuple(AGENTS.values())


def identify_agent(ps_line: str) -> Optional[AgentDefinition]:
    """Identify which agent (if any) a ps output line belongs to.

    Returns the AgentDefinition if the line matches an agent's process
    patterns and none of its excludes, or None otherwise.
    """
    lower = ps_line.lower()

    for agent in ALL_AGENTS:
        # Check excludes first (fast reject)
        if any(exc in lower for exc in agent.process_excludes):
            continue

        # Check if any process pattern matches
        for pattern in agent.process_patterns:
            if re.search(pattern, ps_line):
                return agent

    return None
