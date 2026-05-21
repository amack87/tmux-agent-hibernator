"""Tests for opencode session discovery and management."""

import json
import os
import shutil
import signal
import subprocess
import unittest
from unittest import mock

from hibernator.opencode import (
    OpencodeProcess,
    OpencodeSession,
    discover_opencode_processes,
    discover_opencode_sessions,
    hibernate_opencode_session,
    restore_opencode_session,
    _find_opencode,
    _run_opencode,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_PS_OUTPUT = """  PID TTY      ARGS
 5337 ??        /Users/andy/.opencode/bin/opencode --port 4097
28752 ??        /Users/andy/.opencode/bin/opencode --port 4098 -s ses_abc123
61465 ??        /Users/andy/.opencode/bin/opencode --port 4096 -s ses_def456
 2521 ??        /opt/homebrew/bin/tmux attach-session -t opencode
"""

MOCK_EXPORT_JSON = json.dumps({
    "info": {
        "id": "ses_abc123",
        "title": "Fix login bug",
        "agent": "general",
        "model": {"id": "deepseek-v4-flash", "providerID": "opencode-go"},
        "version": "1.15.5",
        "directory": "/home/user/project",
        "time": {"created": 1779288976409, "updated": 1779309041826},
        "cost": 0.01,
        "tokens": {"input": 100, "output": 50},
    },
    "messages": [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ],
})

MOCK_EMPTY_PS = "  PID TTY      ARGS\n"


# ---------------------------------------------------------------------------
# discover_opencode_processes
# ---------------------------------------------------------------------------

class TestDiscoverProcesses(unittest.TestCase):
    """Discovering opencode processes from ps output."""

    @mock.patch("hibernator.opencode.subprocess.run")
    def test_finds_processes_with_session_ids(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=MOCK_PS_OUTPUT, stderr="",
        )
        processes = discover_opencode_processes()
        # 3 opencode binaries, 1 tmux process excluded
        self.assertEqual(len(processes), 3)

        # Process with session ID and port
        p28752 = [p for p in processes if p.pid == 28752][0]
        self.assertEqual(p28752.session_id, "ses_abc123")
        self.assertEqual(p28752.port, 4098)

        # Process with session ID only
        p61465 = [p for p in processes if p.pid == 61465][0]
        self.assertEqual(p61465.session_id, "ses_def456")
        self.assertEqual(p61465.port, 4096)

    @mock.patch("hibernator.opencode.subprocess.run")
    def test_excludes_tmux_wrapper_processes(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=MOCK_PS_OUTPUT, stderr="",
        )
        processes = discover_opencode_processes()
        pids = [p.pid for p in processes]
        self.assertNotIn(2521, pids, "tmux attach-session should be excluded")

    @mock.patch("hibernator.opencode.subprocess.run")
    def test_finds_fresh_process_without_session_id(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=MOCK_PS_OUTPUT, stderr="",
        )
        processes = discover_opencode_processes()
        fresh = [p for p in processes if p.pid == 5337][0]
        self.assertIsNone(fresh.session_id)
        self.assertEqual(fresh.port, 4097)

    @mock.patch("hibernator.opencode.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        )
        self.assertEqual(discover_opencode_processes(), [])

    @mock.patch("hibernator.opencode.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ps", timeout=5)
        self.assertEqual(discover_opencode_processes(), [])

    @mock.patch("hibernator.opencode.subprocess.run")
    def test_ignores_grep_processes(self, mock_run):
        ps_with_grep = MOCK_PS_OUTPUT + " 99999 ?? grep opencode\n"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=ps_with_grep, stderr="",
        )
        processes = discover_opencode_processes()
        pids = [p.pid for p in processes]
        self.assertNotIn(99999, pids)


# ---------------------------------------------------------------------------
# discover_opencode_sessions
# ---------------------------------------------------------------------------

class TestDiscoverSessions(unittest.TestCase):
    """Discovering full session metadata."""

    @mock.patch("hibernator.opencode.subprocess.run")
    def test_enriches_processes_with_export_data(self, mock_run):
        def side_effect(cmd, *a, **kw):
            if cmd[0] == "ps" or (isinstance(cmd, list) and "ps" in cmd[0]):
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=MOCK_PS_OUTPUT, stderr="",
                )
            # Return export JSON for any opencode export call
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=MOCK_EXPORT_JSON, stderr="",
            )
        mock_run.side_effect = side_effect

        sessions = discover_opencode_sessions()
        # 3 opencode binaries, 1 tmux process excluded
        self.assertEqual(len(sessions), 3)

        abc = [s for s in sessions if s.session_id == "ses_abc123"][0]
        self.assertEqual(abc.title, "Fix login bug")
        self.assertEqual(abc.directory, "/home/user/project")
        self.assertEqual(abc.agent, "general")
        self.assertEqual(abc.pid, 28752)

        def456 = [s for s in sessions if s.session_id == "ses_def456"][0]
        self.assertEqual(def456.pid, 61465)

        fresh = [s for s in sessions if s.session_id == "pid:5337"][0]
        self.assertEqual(fresh.pid, 5337)
        self.assertEqual(fresh.title, "opencode (pid 5337)")


# ---------------------------------------------------------------------------
# hibernate_opencode_session
# ---------------------------------------------------------------------------

class TestHibernateSession(unittest.TestCase):
    """Hibernating an opencode session."""

    def setUp(self):
        self.session = OpencodeSession(
            session_id="ses_abc123",
            title="Fix login bug",
            directory="/home/user/project",
            agent="general",
            version="1.15.5",
            pid=28752,
            port=4098,
            updated=1779309041826,
            created=1779288976409,
        )

    @mock.patch("hibernator.opencode.Path")
    @mock.patch("hibernator.opencode._run_opencode")
    @mock.patch("hibernator.opencode.os.kill")
    @mock.patch("hibernator.opencode.time.sleep")
    def test_exports_and_kills(self, mock_sleep, mock_kill, mock_export, mock_path):
        mock_export.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        mock_path_instance = mock.MagicMock()
        mock_path.return_value = mock_path_instance
        mock_path_instance.read_text.return_value = MOCK_EXPORT_JSON
        mock_path_instance.write_text = mock.MagicMock()

        result = hibernate_opencode_session(self.session)

        self.assertTrue(result.success)
        self.assertEqual(result.session_id, "ses_abc123")
        self.assertIsNotNone(result.context_path)

        mock_export.assert_called_once()
        args, kwargs = mock_export.call_args
        # Called with opencode args: "export", session_id, "--sanitize"
        self.assertEqual(args[0], "export")
        self.assertIn("stdout_file", kwargs)
        self.assertTrue(str(kwargs["stdout_file"]).endswith(".json"))
        mock_kill.assert_any_call(28752, signal.SIGTERM)

    @mock.patch("hibernator.opencode.Path")
    @mock.patch("hibernator.opencode._run_opencode")
    def test_handles_export_failure(self, mock_export, mock_path):
        mock_export.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Export failed",
        )
        mock_path_instance = mock.MagicMock()
        mock_path.return_value = mock_path_instance
        mock_path_instance.read_text.return_value = "{}"

        result = hibernate_opencode_session(self.session)
        self.assertFalse(result.success)
        self.assertIn("Export failed", result.error)

    @mock.patch("hibernator.opencode.Path")
    def test_handles_session_without_pid(self, mock_path):
        no_pid = OpencodeSession(
            session_id="ses_abc123", title="Test", directory="",
            agent="general", version="", pid=None, port=None,
            updated=0, created=0,
        )
        with mock.patch("hibernator.opencode._run_opencode") as mock_export:
            mock_export.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            )
            mock_path_instance = mock.MagicMock()
            mock_path.return_value = mock_path_instance
            mock_path_instance.read_text.return_value = MOCK_EXPORT_JSON
            mock_path_instance.write_text = mock.MagicMock()

            result = hibernate_opencode_session(no_pid)
            self.assertTrue(result.success)
            self.assertIsNotNone(result.context_path)


# ---------------------------------------------------------------------------
# restore_opencode_session
# ---------------------------------------------------------------------------

class TestRestoreSession(unittest.TestCase):
    """Restoring an opencode session."""

    def test_returns_error_when_context_missing(self):
        result = restore_opencode_session("/nonexistent/path.json")
        self.assertFalse(result.success)
        self.assertIn("not found", result.error)

    @mock.patch("hibernator.opencode.Path.exists")
    @mock.patch("hibernator.opencode._run_opencode")
    @mock.patch("hibernator.opencode.Path.read_text")
    def test_imports_context_and_returns_success(
        self, mock_read, mock_run, mock_exists
    ):
        mock_exists.return_value = True
        mock_read.return_value = MOCK_EXPORT_JSON
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=MOCK_EXPORT_JSON, stderr="",
        )

        result = restore_opencode_session("/tmp/test-export.json")
        self.assertTrue(result.success)

    @mock.patch("hibernator.opencode.Path.exists")
    @mock.patch("hibernator.opencode._run_opencode")
    def test_handles_import_failure(self, mock_run, mock_exists):
        mock_exists.return_value = True
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Import failed",
        )

        result = restore_opencode_session("/tmp/test-export.json")
        self.assertFalse(result.success)
        self.assertIn("Import failed", result.error)


# ---------------------------------------------------------------------------
# _find_opencode
# ---------------------------------------------------------------------------

class TestFindOpencode(unittest.TestCase):
    """Finding the opencode binary."""

    @mock.patch("hibernator.opencode.Path.exists")
    @mock.patch("hibernator.opencode.shutil.which")
    def test_finds_via_candidate_paths(self, mock_which, mock_exists):
        mock_exists.side_effect = lambda: False
        mock_which.return_value = "/custom/path/opencode"

        binary = _find_opencode()
        self.assertEqual(binary, "/custom/path/opencode")

    @mock.patch("hibernator.opencode.Path.exists")
    def test_returns_none_when_not_found(self, mock_exists):
        mock_exists.side_effect = lambda: False

        with mock.patch("hibernator.opencode.shutil.which", return_value=None):
            binary = _find_opencode()
            self.assertIsNone(binary)


if __name__ == "__main__":
    unittest.main()
