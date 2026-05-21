"""Opencode session management — discovery, hibernation, and restore via CLI.

Opencode sessions run outside tmux. We discover them through `ps`, map them
to session IDs via the `-s` flag, export context via `opencode export`, and
restore via `opencode import`.
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hibernator.config import CONTEXT_DIR, ensure_dirs
from hibernator.db import record_hibernation

log = logging.getLogger(__name__)

# Regex to find opencode in ps output and extract session ID and port
OPENCODE_PS_RE = re.compile(
    r"(?:^|/|\s)(?:opencode)(?:\s|$)"
)
SESSION_FLAG_RE = re.compile(r"-s\s+(\S+)")
PORT_FLAG_RE = re.compile(r"--port\s+(\d+)")

# Known opencode binary locations
OPENCODE_CANDIDATES = [
    os.path.expanduser("~/.opencode/bin/opencode"),
    "/opt/homebrew/bin/opencode",
    "/usr/local/bin/opencode",
]


@dataclass(frozen=True)
class OpencodeProcess:
    pid: int
    session_id: Optional[str]
    port: Optional[int]
    command: str


@dataclass(frozen=True)
class OpencodeSession:
    session_id: str
    title: str
    directory: str
    agent: str
    version: str
    pid: Optional[int]
    port: Optional[int]
    updated: int
    created: int


@dataclass(frozen=True)
class OpencodeHibernationResult:
    success: bool
    session_id: str
    session_name: str
    context_path: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class OpencodeRestoreResult:
    success: bool
    session_name: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

def _find_opencode() -> Optional[str]:
    """Locate the opencode binary."""
    for path in OPENCODE_CANDIDATES:
        if Path(path).exists():
            return path
    found = shutil.which("opencode")
    return found


def _run_opencode(*args: str, timeout: int = 15, stdout_file: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run opencode CLI with proper PATH.

    For large output (e.g. export), pass *stdout_file* to write directly
    to a file instead of capturing in memory — avoids pipe buffer limits.
    """
    binary = _find_opencode()
    if not binary:
        raise FileNotFoundError("opencode binary not found")
    env = os.environ.copy()
    bin_dir = os.path.dirname(binary)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    if stdout_file:
        with open(stdout_file, "w") as f:
            proc = subprocess.run(
                [binary, *args],
                stdout=f, stderr=subprocess.PIPE, text=True,
                timeout=timeout, env=env,
            )
        # Read stderr back for error reporting
        proc.stdout = ""
        proc.stderr = proc.stderr or ""
        return proc

    return subprocess.run(
        [binary, *args],
        capture_output=True, text=True, timeout=timeout,
        env=env,
    )


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def discover_opencode_processes() -> list[OpencodeProcess]:
    """Find running opencode agent processes via ps.

    Excludes tmux wrapper processes that mention 'opencode' in their args
    (e.g. 'tmux attach-session -t opencode') — only the opencode binary
    itself represents an agent session.
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    processes = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if not OPENCODE_PS_RE.search(line):
            continue
        if "grep" in line.lower():
            continue
        # Exclude tmux wrapper processes that contain 'opencode' in their args
        if "tmux" in line.lower():
            continue

        parts = line.split(None, 1)
        if len(parts) < 2:
            continue

        try:
            pid = int(parts[0])
        except ValueError:
            continue

        command = parts[1]

        # Extract session ID from -s flag
        session_match = SESSION_FLAG_RE.search(command)
        session_id = session_match.group(1) if session_match else None

        # Extract port from --port flag
        port_match = PORT_FLAG_RE.search(command)
        port = int(port_match.group(1)) if port_match else None

        processes.append(OpencodeProcess(
            pid=pid,
            session_id=session_id,
            port=port,
            command=command,
        ))

    return processes


def discover_opencode_sessions() -> list[OpencodeSession]:
    """Discover running opencode sessions with full metadata.

    For each running opencode process, exports the session to get metadata.
    Processes without a session ID (fresh sessions) are still listed but
    without export data.

    Returns list of OpencodeSession from running processes.
    """
    processes = discover_opencode_processes()
    if not processes:
        return []

    sessions = []
    for proc in processes:
        if proc.session_id:
            info = _get_session_info(proc.session_id)
            if info:
                sessions.append(OpencodeSession(
                    session_id=proc.session_id,
                    title=info.get("title", proc.session_id),
                    directory=info.get("directory", ""),
                    agent=info.get("agent", "unknown"),
                    version=info.get("version", ""),
                    pid=proc.pid,
                    port=proc.port,
                    updated=info.get("time", {}).get("updated", 0),
                    created=info.get("time", {}).get("created", 0),
                ))
                continue

        # Still list the process even without export data
        sessions.append(OpencodeSession(
            session_id=proc.session_id or f"pid:{proc.pid}",
            title=f"opencode (pid {proc.pid})",
            directory="",
            agent="opencode",
            version="",
            pid=proc.pid,
            port=proc.port,
            updated=0,
            created=0,
        ))

    return sessions


def _get_session_info(session_id: str) -> Optional[dict]:
    """Export a session and extract just its info block.

    Writes to a temp file to avoid pipe buffer limits on large sessions.
    """
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            result = _run_opencode("export", session_id, "--sanitize", timeout=30, stdout_file=tmp_path)
            if result.returncode != 0:
                return None
            data = json.loads(Path(tmp_path).read_text())
            return data.get("info")
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


# ---------------------------------------------------------------------------
# Hibernation
# ---------------------------------------------------------------------------

def hibernate_opencode_session(
    session: OpencodeSession,
) -> OpencodeHibernationResult:
    """Hibernate an opencode session: export context, kill process.

    For sessions with a session ID: exports the session JSON to a context
    file, then sends SIGTERM to the process.
    For sessions without a session ID (fresh unnamed sessions): just
    sends SIGTERM and returns success.
    """
    ensure_dirs()

    context_path = None
    session_name = session.title or session.session_id

    if session.session_id and not session.session_id.startswith("pid:"):
        # Export session to get context — write directly to file to avoid pipe buffer limits
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name)
        context_path = str(CONTEXT_DIR / f"{safe_name}_{timestamp}.json")
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

        try:
            result = _run_opencode(
                "export", session.session_id, "--sanitize",
                timeout=30, stdout_file=context_path,
            )
            if result.returncode != 0:
                return OpencodeHibernationResult(
                    success=False,
                    session_id=session.session_id,
                    session_name=session_name,
                    error=f"Export failed: {result.stderr.strip() or 'unknown error'}",
                )

            # Read back for message count in logging
            data = json.loads(Path(context_path).read_text())

            # Record in database
            record_hibernation(
                session_name=session_name,
                pane_id=session.session_id,
                working_dir=session.directory or os.getcwd(),
                context_path=context_path,
                agent_type="opencode",
                session_uuid=session.session_id,
            )

            log.info(
                "Exported opencode session %s (%d messages) to %s",
                session.session_id,
                len(data.get("messages", [])),
                context_path,
            )
        except (json.JSONDecodeError, subprocess.TimeoutExpired,
                FileNotFoundError) as exc:
            return OpencodeHibernationResult(
                success=False,
                session_id=session.session_id,
                session_name=session_name,
                error=f"Export failed: {exc}",
            )

    # Kill the process
    if session.pid:
        try:
            os.kill(session.pid, signal.SIGTERM)
            log.info("Sent SIGTERM to opencode PID %d", session.pid)
            # Give it a moment, then SIGKILL if still alive
            time.sleep(2)
            try:
                os.kill(session.pid, 0)  # Check if still alive
                os.kill(session.pid, signal.SIGKILL)
                log.info("Sent SIGKILL to opencode PID %d", session.pid)
            except OSError:
                pass  # Process already dead
        except OSError as exc:
            return OpencodeHibernationResult(
                success=False,
                session_id=session.session_id,
                session_name=session_name,
                error=f"Failed to kill PID {session.pid}: {exc}",
            )

    return OpencodeHibernationResult(
        success=True,
        session_id=session.session_id,
        session_name=session_name,
        context_path=context_path,
    )


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_opencode_session(context_path: str) -> OpencodeRestoreResult:
    """Restore an opencode session from an exported JSON context file."""
    context_file = Path(context_path)
    if not context_file.exists():
        return OpencodeRestoreResult(
            success=False,
            session_name=context_path,
            error=f"Context file not found: {context_path}",
        )

    try:
        result = _run_opencode("import", str(context_file), timeout=30)
        if result.returncode != 0:
            return OpencodeRestoreResult(
                success=False,
                session_name=context_path,
                error=f"Import failed: {result.stderr.strip() or 'unknown error'}",
            )

        # Parse the session ID from import output
        session_name = context_path
        try:
            data = json.loads(result.stdout)
            session_name = data.get("info", {}).get("title", session_name)
        except (json.JSONDecodeError, TypeError):
            pass

        return OpencodeRestoreResult(
            success=True,
            session_name=session_name,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return OpencodeRestoreResult(
            success=False,
            session_name=context_path,
            error=str(exc),
        )
