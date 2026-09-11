import logging
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

from test_activity import Files, activity
from outer_progress import OuterProgress, attaches_to, progress_sequence, progress_state, windows_args


class OuterProgressTests(Files):
    def setUp(self):
        super().setUp()
        self.environment = patch.dict(os.environ, WT_SESSION="test-terminal",
                                      COPILOT_NOTIFY_OUTER_PROGRESS="1")
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.journal = self.directory / "outer.json"
        self.tokens = {10: "client", 9: "parent"}
        self.target = {"pid": 10, "token": "client", "parent": 9, "parent_token": "parent"}
        self.outer = self.create()
        self.outer.discover = Mock(return_value={"10": self.target})
        self.outer.write = Mock()

    def create(self):
        return OuterProgress(self.journal, "term", lambda: {}, self.tokens.get,
                             activity.read_json, activity.atomic_json,
                             logging.getLogger("outer-progress-tests"))

    def test_sequences_and_attention_precedence(self):
        for counts, state, sequence in (
            ((0, 0), 0, "\x1b]9;4;0;0\x07"),
            ((2, 0), 3, "\x1b]9;4;3;0\x07"),
            ((0, 1), 4, "\x1b]9;4;4;100\x07"),
            ((1, 1), 4, "\x1b]9;4;4;100\x07"),
        ):
            self.assertEqual(progress_state(counts), state)
            self.assertEqual(progress_sequence(state), sequence)

    def test_only_explicit_named_clients_match(self):
        for args in (["zellij", "attach", "term"], ["zellij", "a", "-c", "term"],
                     ["zellij", "--session", "term"], ["zellij", "--session=term"]):
            self.assertTrue(attaches_to(args, "term"), args)
        for args in (["zellij"], ["zellij", "attach"], ["zellij", "attach", "other"],
                     ["zellij", "--server", "term"], ["zellij", "action", "list-tabs"],
                     ["zellij", "--session", "term", "action", "list-tabs"],
                     ["zellij", "attach", "--create-background", "term"]):
            self.assertFalse(attaches_to(args, "term"), args)

    def test_aggregate_survives_other_session_finishing_and_focus_changes(self):
        self.assertIsNone(self.outer.update((2, 0)))
        self.outer.write.assert_called_once_with(self.target, 3)
        self.outer.update((1, 0))
        self.outer.next_discovery = 0
        self.outer.update((1, 0))
        self.outer.write.assert_called_once_with(self.target, 3)
        self.outer.update((0, 1))
        self.outer.write.assert_called_with(self.target, 4)
        self.outer.update((0, 0))
        self.outer.write.assert_called_with(self.target, 0, completed=True)
        self.assertEqual(activity.read_json(self.journal), {})

    def test_last_session_finishing_alerts_once_per_work_interval(self):
        self.outer.update((2, 0))
        self.outer.update((1, 0))
        self.outer.write.assert_called_once_with(self.target, 3)
        self.outer.update((0, 0))
        self.outer.write.assert_called_with(self.target, 0, completed=True)
        self.outer.write.reset_mock()
        self.outer.update((0, 0))
        self.outer.write.assert_not_called()
        self.outer.update((1, 0))
        self.outer.update((0, 0))
        self.outer.write.assert_called_with(self.target, 0, completed=True)

    def test_completion_sequence_clears_progress_then_rings_bell(self):
        self.assertEqual(progress_sequence(0, completed=True), "\x1b]9;4;0;0\x07\x07")
        with self.assertRaises(ValueError):
            progress_sequence(3, completed=True)

    def test_completion_failure_is_retried_without_marking_idle(self):
        self.outer.update((1, 0))
        self.outer.write.side_effect = OSError("unavailable")
        with self.assertLogs("outer-progress-tests", level="ERROR"):
            self.assertIsNotNone(self.outer.update((0, 0)))
        self.assertEqual(self.outer.applied["10"], 3)
        self.outer.write.side_effect = None
        self.assertIsNone(self.outer.update((0, 0)))
        self.outer.write.assert_called_with(self.target, 0, completed=True)

    def test_native_completion_delivers_clear_and_bell(self):
        outer = self.create()
        with patch("outer_progress.os.name", "nt"), \
                patch("outer_progress.write_windows") as write_windows:
            outer.write(self.target, 0, completed=True)
        write_windows.assert_called_once_with(10, "\x1b]9;4;0;0\x07\x07")

    def test_idle_does_not_clear_unowned_terminal_progress(self):
        self.outer.update((0, 0))
        self.outer.write.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_new_client_gets_current_state_without_state_change(self):
        self.outer.update((1, 0))
        other = dict(self.target, pid=20, token="other")
        self.outer.discover.return_value = {"10": self.target, "20": other}
        self.outer.next_discovery = 0
        self.outer.update((1, 0))
        self.outer.write.assert_called_with(other, 3)

    def test_detached_client_is_cleared(self):
        self.outer.update((1, 0))
        self.outer.discover.return_value = {}
        self.outer.next_discovery = 0
        self.outer.update((1, 0))
        self.outer.write.assert_called_with(self.target, 0)
        self.assertEqual(activity.read_json(self.journal), {})

    def test_shutdown_clears_progress(self):
        self.outer.update((1, 0))
        self.outer.update((0, 0), closing=True)
        self.outer.write.assert_called_with(self.target, 0)
        self.assertEqual(activity.read_json(self.journal), {})

    def test_restart_clears_journal_owned_state(self):
        self.outer.update((1, 0))
        recovered = self.create()
        recovered.discover = Mock(return_value={"10": self.target})
        recovered.write = Mock()
        recovered.update((0, 0))
        recovered.write.assert_called_once_with(self.target, 0)
        self.assertEqual(activity.read_json(self.journal), {})

    def test_opt_out_restores_previous_progress(self):
        self.outer.update((1, 0))
        with patch.dict(os.environ, COPILOT_NOTIFY_OUTER_PROGRESS="0"):
            recovered = self.create()
        recovered.discover = Mock()
        recovered.write = Mock()
        recovered.update((1, 0))
        recovered.discover.assert_not_called()
        recovered.write.assert_called_once_with(self.target, 0)

    def test_non_windows_terminal_is_untouched(self):
        with patch.dict(os.environ, WT_SESSION=""):
            outer = self.create()
        outer.discover = Mock()
        outer.update((1, 0))
        outer.discover.assert_not_called()

    def test_invalid_setting_reports_error(self):
        with patch.dict(os.environ, COPILOT_NOTIFY_OUTER_PROGRESS="invalid"):
            with self.assertRaisesRegex(ValueError, "must be 0 or 1"):
                self.create()

    def test_output_failure_is_reported_and_retried(self):
        self.outer.write.side_effect = OSError("console unavailable")
        with self.assertLogs("outer-progress-tests", level="ERROR"):
            self.assertIn("console unavailable", self.outer.update((1, 0)))
        self.assertIn("10", activity.read_json(self.journal))
        self.outer.write.side_effect = None
        self.assertIsNone(self.outer.update((1, 0)))
        self.outer.write.assert_called_with(self.target, 3)

    def test_discovery_failure_does_not_clear_previous_progress(self):
        self.outer.update((1, 0))
        self.outer.next_discovery = 0
        self.outer.discover.side_effect = OSError("process query failed")
        with self.assertLogs("outer-progress-tests", level="ERROR"):
            self.assertIn("process query failed", self.outer.update((1, 0)))
        self.outer.write.assert_called_once_with(self.target, 3)

    def test_pid_reuse_never_writes_to_new_process(self):
        outer = self.create()
        self.tokens[10] = "reused"
        self.tokens[9] = "reused-parent"
        with patch("outer_progress.os.open") as open_fd, \
                patch("outer_progress.write_windows") as write_windows, \
                self.assertLogs("outer-progress-tests", level="WARNING"):
            outer.write(self.target, 0)
        open_fd.assert_not_called()
        write_windows.assert_not_called()

    def test_linux_discovery_requires_matching_session_and_windows_terminal(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux /proc discovery")
        for pid, args, env in (
            (10, b"zellij\0attach\0term\0", b"WT_SESSION=test\0"),
            (11, b"zellij\0attach\0other\0", b"WT_SESSION=test\0"),
            (12, b"zellij\0attach\0term\0", b"TERM=xterm\0"),
            (13, b"zellij\0--session\0term\0action\0list-tabs\0", b"WT_SESSION=test\0"),
        ):
            proc = self.directory / str(pid)
            (proc / "fd").mkdir(parents=True)
            (proc / "environ").write_bytes(env)
            (proc / "cmdline").write_bytes(args)
            (proc / "fd/1").symlink_to("/dev/null")
            self.tokens[pid] = "client"
        outer = self.create()
        outer.process_table = lambda: {pid: (9, "zellij") for pid in range(10, 14)}

        def proc_path(*args):
            if args[0] == "/proc":
                return self.directory.joinpath(*args[1:])
            return Path(*args)

        with patch("outer_progress.Path", side_effect=proc_path):
            self.assertEqual(set(outer.discover()), {"10"})

    def test_windows_command_parsing_preserves_quoted_session_name(self):
        if os.name != "nt":
            self.skipTest("Native CommandLineToArgvW")
        args = windows_args('"C:\\Program Files\\zellij.exe" attach "my session"')
        self.assertEqual(args, ["C:\\Program Files\\zellij.exe", "attach", "my session"])
        self.assertTrue(attaches_to(args, "my session"))

    def test_linux_writes_outer_pty_and_preserves_title_bytes(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux PTY delivery")
        import pty
        import select
        master, slave = pty.openpty()
        try:
            target = dict(self.target, terminal=os.ttyname(slave),
                          device=os.fstat(slave).st_rdev)
            outer = self.create()
            with patch("outer_progress.Path") as path:
                path.return_value = Path("/proc/self/fd", str(slave))
                # resolve() is used to compare the actual terminal path.
                self.assertEqual(str(path.return_value.resolve()), target["terminal"])
                outer.write(target, 3)
            self.assertTrue(select.select([master], [], [], 1)[0])
            self.assertEqual(os.read(master, 1024), b"\x1b]9;4;3;0\x07")
        finally:
            os.close(slave)
            os.close(master)

    def test_native_delivery_uses_client_console_not_hook_console(self):
        with patch("outer_progress.os.name", "nt"), \
                patch("outer_progress.write_windows") as write_windows:
            self.create().write(self.target, 3)
        write_windows.assert_called_once_with(10, "\x1b]9;4;3;0\x07")

    def test_native_detach_clears_original_parent_console(self):
        self.tokens.pop(10)
        outer = self.create()
        with patch("outer_progress.os.name", "nt"), \
                patch("outer_progress.write_windows") as write_windows:
            outer.write(self.target, 0)
        write_windows.assert_called_once_with(9, "\x1b]9;4;0;0\x07")

    def test_exited_client_never_sets_progress_on_parent(self):
        self.tokens.pop(10)
        outer = self.create()
        with patch("outer_progress.write_windows") as write_windows:
            with self.assertRaisesRegex(ProcessLookupError, "client exited"):
                outer.write(self.target, 3)
        write_windows.assert_not_called()
