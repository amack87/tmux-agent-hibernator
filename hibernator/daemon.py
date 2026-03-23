"""Main monitoring daemon — run periodically via launchd."""

import fcntl
import logging
import sys

from hibernator.config import IDLE_THRESHOLD_MINUTES, PID_FILE, ensure_dirs
from hibernator.db import init_db
from hibernator.detect import IdleTracker, discover_sessions, SessionStatus
from hibernator.hibernate import hibernate_session
from hibernator.tmux import capture_pane

log = logging.getLogger(__name__)


def _acquire_lock() -> bool:
    """Ensure only one daemon instance runs at a time."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = open(PID_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(__import__("os").getpid()))
        lock_fd.flush()
        # Keep fd open to hold the lock
        _acquire_lock._fd = lock_fd
        return True
    except (IOError, OSError):
        return False


def run_check(threshold_minutes: int = IDLE_THRESHOLD_MINUTES) -> None:
    """Run one monitoring cycle."""
    ensure_dirs()
    init_db()

    tracker = IdleTracker()

    sessions = discover_sessions()
    if not sessions:
        log.debug("No active agent sessions found")
        tracker.cleanup(set())
        tracker.save()
        return

    active_pane_ids = {s.pane.pane_id for s in sessions}
    tracker.cleanup(active_pane_ids)

    for session in sessions:
        pane_id = session.pane.pane_id
        name = session.pane.session_name
        agent_name = session.process.agent.display_name

        # Get content for hash tracking
        content = capture_pane(pane_id) or ""

        # Update idle tracking
        idle_seconds = tracker.update(pane_id, content, session.status)

        if idle_seconds is not None and idle_seconds > 0:
            idle_minutes = idle_seconds / 60
            log.debug(
                "%s session %s idle for %.1f minutes (threshold: %d)",
                agent_name, name, idle_minutes, threshold_minutes,
            )

        # Check if eligible for hibernation
        if tracker.is_eligible(pane_id, threshold_minutes):
            idle_mins = (idle_seconds or 0) / 60
            log.info(
                "Hibernating %s session %s (idle for %.1f minutes)",
                agent_name, name, idle_mins,
            )

            result = hibernate_session(session)
            if result.success:
                tracker.remove(pane_id)
                log.info("Hibernated %s -> %s", name, result.context_path)
            else:
                log.error("Failed to hibernate %s: %s", name, result.error)

    tracker.save()


def main() -> None:
    """Entry point for daemon execution."""
    import argparse

    parser = argparse.ArgumentParser(description="Agent session hibernation daemon")
    parser.add_argument(
        "--threshold", type=int, default=IDLE_THRESHOLD_MINUTES,
        help=f"Idle threshold in minutes (default: {IDLE_THRESHOLD_MINUTES})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not _acquire_lock():
        log.warning("Another daemon instance is already running, exiting")
        sys.exit(0)

    run_check(threshold_minutes=args.threshold)


if __name__ == "__main__":
    main()
