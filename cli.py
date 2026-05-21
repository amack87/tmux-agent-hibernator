#!/usr/bin/env python3
"""CLI for tmux-agent-hibernator."""

import argparse
import logging
import sys
from pathlib import Path

from hibernator.agents import ALL_AGENTS, AgentType
from hibernator.config import IDLE_THRESHOLD_MINUTES, ensure_dirs
from hibernator.db import get_session, init_db, list_all, list_hibernated
from hibernator.daemon import run_check
from hibernator.detect import discover_sessions, IdleTracker, SessionStatus, check_pane_status
from hibernator.hibernate import hibernate_session
from hibernator.opencode import (
    discover_opencode_sessions,
    hibernate_opencode_session,
    restore_opencode_session,
)
from hibernator.restore import restore_session
from hibernator.tmux import capture_pane


def cmd_status(_args: argparse.Namespace) -> None:
    """Show all running agent sessions (tmux + opencode) with idle time."""
    tmux_sessions = discover_sessions()
    opencode_sessions = discover_opencode_sessions()

    if not tmux_sessions and not opencode_sessions:
        print("No active agent sessions found.")
        return

    tracker = IdleTracker()

    print(f"{'Session':<30} {'Agent':<15} {'Status':<15} {'Idle':<12} {'Working Dir'}")
    print("-" * 100)

    for s in tmux_sessions:
        content = capture_pane(s.pane.pane_id) or ""
        idle_secs = tracker.update(s.pane.pane_id, content, s.status)

        if idle_secs is not None and idle_secs > 0:
            mins = idle_secs / 60
            idle_str = f"{mins:.0f}m" if mins >= 1 else f"{idle_secs:.0f}s"
        else:
            idle_str = "-"

        agent_name = s.process.agent.display_name
        print(f"{s.pane.session_name:<30} {agent_name:<15} {s.status.value:<15} {idle_str:<12} {s.pane.working_dir}")

    for s in opencode_sessions:
        print(f"{s.title:<30} {'opencode':<15} {'running':<15} {'-':<12} {s.directory}")

    tracker.save()


def cmd_list(args: argparse.Namespace) -> None:
    """List hibernated sessions."""
    init_db()
    sessions = list_hibernated()

    if getattr(args, "json", False):
        import json
        print(json.dumps(sessions, indent=2))
        return

    if not sessions:
        print("No hibernated sessions.")
        return

    print(f"{'ID':<5} {'Session':<25} {'Agent':<15} {'Hibernated At':<22} {'Working Dir'}")
    print("-" * 95)
    for s in sessions:
        ts = s["hibernated_at"][:19].replace("T", " ")
        agent = s.get("agent_type", "claude")
        print(f"{s['id']:<5} {s['session_name']:<25} {agent:<15} {ts:<22} {s['working_directory']}")


def cmd_history(_args: argparse.Namespace) -> None:
    """Show all sessions including restored."""
    init_db()
    sessions = list_all()
    if not sessions:
        print("No session history.")
        return

    print(f"{'ID':<5} {'Session':<20} {'Agent':<12} {'Status':<12} {'Hibernated':<22} {'Restored'}")
    print("-" * 100)
    for s in sessions:
        hib_ts = s["hibernated_at"][:19].replace("T", " ")
        res_ts = (s["restored_at"] or "")[:19].replace("T", " ") if s["restored_at"] else "-"
        agent = s.get("agent_type", "claude")
        print(f"{s['id']:<5} {s['session_name']:<20} {agent:<12} {s['status']:<12} {hib_ts:<22} {res_ts}")


def cmd_agents(_args: argparse.Namespace) -> None:
    """List all supported agents."""
    print("Supported agents:\n")
    for agent in ALL_AGENTS:
        if agent.agent_type == AgentType.OPENCODE:
            print(f"  {agent.display_name:<20} slug: {agent.agent_type.value}")
            print(f"  {'':20} discovered via: opencode CLI (not tmux)")
            print(f"  {'':20} restore via: opencode import")
            print()
        else:
            patterns = ", ".join(agent.process_patterns)
            print(f"  {agent.display_name:<20} slug: {agent.agent_type.value}")
            print(f"  {'':20} process: {patterns}")
            print(f"  {'':20} restore: {agent.restore_command}")
            print()


def cmd_hibernate(args: argparse.Namespace) -> None:
    """Manually hibernate a specific session (tmux or opencode)."""
    target = args.session_name

    # Check tmux sessions
    tmux_sessions = discover_sessions()
    match = None
    for s in tmux_sessions:
        if s.pane.session_name == target:
            match = ("tmux", s)
            break

    # Check opencode sessions
    if match is None:
        opencode_sessions = discover_opencode_sessions()
        for s in opencode_sessions:
            if s.title == target or s.session_id == target:
                match = ("opencode", s)
                break

    if match is None:
        print(f"No active agent session found with name '{target}'.")
        all_sessions = []
        for s in tmux_sessions:
            all_sessions.append(f"  - {s.pane.session_name} ({s.process.agent.display_name}, {s.status.value})")
        for s in discover_opencode_sessions():
            all_sessions.append(f"  - {s.title} (opencode, running)")
        if all_sessions:
            print("Active sessions:")
            for line in all_sessions:
                print(line)
        sys.exit(1)

    match_type, match_session = match

    if match_type == "tmux":
        if match_session.status == SessionStatus.WORKING:
            print(f"Session '{target}' is currently working. Force hibernate? (y/N) ", end="")
            if input().strip().lower() != "y":
                print("Aborted.")
                return

        print(f"Hibernating {match_session.process.agent.display_name} session '{target}'...")
        result = hibernate_session(match_session)
    else:
        print(f"Hibernating opencode session '{target}'...")
        result = hibernate_opencode_session(match_session)

    if result.success:
        msg = f"Hibernated successfully."
        if result.context_path:
            msg += f" Context saved to: {result.context_path}"
        print(msg)
    else:
        print(f"Failed: {result.error}")
        sys.exit(1)


def cmd_restore(args: argparse.Namespace) -> None:
    """Restore a hibernated session (tmux or opencode)."""
    import json as json_mod

    from hibernator.db import get_session

    # Look up the session in the database first
    try:
        session_id = int(args.target)
        record = get_session(session_id=session_id)
    except ValueError:
        record = get_session(session_name=args.target)

    if record is None:
        error_msg = f"Session '{args.target}' not found in database."
        if getattr(args, "json", False):
            print(json_mod.dumps({"success": False, "session_name": args.target, "error": error_msg}))
            sys.exit(1)
        print(f"Failed: {error_msg}")
        sys.exit(1)

    agent_type = record.get("agent_type", "claude")

    if agent_type == "opencode":
        result = restore_opencode_session(record["context_file_path"])
    else:
        try:
            session_id_val = int(args.target)
            result = restore_session(session_id=session_id_val)
        except ValueError:
            result = restore_session(session_name=args.target)

    if getattr(args, "json", False):
        print(json_mod.dumps({
            "success": result.success,
            "session_name": result.session_name,
            "error": result.error,
        }))
        if not result.success:
            sys.exit(1)
        return

    if result.success:
        if agent_type == "opencode":
            print(f"Restored opencode session '{result.session_name}'.")
        else:
            print(f"Restored session '{result.session_name}'. Attach with: tmux attach -t {result.session_name}")
    else:
        print(f"Failed: {result.error}")
        sys.exit(1)


def cmd_context(args: argparse.Namespace) -> None:
    """Print the context file for a hibernated session."""
    init_db()

    try:
        session_id = int(args.target)
        record = get_session(session_id=session_id)
    except ValueError:
        record = get_session(session_name=args.target)

    if record is None:
        print(f"Session '{args.target}' not found.")
        sys.exit(1)

    path = Path(record["context_file_path"])
    if not path.exists():
        print(f"Context file missing: {path}")
        sys.exit(1)

    print(path.read_text())


def cmd_check(args: argparse.Namespace) -> None:
    """Run one monitoring cycle."""
    threshold = args.threshold or IDLE_THRESHOLD_MINUTES
    print(f"Running check with {threshold}m threshold...")
    run_check(threshold_minutes=threshold)
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tmux-agent-hibernator",
        description="Hibernate and restore idle AI agent sessions in tmux",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show running agent sessions")
    p_list = sub.add_parser("list", help="List hibernated sessions")
    p_list.add_argument("--json", action="store_true", help="Output as JSON")
    sub.add_parser("history", help="Show all session history")
    sub.add_parser("agents", help="List supported agents")

    p_hib = sub.add_parser("hibernate", help="Manually hibernate a session")
    p_hib.add_argument("session_name", help="Tmux session name")

    p_res = sub.add_parser("restore", help="Restore a hibernated session")
    p_res.add_argument("target", help="Session name or ID")
    p_res.add_argument("--json", action="store_true", help="Output as JSON")

    p_ctx = sub.add_parser("context", help="Show context file for a session")
    p_ctx.add_argument("target", help="Session name or ID")

    p_chk = sub.add_parser("check", help="Run one monitoring cycle")
    p_chk.add_argument("--threshold", type=int, help="Override idle threshold (minutes)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ensure_dirs()

    if args.command is None:
        parser.print_help()
        return

    commands = {
        "status": cmd_status,
        "list": cmd_list,
        "history": cmd_history,
        "agents": cmd_agents,
        "hibernate": cmd_hibernate,
        "restore": cmd_restore,
        "context": cmd_context,
        "check": cmd_check,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
