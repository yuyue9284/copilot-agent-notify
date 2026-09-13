import json
import io
import errno
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import session_gadget as gadget
import session_probe as probe


def event(kind, **data):
    return {"type": kind, "data": data}


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        cache = patch.object(probe, "state_directory", return_value=self.home / "cache" / "zellij" / "test")
        cache.start()
        self.addCleanup(cache.stop)
        self.scanner = probe.Scanner(self.home)
        self.token = patch.object(probe, "process_token", return_value="identity")
        self.token.start()
        self.addCleanup(self.token.stop)
        self.birth = patch.object(probe, "started_at", return_value=time.time() - 60)
        self.birth.start()
        self.addCleanup(self.birth.stop)

    def session(self, name="root", pid=42, events=()):
        directory = self.home / "session-state" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ("inuse.%d.lock" % pid)).write_text(str(pid))
        self.write(directory, [event("session.start", sessionId=name, context={"cwd": "/project"})]
                   + list(events))
        return directory

    def write(self, directory, events, mode="w"):
        with (directory / "events.jsonl").open(mode, encoding="utf-8") as stream:
            for value in events:
                stream.write(json.dumps(value) + "\n")

    def status(self):
        snapshot = self.scanner.snapshot()
        self.assertEqual(snapshot["errors"], [])
        return snapshot["sessions"][0]["status"]

    def test_busy_done_resume_input_and_process_close(self):
        directory = self.session(events=[event("assistant.turn_start")])
        self.assertEqual(self.status(), "In progress")
        reader = next(iter(self.scanner.readers.values()))[0]
        self.write(directory, [event("assistant.message", phase="final_answer", turnId="1"),
                               event("assistant.turn_end", turnId="1")], "a")
        self.assertEqual(self.status(), "Done")
        self.assertIs(reader, next(iter(self.scanner.readers.values()))[0])
        self.write(directory, [event("assistant.turn_start"),
                               event("hook.start", hookType="notification", input={
                                   "notification_type": "permission_prompt", "sessionId": "root"})], "a")
        self.assertEqual(self.status(), "Needs input")
        self.write(directory, [event("assistant.turn_start")], "a")
        self.assertEqual(self.status(), "In progress")
        with patch.object(probe, "process_token", return_value=None):
            self.assertEqual(self.scanner.snapshot()["sessions"], [])
        self.assertEqual(self.scanner.readers, {})

    def test_dead_or_reused_pid_does_not_resurrect_old_session(self):
        directory = self.session()
        os.utime(directory / "inuse.42.lock", (1, 1))
        self.assertEqual(self.scanner.snapshot()["sessions"], [])

    def test_verified_identity_survives_clock_shift_and_scanner_restart(self):
        self.session(events=[event("assistant.turn_start")])
        self.assertEqual(self.status(), "In progress")
        with patch.object(probe, "started_at", return_value=time.time() + 6 * 3600) as estimate:
            self.assertEqual(self.status(), "In progress")
            self.assertEqual(probe.Scanner(self.home).snapshot()["sessions"][0]["status"], "In progress")
            estimate.assert_not_called()

    def test_cached_identity_rejects_reused_pid_even_with_misleading_clock(self):
        self.session()
        self.assertEqual(self.status(), "Done")
        with patch.object(probe, "process_token", return_value="another-process"), \
                patch.object(probe, "started_at", return_value=0):
            for scanner in (self.scanner, probe.Scanner(self.home), probe.Scanner(self.home)):
                self.assertEqual(scanner.snapshot()["sessions"], [])

    def test_previous_boot_identity_cannot_adopt_old_lock(self):
        self.session()
        with patch.object(probe, "boot_identity", return_value="first-boot"):
            self.assertEqual(self.status(), "Done")
        with patch.object(probe, "boot_identity", return_value="second-boot"), \
                patch.object(probe, "started_at", return_value=0):
            self.assertEqual(probe.Scanner(self.home).snapshot()["sessions"], [])
            self.assertEqual(probe.Scanner(self.home).snapshot()["sessions"], [])

    def registration(self, directory, token="identity", session_id=None, transcript=None):
        path = self.home / "cache" / "zellij" / "test" / "registrations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"registered": {
            "session_id": session_id or directory.name, "pid": 42, "token": token,
            "transcript": str(transcript or directory / "events.jsonl"),
        }}))
        return path

    def test_registration_recovers_first_scan_after_clock_shift(self):
        directory = self.session(events=[event("assistant.turn_start")])
        registration = self.registration(directory)
        with patch.object(probe, "started_at", return_value=time.time() + 6 * 3600):
            self.assertEqual(self.status(), "In progress")
            registration.unlink()
            self.assertEqual(probe.Scanner(self.home).snapshot()["sessions"][0]["status"], "In progress")

    def test_registration_must_match_session_path_and_process_identity(self):
        directory = self.session()
        with patch.object(probe, "started_at", return_value=time.time() + 6 * 3600):
            for overrides in ({"token": "different"}, {"session_id": "another"},
                              {"transcript": self.home / "other" / "events.jsonl"}):
                self.registration(directory, **overrides)
                self.assertEqual(self.scanner.snapshot()["sessions"], [])

    def test_rewritten_lock_is_not_trusted_from_previous_cache_entry(self):
        directory = self.session()
        self.assertEqual(self.status(), "Done")
        lock = directory / "inuse.42.lock"
        os.utime(lock, (1, 1))
        with patch.object(probe, "started_at", return_value=time.time() + 6 * 3600):
            self.assertEqual(self.scanner.snapshot()["sessions"], [])

    def test_identity_cache_errors_are_reported_without_hiding_verified_live_rows(self):
        self.session()
        with patch.object(probe, "atomic_json", side_effect=PermissionError("read-only cache")):
            snapshot = self.scanner.snapshot()
            self.assertEqual(len(snapshot["sessions"]), 1)
            self.assertTrue(any("Cannot save process identities" in error for error in snapshot["errors"]))
            with patch.object(probe, "started_at", return_value=time.time() + 6 * 3600):
                self.assertEqual(len(self.scanner.snapshot()["sessions"]), 1)
        self.scanner.identity_path.parent.mkdir(parents=True, exist_ok=True)
        self.scanner.identity_path.write_text('{"root/42": false}')
        snapshot = self.scanner.snapshot()
        self.assertEqual(len(snapshot["sessions"]), 1)
        self.assertTrue(any("invalid process identity cache" in error for error in snapshot["errors"]))

    def test_same_process_keeps_all_root_sessions_and_ignores_empty_launch(self):
        old = self.session("old")
        os.utime(old / "inuse.42.lock", (time.time() - 20, time.time() - 20))
        self.session("resumed")
        empty = self.session("empty")
        (empty / "events.jsonl").unlink()
        self.assertEqual({row["id"] for row in self.scanner.snapshot()["sessions"]}, {"old", "resumed"})

    def test_shared_process_tracks_each_status_and_completion_independently(self):
        working = self.session("working", events=[event("assistant.turn_start")])
        waiting = self.session("waiting", events=[
            event("assistant.turn_start"),
            event("hook.start", hookType="notification", input={
                "notification_type": "permission_prompt", "sessionId": "waiting"})])
        self.session("idle")

        def statuses():
            snapshot = self.scanner.snapshot()
            self.assertEqual(snapshot["errors"], [])
            self.assertTrue(all(row["pid"] == 42 for row in snapshot["sessions"]))
            return {row["id"]: row["status"] for row in snapshot["sessions"]}

        self.assertEqual(statuses(), {"working": "In progress", "waiting": "Needs input", "idle": "Done"})
        reader = self.scanner.readers[("working", (42, "identity"))][0]
        self.write(working, [event("assistant.message", phase="final_answer", turnId="1"),
                             event("assistant.turn_end", turnId="1")], "a")
        self.write(waiting, [event("assistant.turn_start")], "a")
        self.assertEqual(statuses(), {"working": "Done", "waiting": "In progress", "idle": "Done"})
        self.assertIs(reader, self.scanner.readers[("working", (42, "identity"))][0])
        self.session("new", events=[event("assistant.turn_start")])
        self.assertEqual(len(statuses()), 4)
        self.assertIs(reader, self.scanner.readers[("working", (42, "identity"))][0])

    def test_shared_process_session_close_and_lock_removal_do_not_remove_siblings(self):
        closed = self.session("closed")
        unlocked = self.session("unlocked")
        self.session("remaining", events=[event("assistant.turn_start")])
        self.assertEqual(len(self.scanner.snapshot()["sessions"]), 3)
        self.write(closed, [event("session.shutdown")], "a")
        (unlocked / "inuse.42.lock").unlink()
        self.assertEqual([row["id"] for row in self.scanner.snapshot()["sessions"]], ["remaining"])
        self.assertNotIn(("unlocked", (42, "identity")), self.scanner.readers)
        self.write(closed, [event("session.resume"), event("assistant.turn_start")], "a")
        self.assertEqual({row["id"] for row in self.scanner.snapshot()["sessions"]}, {"closed", "remaining"})
        with patch.object(probe, "process_token", return_value=None):
            self.assertEqual(self.scanner.snapshot()["sessions"], [])
            self.assertEqual(self.scanner.readers, {})

    def test_shared_process_stale_lock_does_not_hide_valid_sessions(self):
        stale = self.session("stale")
        os.utime(stale / "inuse.42.lock", (1, 1))
        self.session("first")
        self.session("second")
        self.assertEqual({row["id"] for row in self.scanner.snapshot()["sessions"]}, {"first", "second"})

    def test_different_processes_and_multiple_locks_deduplicate(self):
        directory = self.session()
        (directory / "inuse.99.lock").write_text("99")
        self.session("second", pid=100)
        # Multiple open owners of one root should yield one session row.
        rows = self.scanner.snapshot()["sessions"]
        self.assertEqual({row["id"] for row in rows}, {"root", "second"})
        self.assertEqual(len(rows), 2)

    def test_shutdown_is_not_an_open_session(self):
        self.session(events=[event("session.shutdown")])
        self.assertEqual(self.scanner.snapshot()["sessions"], [])

    def test_missing_transcript_is_not_done(self):
        directory = self.session()
        (directory / "events.jsonl").unlink()
        self.assertEqual(self.scanner.snapshot()["sessions"], [])

    def test_invalid_transcript_is_unknown_and_reported(self):
        directory = self.session()
        (directory / "events.jsonl").write_text("not json\n")
        snapshot = self.scanner.snapshot()
        self.assertEqual(snapshot["sessions"][0]["status"], "Unknown")
        self.assertTrue(snapshot["errors"])

    def test_malformed_record_is_unknown_not_false_done(self):
        directory = self.session()
        with (directory / "events.jsonl").open("a") as stream:
            stream.write("bad json\n")
        self.assertEqual(self.scanner.snapshot()["sessions"][0]["status"], "Unknown")

    def test_large_history_remains_loading_until_caught_up(self):
        self.session()
        with patch.object(probe.Transcript, "poll", return_value=False):
            self.assertEqual(self.status(), "Loading")

    def test_name_and_metadata_only_no_prompts_in_output(self):
        directory = self.session(events=[event("user.message", content="private prompt")])
        (directory / "workspace.yaml").write_text("name: \"Useful title\"\n")
        snapshot = self.scanner.snapshot()
        row = snapshot["sessions"][0]
        self.assertEqual(row["title"], "Useful title")
        self.assertEqual(row["cwd"], "/project")
        self.assertNotIn("private prompt", json.dumps(snapshot))

    def test_live_child_work_is_aggregated_into_root(self):
        self.session(events=[{"type": "assistant.turn_start", "agentId": "child", "data": {}}])
        self.assertEqual(self.status(), "In progress")

    def test_transcript_replacement_resets_reader(self):
        directory = self.session(events=[event("assistant.turn_start")])
        self.assertEqual(self.status(), "In progress")
        self.write(directory, [event("session.start", sessionId="root")])
        self.assertEqual(self.status(), "Done")

    def test_activity_revision_ignores_bookkeeping_and_survives_replay(self):
        directory = self.session(events=[event("assistant.turn_start", turnId="1")])

        def revision(scanner=None):
            return (scanner or self.scanner).snapshot()["sessions"][0]["activity_revision"]

        first = revision()
        self.assertTrue(first)
        self.write(directory, [event("assistant.message", phase="final_answer", turnId="1"),
                               event("assistant.turn_end", turnId="1"),
                               event("session.model_change"), event("session.resume")], "a")
        self.assertEqual(revision(), first)
        self.assertEqual(revision(probe.Scanner(self.home)), first)
        self.write(directory, [event("assistant.turn_start", turnId="2")], "a")
        self.assertNotEqual(revision(), first)

    def test_activity_revision_marks_only_requested_activity(self):
        self.session()
        tracker = probe.ActivityRevision("root")
        for kind in ("session.start", "session.resume", "session.info", "model.message"):
            tracker.event(event(kind))
            self.assertEqual(tracker.activity_revision, "")
        activities = [event("user.message"), event("assistant.turn_start"),
                      event("tool.execution_start"),
                      event("hook.start", hookType="notification",
                            input={"notification_type": "permission_prompt"}),
                      event("hook.start", hookType="notification",
                            input={"notification_type": "elicitation_dialog"})]
        for index, item in enumerate(activities):
            item["id"] = str(index)
            previous = tracker.activity_revision
            tracker.event(item)
            self.assertNotEqual(tracker.activity_revision, previous)
            previous = tracker.activity_revision
            tracker.event(event("hook.start", hookType="agentStop"))
            tracker.event(event("tool.execution_complete"))
            self.assertEqual(tracker.activity_revision, previous)
        repeat = event("user.message", content="same prompt")
        repeat["id"] = "first"
        tracker.event(repeat)
        first = tracker.activity_revision
        repeat["id"] = "second"
        tracker.event(repeat)
        self.assertNotEqual(tracker.activity_revision, first)

    def test_activity_revision_is_only_published_after_catchup(self):
        self.session(events=[event("assistant.turn_start")])
        with patch.object(probe.Transcript, "poll", return_value=False):
            row = self.scanner.snapshot()["sessions"][0]
            self.assertEqual(row["status"], "Loading")
            self.assertIsNone(row["activity_revision"])
        self.assertTrue(self.scanner.snapshot()["sessions"][0]["activity_revision"])

    def test_activity_revision_orders_timestamped_events_and_resets_on_replacement(self):
        newer = event("assistant.turn_start", turnId="new")
        newer["timestamp"] = "2026-09-12T12:00:00+08:00"
        directory = self.session(events=[newer])
        first = self.scanner.snapshot()["sessions"][0]["activity_revision"]
        older = event("assistant.turn_start", turnId="old")
        older["timestamp"] = "2026-09-12T03:00:00Z"
        self.write(directory, [event("session.start", sessionId="root"), older])
        second = self.scanner.snapshot()["sessions"][0]["activity_revision"]
        self.assertLess(second[:20], first[:20])
        self.write(directory, [event("session.start", sessionId="root")])
        self.assertEqual(self.scanner.snapshot()["sessions"][0]["activity_revision"], "")


class SourceTests(unittest.TestCase):
    def test_config_missing_defaults_and_valid_allowlists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gadget.json"
            self.assertEqual(gadget.load_config(path), {"wsl_distros": None})
            for config in ({}, {"wsl_distros": None}, {"wsl_distros": []},
                           {"wsl_distros": ["Ubuntu"]}):
                path.write_text(json.dumps(config), encoding="utf-8-sig")
                self.assertEqual(gadget.load_config(path)["wsl_distros"], config.get("wsl_distros"))

    def test_invalid_config_is_reported_not_treated_as_monitor_all(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gadget.json"
            for config in ([], {"typo": ["Ubuntu"]}, {"wsl_distros": "Ubuntu"},
                           {"wsl_distros": [None]}, {"wsl_distros": [""]},
                           {"wsl_distros": [" Ubuntu"]}):
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    gadget.load_config(path)
            path.write_text("{")
            with self.assertRaises(ValueError):
                gadget.load_config(path)

    def test_allowlist_filters_running_distros_without_starting_missing_ones(self):
        with patch.object(gadget, "running_distros", return_value=["Ubuntu", "docker-desktop"]):
            self.assertEqual(gadget.selected_distros(["ubuntu", "Debian"]), {"Ubuntu"})
            self.assertEqual(gadget.selected_distros(None), {"Ubuntu", "docker-desktop"})
        with patch.object(gadget, "running_distros") as discovery:
            self.assertEqual(gadget.selected_distros([]), set())
            discovery.assert_not_called()

    def test_monitor_only_starts_allowed_workers(self):
        monitor = gadget.Monitor(wsl_distros=["Ubuntu"])
        with patch.object(gadget, "Scanner") as scanner, \
                patch.object(gadget, "running_distros", return_value=["Ubuntu", "docker-desktop"]), \
                patch.object(gadget, "WslWorker") as worker:
            scanner.return_value.snapshot.return_value = {"sessions": [], "errors": []}
            worker.return_value.start.side_effect = monitor.close
            monitor.run()
            worker.assert_called_once_with("Ubuntu", monitor.updates)
            worker.return_value.close.assert_called_once()
            source, _, _ = monitor.updates.get_nowait()
            self.assertEqual(source, "Windows")

    def test_distros_decode_utf16_and_do_not_start_stopped_distros(self):
        result = subprocess.CompletedProcess([], 0, "Ubuntu\r\nDebian\r\n".encode("utf-16-le"), b"")
        with patch.object(gadget.shutil, "which", return_value="wsl.exe"), \
                patch.object(gadget.subprocess, "run", return_value=result) as run:
            self.assertEqual(gadget.running_distros(), ["Ubuntu", "Debian"])
            self.assertEqual(run.call_args.args[0][1:], ["--list", "--running", "--quiet"])

    def test_missing_wsl_and_failed_discovery(self):
        with patch.object(gadget.shutil, "which", return_value=None):
            self.assertEqual(gadget.running_distros(), [])
        with patch.object(gadget.shutil, "which", return_value="wsl.exe"), \
                patch.object(gadget.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 1, b"", b"")):
            with self.assertRaises(OSError):
                gadget.running_distros()

    def test_stale_rows_become_unknown_without_false_done(self):
        row = {"id": "a", "title": "Title", "status": "In progress"}
        sources = {"Windows": ({"sessions": [row], "errors": []}, 0),
                   "WSL:Ubuntu": ({"sessions": [dict(row, status="Done")], "errors": []}, 20)}
        rows, errors = gadget.display_snapshot(sources, 20)
        self.assertEqual([row["status"] for row in rows], ["Unknown", "Done"])
        self.assertTrue(errors)
        self.assertEqual(sources["Windows"][0]["sessions"][0]["status"], "In progress")

    def test_source_failure_preserves_unknown_rows_and_recovers(self):
        sources = {}
        row = {"id": "sample", "status": "Done"}
        gadget.merge_update(sources, "Windows", {"sessions": [row], "errors": []}, 1)
        gadget.merge_update(sources, "Windows", {"sessions": [], "errors": ["Unavailable"]}, 2)
        self.assertEqual(sources["Windows"][0]["sessions"][0]["status"], "Unknown")
        self.assertEqual(row["status"], "Done")
        gadget.merge_update(sources, "Windows", {"sessions": [row], "errors": []}, 3)
        self.assertEqual(sources["Windows"][0]["sessions"][0]["status"], "Done")
        gadget.merge_update(sources, "Windows", None, 4)
        self.assertEqual(sources, {})

    @unittest.skipUnless(sys.platform.startswith("linux"), "WSL bootstrap runs on Linux")
    def test_bundled_worker_streams_and_exits_on_input_close(self):
        with tempfile.TemporaryDirectory() as home:
            sources = {name: (ROOT / "scripts" / (name + ".py")).read_text()
                       for name in ("outer_progress", "activity", "session_probe")}
            process = subprocess.Popen([sys.executable, "-u", "-c", gadget.BOOTSTRAP],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       env=dict(os.environ, COPILOT_HOME=home))
            try:
                output, error = process.communicate((json.dumps(sources) + "\n").encode(), timeout=10)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(json.loads(output), {"sessions": [], "errors": []})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class WorkerLifecycleTests(unittest.TestCase):
    class Process:
        def __init__(self, blocked=False):
            self.exited = threading.Event()
            self.writing = threading.Event()
            self.release = threading.Event()
            self.terminated = threading.Event()
            self.returncode = None
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            process = self

            class Input(io.BytesIO):
                def __init__(self):
                    super().__init__()
                    self.lock = threading.Lock()

                def write(self, data):
                    with self.lock:
                        process.writing.set()
                        if blocked:
                            process.release.wait(10)
                            raise BrokenPipeError("collector terminated")
                        return super().write(data)

                def close(self):
                    with self.lock:
                        super().close()
                        process.returncode = 0
                        process.exited.set()

            self.stdin = Input()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if not self.exited.wait(timeout):
                raise subprocess.TimeoutExpired("collector", timeout)
            return self.returncode

        def terminate(self):
            self.terminated.set()
            self.returncode = 1
            self.release.set()
            self.exited.set()

        def kill(self):
            self.terminate()

    def test_cancelled_worker_does_not_launch_after_source_read(self):
        worker = gadget.WslWorker("Ubuntu", queue.Queue())
        reading = threading.Event()
        release = threading.Event()
        original = Path.read_text

        def read(path, *args, **kwargs):
            reading.set()
            release.wait(5)
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read), \
                patch.object(gadget.subprocess, "Popen", return_value=self.Process()) as launch:
            worker.start()
            try:
                self.assertTrue(reading.wait(3))
                worker.close()
                release.set()
                worker.thread.join(3)
                self.assertFalse(worker.thread.is_alive())
                launch.assert_not_called()
            finally:
                release.set()
                worker.thread.join(5)

    def test_close_interrupts_blocked_bootstrap_before_closing_stdin(self):
        worker = gadget.WslWorker("Ubuntu", queue.Queue())
        process = self.Process(blocked=True)
        closing = threading.Thread(target=worker.close, daemon=True)
        with patch.object(gadget.subprocess, "Popen", return_value=process):
            worker.start()
            try:
                self.assertTrue(process.writing.wait(3))
                closing.start()
                self.assertTrue(process.terminated.wait(2), "Blocked stdin prevented process termination")
                closing.join(3)
                worker.thread.join(3)
                self.assertFalse(closing.is_alive(), "Close did not finish")
                self.assertFalse(worker.thread.is_alive(), "Worker did not finish")
                self.assertTrue(worker.updates.empty(), "Cancellation emitted a spurious source error")
            finally:
                process.release.set()
                process.terminate()
                closing.join(5)
                worker.thread.join(5)

    def test_cancel_during_process_creation_cleans_up_published_child(self):
        worker = gadget.WslWorker("Ubuntu", queue.Queue())
        process = self.Process(blocked=True)
        creating = threading.Event()
        release = threading.Event()

        def launch(*args, **kwargs):
            creating.set()
            release.wait(5)
            return process

        closing = threading.Thread(target=worker.close, daemon=True)
        with patch.object(gadget.subprocess, "Popen", side_effect=launch):
            worker.start()
            try:
                self.assertTrue(creating.wait(3))
                closing.start()
                self.assertTrue(worker.stopping.wait(3))
                release.set()
                closing.join(3)
                worker.thread.join(3)
                self.assertFalse(closing.is_alive())
                self.assertFalse(worker.thread.is_alive())
                self.assertIsNotNone(process.poll(), "Cancel left a late-created process alive")
                self.assertTrue(worker.updates.empty())
            finally:
                release.set()
                process.terminate()
                closing.join(5)
                worker.thread.join(5)

    def test_real_pipe_blocked_bootstrap_is_cancelled(self):
        worker = gadget.WslWorker("Ubuntu", queue.Queue())
        writing = threading.Event()
        created = []
        real_popen = subprocess.Popen

        class Input:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, data):
                writing.set()
                # Linux can buffer the entire real bootstrap. Fill beyond pipe
                # capacity so cancellation exercises an actual blocked OS write.
                return self.stream.write(data + b" " * (4 * 1024 * 1024))

        def launch(*args, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            process.stdin = Input(process.stdin)
            created.append(process)
            return process

        closing = threading.Thread(target=worker.close, daemon=True)
        with patch.object(gadget.subprocess, "Popen", side_effect=launch):
            worker.start()
            try:
                self.assertTrue(writing.wait(5))
                self.assertFalse(worker.bootstrap_sent.wait(.1), "Fixture did not block the real pipe")
                closing.start()
                closing.join(5)
                worker.thread.join(5)
                self.assertFalse(closing.is_alive(), "Real pipe cancellation hung")
                self.assertFalse(worker.thread.is_alive(), "Real worker cancellation hung")
                self.assertIsNotNone(created[0].poll())
                self.assertTrue(created[0].stdin.closed)
                self.assertTrue(created[0].stdout.closed)
                self.assertTrue(created[0].stderr.closed)
                self.assertTrue(worker.updates.empty())
            finally:
                for process in created:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                if closing.ident is not None:
                    closing.join(5)
                worker.thread.join(5)

    def test_ready_collector_gets_graceful_eof_without_termination(self):
        worker = gadget.WslWorker("Ubuntu", queue.Queue())
        real_popen = subprocess.Popen
        created = []
        script = ("import sys; sys.stdin.readline(); "
                  "print('{\"sessions\": [], \"errors\": []}', flush=True); sys.stdin.read()")

        def launch(*args, **kwargs):
            process = real_popen([sys.executable, "-u", "-c", script], **kwargs)
            created.append(process)
            return process

        with patch.object(gadget.subprocess, "Popen", side_effect=launch):
            worker.start()
            try:
                source, snapshot, _ = worker.updates.get(timeout=5)
                self.assertEqual((source, snapshot), ("WSL:Ubuntu", {"sessions": [], "errors": []}))
                self.assertTrue(worker.bootstrap_sent.is_set())
                with patch.object(created[0], "terminate", wraps=created[0].terminate) as terminate:
                    worker.close()
                    worker.thread.join(5)
                    self.assertFalse(worker.thread.is_alive())
                    terminate.assert_not_called()
                self.assertEqual(created[0].returncode, 0)
                self.assertTrue(worker.updates.empty())
            finally:
                for process in created:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                worker.thread.join(5)


class WindowPipeTests(unittest.TestCase):
    def test_only_expected_disconnected_pipe_errors_are_accepted(self):
        process = Mock()
        process.wait.return_value = 0
        with patch.object(gadget, "os", SimpleNamespace(name="nt")):
            self.assertTrue(gadget.closed_process_pipe(process, OSError(errno.EINVAL, "closed pipe")))
            self.assertFalse(gadget.closed_process_pipe(process, OSError(errno.EIO, "I/O failure")))
            process.wait.side_effect = subprocess.TimeoutExpired("window", .5)
            self.assertFalse(gadget.closed_process_pipe(process, OSError(errno.EINVAL, "still alive")))
        with patch.object(gadget, "os", SimpleNamespace(name="posix")):
            self.assertFalse(gadget.closed_process_pipe(process, OSError(errno.EINVAL, "invalid argument")))
            self.assertTrue(gadget.closed_process_pipe(process, BrokenPipeError()))

    def run_race(self, failure_stage, error_number=errno.EINVAL, exit_code=0, alive=False):
        window = Mock()
        window.returncode = None
        window.poll.side_effect = lambda: window.returncode
        monitor = Mock()
        monitor.updates = queue.Queue()
        monitor.updates.put(("Windows", {"sessions": [], "errors": []}, time.monotonic()))

        def fail(*args):
            if not alive:
                window.returncode = exit_code
            raise OSError(error_number, "pipe operation failed")

        def wait(timeout=None):
            if alive and timeout == .5:
                raise subprocess.TimeoutExpired("window", timeout)
            window.returncode = exit_code
            return exit_code

        window.wait.side_effect = wait
        getattr(window.stdin, failure_stage).side_effect = fail
        # A failed buffered flush is retried by close(), matching Windows Python.
        if failure_stage == "flush":
            window.stdin.close.side_effect = fail
        if failure_stage == "close":
            window.poll.side_effect = [None, 0]

        with patch.object(gadget, "os", SimpleNamespace(name="nt")), \
                patch.object(gadget, "hidden_options", return_value={}), \
                patch.object(gadget, "load_config", return_value={"wsl_distros": []}), \
                patch.object(gadget, "build_window", return_value="test-window"), \
                patch.object(gadget, "Monitor", return_value=monitor), \
                patch.object(gadget.subprocess, "Popen", return_value=window):
            try:
                gadget.run_window()
            finally:
                monitor.close.assert_called_once()
                monitor.thread.join.assert_called_once_with(timeout=20)
                window.stdin.close.assert_called_once()
                self.assertIn(unittest.mock.call(timeout=5), window.wait.call_args_list)

    def test_windows_exit_races_in_write_flush_and_close_are_clean(self):
        for stage in ("write", "flush", "close"):
            with self.subTest(stage=stage):
                self.run_race(stage)

    def test_real_io_errors_are_not_hidden_and_still_run_cleanup(self):
        for stage in ("write", "flush", "close"):
            with self.subTest(stage=stage):
                with self.assertRaises(OSError) as caught:
                    self.run_race(stage, error_number=errno.EIO)
                self.assertEqual(caught.exception.errno, errno.EIO)

    def test_invalid_argument_with_live_receiver_is_not_hidden(self):
        with self.assertRaises(OSError) as caught:
            self.run_race("flush", alive=True)
        self.assertEqual(caught.exception.errno, errno.EINVAL)

    def test_nonzero_window_exit_remains_an_error(self):
        with self.assertRaisesRegex(OSError, "Desktop window exited with code 17"):
            self.run_race("flush", exit_code=17)

    def test_worker_close_accepts_windows_peer_exit(self):
        worker = gadget.WslWorker("test", queue.Queue())
        worker.process = Mock()
        worker.process.stdin.closed = False
        worker.process.stdin.close.side_effect = OSError(errno.EINVAL, "closed pipe")
        worker.process.wait.return_value = 0
        worker.bootstrap_sent.set()
        with patch.object(gadget, "os", SimpleNamespace(name="nt")):
            worker.close()
        self.assertTrue(worker.stopping.is_set())
        worker.process.wait.assert_any_call(timeout=3)
        worker.process.terminate.assert_not_called()

    def test_failed_worker_cleanup_preserves_collector_error(self):
        worker = gadget.WslWorker("test", queue.Queue())
        process = Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.stdin.write.side_effect = OSError(errno.EINVAL, "closed pipe")
        process.stdin.close.side_effect = OSError(errno.EINVAL, "closed pipe")
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        with patch.object(gadget, "os", SimpleNamespace(name="nt")), \
                patch.object(gadget, "hidden_options", return_value={}), \
                patch.object(gadget.subprocess, "Popen", return_value=process):
            worker.run()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertIn("closed pipe", worker.updates.get_nowait()[1]["errors"][0])
        self.assertTrue(worker.updates.empty())


@unittest.skipUnless(os.name == "nt", "Native Windows gadget UI")
class WindowTests(unittest.TestCase):
    def test_window_close_during_update_exits_backend_cleanly(self):
        import ctypes
        from ctypes import wintypes

        executable = gadget.build_window()
        monitor = gadget.Monitor(wsl_distros=[])
        monitor.thread = threading.Thread(target=lambda: None)
        launched = threading.Event()
        created = []
        close_errors = []
        original_popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            created.append(process)
            launched.set()
            return process

        def close_window():
            try:
                if not launched.wait(10):
                    raise AssertionError("Native window was not launched")
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
                user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
                user32.IsWindowVisible.argtypes = [wintypes.HWND]
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                handles = []

                @callback_type
                def find_window(hwnd, _):
                    owner = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                    if owner.value == created[0].pid and user32.IsWindowVisible(hwnd):
                        handles.append(hwnd)
                    return True

                deadline = time.monotonic() + 10
                while not handles and time.monotonic() < deadline:
                    user32.EnumWindows(find_window, 0)
                    time.sleep(.02)
                if not handles or not user32.PostMessageW(handles[0], 0x10, 0, 0):  # WM_CLOSE
                    raise AssertionError("Could not request native window closure")
                created[0].wait(timeout=10)
                # Deliver an update after WM_CLOSE while run_window is awaiting
                # its queue: reproduces the real Windows broken-pipe race.
                monitor.updates.put(("Windows", {"sessions": [], "errors": []}, time.monotonic()))
            except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
                close_errors.append(error)
                for process in created:
                    if process.poll() is None:
                        process.kill()

        closer = threading.Thread(target=close_window, daemon=True)
        with patch.object(gadget, "load_config", return_value={"wsl_distros": []}), \
                patch.object(gadget, "build_window", return_value=executable), \
                patch.object(gadget, "Monitor", return_value=monitor), \
                patch.object(gadget.subprocess, "Popen", side_effect=launch):
            closer.start()
            try:
                gadget.run_window()
            finally:
                closer.join(timeout=20)
                for process in created:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
        self.assertFalse(closer.is_alive())
        self.assertEqual(close_errors, [])
        self.assertTrue(monitor.stop.is_set())
        self.assertTrue(created[0].stdin.closed)
        self.assertEqual(created[0].returncode, 0)

    @unittest.skipUnless(os.environ.get("COPILOT_GADGET_LIVE_TEST") == "1",
                         "Opt-in: requires live Windows and WSL Copilot sessions")
    def test_live_windows_and_wsl_feed_and_shutdown(self):
        monitor = gadget.Monitor()
        monitor.thread.start()
        ready = {}
        deadline = time.monotonic() + 45
        try:
            while time.monotonic() < deadline:
                try:
                    source, snapshot, _ = monitor.updates.get(timeout=2)
                except queue.Empty:
                    continue
                if snapshot and snapshot["sessions"] and not snapshot["errors"]:
                    statuses = [row["status"] for row in snapshot["sessions"]]
                    if all(status in ("In progress", "Needs input", "Done") for status in statuses):
                        ready[source] = statuses
                if "Windows" in ready and any(source.startswith("WSL:") for source in ready):
                    break
            self.assertIn("Windows", ready)
            self.assertTrue(any(source.startswith("WSL:") for source in ready), ready)
            print("Live gadget sources:", ready)
        finally:
            monitor.close()
            monitor.thread.join(timeout=20)
        self.assertFalse(monitor.thread.is_alive())
        for worker in monitor.workers.values():
            worker.thread.join(timeout=5)
            self.assertFalse(worker.thread.is_alive())
            self.assertIsNotNone(worker.process.poll())

    def test_rows_topmost_selection_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "GadgetUiTests.exe"
            command = gadget.compiler_command(output, [ROOT / "tests" / "GadgetUiTests.cs"])
            framework = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF"
            command += ["/main:GadgetUiTests",
                        "/reference:" + str(framework / "UIAutomationProvider.dll"),
                        "/reference:" + str(framework / "UIAutomationTypes.dll")]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_window_consumes_protocol_and_closes_on_eof(self):
        executable = gadget.build_window()
        with subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as process:
            try:
                _, errors = process.communicate(json.dumps({"rows": [], "errors": [],
                                                           "discovering": False}).encode() + b"\n", timeout=20)
                self.assertEqual(process.returncode, 0, errors)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("Native window did not close after collector EOF")
