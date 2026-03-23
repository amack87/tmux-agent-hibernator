"""SQLite database for session metadata."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hibernator.config import DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    tmux_pane_id TEXT,
    working_directory TEXT NOT NULL,
    context_file_path TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT 'claude',
    claude_session_uuid TEXT,
    original_command TEXT,
    project_path TEXT,
    status TEXT NOT NULL DEFAULT 'hibernated',
    hibernated_at TEXT NOT NULL,
    restored_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_name ON sessions(session_name);
"""

_MIGRATIONS = [
    # Add agent_type column if missing (upgrading from claude-only schema)
    "ALTER TABLE sessions ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'claude'",
]


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations for existing databases."""
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            # Column/table already exists — skip
            pass


def init_db(db_path: Optional[Path] = None) -> None:
    """Create tables if they don't exist and run migrations."""
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _run_migrations(conn)
    finally:
        conn.close()


def record_hibernation(
    session_name: str,
    pane_id: str,
    working_dir: str,
    context_path: str,
    agent_type: str = "claude",
    session_uuid: Optional[str] = None,
    command: Optional[str] = None,
    project_path: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Record a hibernated session. Returns the row ID."""
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO sessions
               (session_name, tmux_pane_id, working_directory, context_file_path,
                agent_type, claude_session_uuid, original_command, project_path,
                status, hibernated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'hibernated', ?)""",
            (
                session_name, pane_id, working_dir, context_path,
                agent_type, session_uuid, command, project_path,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def mark_restored(session_id: int, db_path: Optional[Path] = None) -> None:
    """Mark a session as restored."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE sessions SET status = 'restored', restored_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_expired(session_id: int, db_path: Optional[Path] = None) -> None:
    """Mark a session as expired."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE sessions SET status = 'expired' WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def list_hibernated(db_path: Optional[Path] = None) -> list[dict]:
    """List all hibernated sessions."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status = 'hibernated' ORDER BY hibernated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_all(db_path: Optional[Path] = None) -> list[dict]:
    """List all sessions."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY hibernated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_session(
    session_id: Optional[int] = None,
    session_name: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Look up a session by ID or most recent by name."""
    conn = _connect(db_path)
    try:
        if session_id is not None:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        elif session_name is not None:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_name = ? AND status = 'hibernated' "
                "ORDER BY hibernated_at DESC LIMIT 1",
                (session_name,),
            ).fetchone()
        else:
            return None
        return dict(row) if row else None
    finally:
        conn.close()
