"""Tmux subprocess wrappers."""

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
from typing import Optional

from hibernator.config import TMUX_SEARCH_PATHS


_tmux_path: Optional[str] = None


def find_tmux() -> str:
    """Resolve the tmux binary path."""
    global _tmux_path
    if _tmux_path is not None:
        return _tmux_path

    for path in TMUX_SEARCH_PATHS:
        if Path(path).exists():
            _tmux_path = path
            return path

    # Fall back to PATH lookup
    result = subprocess.run(["which", "tmux"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        _tmux_path = result.stdout.strip()
        return _tmux_path

    raise FileNotFoundError("tmux not found")


def run(*args: str, timeout: int = 10) -> Optional[str]:
    """Execute a tmux command and return stdout, or None on error."""
    try:
        result = subprocess.run(
            [find_tmux(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    session_name: str
    tty: str
    working_dir: str


def list_panes() -> list[PaneInfo]:
    """List all tmux panes with their metadata."""
    output = run(
        "list-panes", "-a",
        "-F", "#{pane_tty}\t#{pane_id}\t#{session_name}\t#{pane_current_path}",
    )
    if output is None:
        return []

    panes = []
    for line in output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            panes.append(PaneInfo(
                tty=parts[0],
                pane_id=parts[1],
                session_name=parts[2],
                working_dir=parts[3],
            ))
    return panes


def capture_pane(pane_id: str, lines: int = 20) -> Optional[str]:
    """Capture the last N lines of a tmux pane."""
    return run("capture-pane", "-t", pane_id, "-p", "-J", "-S", f"-{lines}")


def send_keys(target: str, text: str) -> bool:
    """Send keystrokes to a tmux pane."""
    result = run("send-keys", "-t", target, text, "Enter")
    return result is not None


def send_long_text(target: str, text: str) -> bool:
    """Send long text to a Claude Code session.

    Claude Code treats multi-line pastes as collapsed '[Pasted text]' blocks
    that require manual confirmation. To avoid this, we collapse the text
    to a single line and use send-keys, which Claude Code processes as
    normal typed input.
    """
    # Collapse to single line — Claude Code will still understand it
    single_line = " ".join(text.splitlines())

    # Use send-keys with the text + Enter
    # For very long text, tmux send-keys handles it fine (tested up to 64KB)
    result = run("send-keys", "-t", target, single_line, "Enter")
    return result is not None


def kill_session(session_name: str) -> bool:
    """Kill a tmux session by name."""
    result = run("kill-session", "-t", session_name)
    return result is not None


def new_session(name: str, working_dir: str) -> bool:
    """Create a new detached tmux session."""
    result = run("new-session", "-d", "-s", name, "-c", working_dir)
    return result is not None
