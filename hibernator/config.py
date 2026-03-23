"""Configuration constants and paths."""

from dataclasses import dataclass
from pathlib import Path
import os
import shutil


NEW_BASE_DIR = Path.home() / ".tmux-agent-hibernator"
OLD_BASE_DIR = Path.home() / ".claude-hibernator"


def _resolve_base_dir() -> Path:
    """Resolve the data directory, migrating from the old name if needed."""
    if NEW_BASE_DIR.exists():
        return NEW_BASE_DIR
    if OLD_BASE_DIR.exists():
        # Migrate: rename old dir to new name
        shutil.move(str(OLD_BASE_DIR), str(NEW_BASE_DIR))
        return NEW_BASE_DIR
    return NEW_BASE_DIR


BASE_DIR = _resolve_base_dir()
CONTEXT_DIR = BASE_DIR / "contexts"
DB_PATH = BASE_DIR / "hibernator.db"
IDLE_STATE_PATH = BASE_DIR / "idle_state.json"
PID_FILE = BASE_DIR / "daemon.pid"
LOG_PATH = BASE_DIR / "daemon.log"

IDLE_THRESHOLD_MINUTES = int(os.environ.get("HIBERNATOR_IDLE_MINUTES", "720"))
POLL_INTERVAL_SECONDS = 5
CONTEXT_TIMEOUT_SECONDS = 180
CONTEXT_STABLE_CHECKS = 2

TMUX_SEARCH_PATHS = [
    "/opt/homebrew/bin/tmux",
    "/usr/local/bin/tmux",
    "/usr/bin/tmux",
]


@dataclass(frozen=True)
class HibernatorConfig:
    base_dir: Path = BASE_DIR
    context_dir: Path = CONTEXT_DIR
    db_path: Path = DB_PATH
    idle_state_path: Path = IDLE_STATE_PATH
    idle_threshold_minutes: int = IDLE_THRESHOLD_MINUTES
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS
    context_timeout_seconds: int = CONTEXT_TIMEOUT_SECONDS


def ensure_dirs() -> None:
    """Create required directories if they don't exist."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
