import json
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


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

    def test_newest_root_wins_after_resume_and_empty_launch_is_ignored(self):
        old = self.session("old")
        os.utime(old / "inuse.42.lock", (time.time() - 20, time.time() - 20))
        self.session("resumed")
        empty = self.session("empty")
        (empty / "events.jsonl").unlink()
        self.assertEqual([row["id"] for row in self.scanner.snapshot()["sessions"]], ["resumed"])

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


@unittest.skipUnless(os.name == "nt", "Native Windows gadget UI")
class WindowTests(unittest.TestCase):
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
