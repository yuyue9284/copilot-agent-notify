import base64
import json
import io
import errno
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import session_gadget as gadget
import session_probe as probe
from gadget_test_support import (BOOTSTRAP, COLLECTORS, build_window, close_native_window,
                                 isolated_environment, temporary_directory)


def event(kind, **data):
    return {"type": kind, "data": data}


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
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

    def sdk_snapshot(self, directory, **overrides):
        value = dict(protocol_version=1, session_id=directory.name, owner_pid=42,
                     updated_at=time.time(), state="ready", pending_shells=0, settling=False)
        value.update(overrides)
        (directory / "gadget-sdk.json").write_text(json.dumps(value))

    def test_sdk_pending_shell_overrides_idle_transcript_and_settles(self):
        directory = self.session()
        self.sdk_snapshot(directory, pending_shells=1)
        self.assertEqual(self.status(), "In progress")
        self.sdk_snapshot(directory, settling=True)
        self.assertEqual(self.status(), "In progress")
        self.sdk_snapshot(directory)
        self.assertEqual(self.status(), "Done")

    def test_sdk_idle_does_not_override_root_work_or_attention(self):
        directory = self.session(events=[event("assistant.turn_start")])
        self.sdk_snapshot(directory)
        self.assertEqual(self.status(), "In progress")
        self.sdk_snapshot(directory, pending_shells=1)
        self.write(directory, [event("hook.start", hookType="notification", input={
            "notification_type": "permission_prompt", "sessionId": "root"})], "a")
        self.assertEqual(self.status(), "Needs input")

    def test_sdk_stale_unavailable_and_invalid_data_report_unknown(self):
        directory = self.session()
        for overrides in (
                {"updated_at": time.time() - 16}, {"updated_at": time.time() + 30},
                {"updated_at": float("nan")}, {"state": "task_query_failed"},
                {"state": "stopped"}, {"state": "starting"},
                {"owner_pid": 99}, {"session_id": "other"},
                {"pending_shells": -1}, {"pending_shells": True},
                {"settling": "false"}, {"protocol_version": 2}):
            with self.subTest(overrides=overrides):
                self.sdk_snapshot(directory, **overrides)
                snapshot = self.scanner.snapshot()
                self.assertEqual(snapshot["sessions"][0]["status"], "Unknown")
                self.assertTrue(any("SDK bridge:" in error for error in snapshot["errors"]))
        self.sdk_snapshot(directory, pending_shells=1)
        self.assertEqual(self.status(), "In progress")

    def test_sdk_null_or_malformed_file_is_not_absence(self):
        directory = self.session()
        for contents in ("null", "{", "[]"):
            (directory / "gadget-sdk.json").write_text(contents)
            snapshot = self.scanner.snapshot()
            self.assertEqual(snapshot["sessions"][0]["status"], "Unknown")
            self.assertTrue(snapshot["errors"])

    def test_sdk_scope_does_not_affect_other_sessions_or_publish_private_fields(self):
        directory = self.session()
        self.session("other")
        self.sdk_snapshot(directory, pending_shells=1, command="synthetic-private-command")
        snapshot = self.scanner.snapshot()
        self.assertEqual({row["id"]: row["status"] for row in snapshot["sessions"]},
                         {"root": "In progress", "other": "Done"})
        self.assertNotIn("synthetic-private-command", json.dumps(snapshot))

    def test_sdk_failure_retains_transcript_reader_and_legacy_override_is_live(self):
        directory = self.session()
        self.sdk_snapshot(directory, pending_shells=1)
        self.assertEqual(self.status(), "In progress")
        reader = next(iter(self.scanner.readers.values()))[0]
        self.sdk_snapshot(directory, state="stopped")
        self.assertEqual(self.scanner.snapshot()["sessions"][0]["status"], "Unknown")
        self.assertIs(reader, next(iter(self.scanner.readers.values()))[0])
        (self.home / "copilot-agent-notify.json").write_text('{"status_backend":"legacy"}')
        self.assertEqual(self.status(), "Done")
        self.assertIs(reader, next(iter(self.scanner.readers.values()))[0])

    def test_sdk_disappearance_is_unknown_until_explicit_override(self):
        directory = self.session()
        self.sdk_snapshot(directory)
        self.assertEqual(self.status(), "Done")
        (directory / "gadget-sdk.json").unlink()
        self.assertEqual(self.scanner.snapshot()["sessions"][0]["status"], "Unknown")
        (self.home / "copilot-agent-notify.json").write_text('{"status_backend":"legacy"}')
        self.assertEqual(self.status(), "Done")

    def test_busy_done_resume_input_and_process_close(self):
        directory = self.session(events=[event("assistant.turn_start")])
        self.assertEqual(self.status(), "In progress")
        reader = next(iter(self.scanner.readers.values()))[0]
        self.write(directory, [event("assistant.message", turnId="1"),
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

    def child_event(self, kind, child="child", **data):
        return dict(event(kind, **data), agentId=child)

    def test_background_completion_holds_status_until_exact_deadline(self):
        directory = self.session(events=[self.child_event("assistant.turn_start")])
        with patch.object(probe.time, "monotonic", return_value=100) as clock:
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("assistant.message", turnId="1",
                                                    phase="final_answer"),
                                   self.child_event("assistant.turn_end", turnId="1")], "a")
            self.assertEqual(self.status(), "In progress")
            revision = self.scanner.snapshot()["sessions"][0]["activity_revision"]
            clock.return_value = 109.999
            self.write(directory, [self.child_event("subagent.completed"),
                                   event("session.info")], "a")
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 110
            self.assertEqual(self.status(), "Done")
            self.assertEqual(self.scanner.snapshot()["sessions"][0]["activity_revision"], revision)
            clock.return_value = 120
            self.assertEqual(self.status(), "Done")

    def test_parent_resume_cancels_grace_and_root_completion_is_immediate(self):
        directory = self.session(events=[self.child_event("assistant.turn_start")])
        with patch.object(probe.time, "monotonic", return_value=100) as clock:
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("subagent.completed")], "a")
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 105
            self.write(directory, [event("assistant.turn_start", turnId="parent")], "a")
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [event("assistant.message", turnId="parent", phase="final_answer"),
                                   event("assistant.turn_end", turnId="parent")], "a")
            self.assertEqual(self.status(), "Done")

    def test_background_grace_does_not_hide_input_or_new_user_work(self):
        directory = self.session(events=[self.child_event("assistant.turn_start")])
        with patch.object(probe.time, "monotonic", return_value=100) as clock:
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("subagent.completed"),
                                   event("hook.start", hookType="notification", input={
                                       "notification_type": "permission_prompt",
                                       "sessionId": "root"})], "a")
            self.assertEqual(self.status(), "Needs input")
            self.write(directory, [event("user.message", delivery="idle", source="user")], "a")
            clock.return_value = 120
            self.assertEqual(self.status(), "In progress")

    def test_queued_child_resume_starts_a_fresh_completion_grace(self):
        directory = self.session(events=[self.child_event("assistant.turn_start")])
        with patch.object(probe.time, "monotonic", return_value=100) as clock:
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("subagent.completed")], "a")
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 105
            self.write(directory, [self.child_event("assistant.turn_start")], "a")
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 111
            self.write(directory, [self.child_event("subagent.failed")], "a")
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 120.999
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 121
            self.assertEqual(self.status(), "Done")

    def test_only_last_child_completion_starts_grace_including_stop_hooks(self):
        directory = self.session(events=[self.child_event("assistant.turn_start"),
                                         self.child_event("assistant.turn_start", child="other")])
        with patch.object(probe.time, "monotonic", return_value=100) as clock:
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("subagent.completed")], "a")
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 120
            self.write(directory, [self.child_event("assistant.message", child="other"),
                                   event("hook.start", hookType="agentStop", hookInvocationId="stop",
                                         input={"sessionId": "other"}),
                                   event("hook.end", hookInvocationId="stop", success=True)], "a")
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 129.999
            self.assertEqual(self.status(), "In progress")
            clock.return_value = 130
            self.assertEqual(self.status(), "Done")

    def test_historical_child_completion_and_rewrite_do_not_start_grace(self):
        history = [event("session.start", sessionId="root"),
                   self.child_event("assistant.turn_start"), self.child_event("subagent.completed")]
        directory = self.session(events=history[1:])
        self.assertEqual(self.status(), "Done")
        self.write(directory, [self.child_event("assistant.turn_start")], "a")
        self.assertEqual(self.status(), "In progress")
        self.write(directory, [self.child_event("subagent.completed")], "a")
        self.assertEqual(self.status(), "In progress")
        self.assertEqual(probe.Scanner(self.home).snapshot()["sessions"][0]["status"], "Done")
        self.write(directory, history)
        self.assertEqual(self.status(), "Done")

    def test_child_finishing_while_root_busy_does_not_delay_root_completion(self):
        directory = self.session(events=[event("assistant.turn_start", turnId="parent"),
                                         self.child_event("assistant.turn_start")])
        with patch.object(probe.time, "monotonic", return_value=100):
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("subagent.completed"),
                                   event("assistant.message", turnId="parent", phase="final_answer"),
                                   event("assistant.turn_end", turnId="parent")], "a")
            self.assertEqual(self.status(), "Done")

    def test_abort_and_shutdown_clear_pending_background_completion(self):
        directory = self.session(events=[self.child_event("assistant.turn_start")])
        with patch.object(probe.time, "monotonic", return_value=100):
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("subagent.completed"), event("abort")], "a")
            self.assertEqual(self.status(), "Done")
            self.write(directory, [self.child_event("assistant.turn_start")], "a")
            self.assertEqual(self.status(), "In progress")
            self.write(directory, [self.child_event("subagent.completed"),
                                   event("session.shutdown")], "a")
            self.assertEqual(self.scanner.snapshot()["sessions"], [])

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


class CollectorTests(unittest.TestCase):
    def run_collector(self, args=(), bootstrap=False, input_data=b""):
        with temporary_directory() as directory:
            command = [sys.executable, "-u"]
            if bootstrap:
                command += ["-c", BOOTSTRAP]
                sources = {name: (ROOT / "scripts" / (name + ".py")).read_text(encoding="utf-8")
                           for name in COLLECTORS}
                input_data = (json.dumps(sources) + "\n").encode() + input_data
            else:
                command += [str(ROOT / "scripts" / "session_probe.py"), *args]
            return subprocess.run(command, input=input_data, capture_output=True,
                                  env=isolated_environment(directory), timeout=10)

    def test_one_shot_shape_is_unchanged(self):
        result = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"sessions": [], "errors": []})

    def test_watch_is_versioned_and_exits_on_stdin_eof(self):
        result = self.run_collector(["--watch"], input_data=b"ignored input without newline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        snapshots = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(snapshots)
        self.assertTrue(all(value == {"protocol_version": 1, "sessions": [], "errors": []}
                            for value in snapshots))

    def test_bundled_worker_streams_and_exits_on_input_close(self):
        result = self.run_collector(bootstrap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"protocol_version": 1, "sessions": [], "errors": []})

    def test_watch_streams_repeated_snapshots_until_eof(self):
        with temporary_directory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "scripts" / "session_probe.py"), "--watch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=isolated_environment(directory))
            lines = []
            ready = threading.Event()

            def read():
                for line in process.stdout:
                    lines.append(json.loads(line))
                    if len(lines) >= 2:
                        ready.set()

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                self.assertTrue(ready.wait(10), "Collector did not continue publishing")
                self.assertIsNone(process.poll())
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0)
                reader.join(5)
                self.assertFalse(reader.is_alive())
                self.assertTrue(all(value["protocol_version"] == 1 for value in lines))
                self.assertEqual(process.stderr.read(), b"")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                reader.join(5)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()

    def exercise_watch(self, scanner_error=None, input_error=None, output_error=None,
                       snapshot=None, snapshot_error=None):
        output = []
        scanner = Mock()
        scanner.snapshot.return_value = snapshot or {"sessions": [], "errors": []}
        scanner.snapshot.side_effect = snapshot_error
        fake_input = Mock()
        fake_input.fileno.return_value = 123
        fake_output = Mock()
        fake_output.fileno.return_value = 124

        def write(_, data):
            if output_error:
                raise output_error
            output.append(data)
            return len(data)

        with patch.object(probe, "Scanner", side_effect=scanner_error, return_value=scanner), \
                patch.object(probe.sys, "stdin", fake_input), \
                patch.object(probe.sys, "stdout", fake_output), \
                patch.object(probe.os, "read", side_effect=input_error, return_value=b""), \
                patch.object(probe, "write_pipe", side_effect=write), \
                patch.object(probe.sys, "stderr", io.StringIO()) as errors:
            status = probe.watch()
        return status, [json.loads(line) for line in output], errors.getvalue()

    def test_startup_error_is_protocol_data_and_nonzero(self):
        status, output, _ = self.exercise_watch(scanner_error=OSError("fixture startup"))
        self.assertEqual(status, 1)
        self.assertEqual(output[0]["protocol_version"], 1)
        self.assertIn("fixture startup", output[0]["errors"][0])

    def test_snapshot_failure_is_visible_not_false_empty_success(self):
        status, output, _ = self.exercise_watch(snapshot_error=OSError("fixture scan"))
        self.assertEqual(status, 0)
        self.assertEqual(output[0]["sessions"], [])
        self.assertIn("fixture scan", output[0]["errors"][0])

    def test_watch_preserves_exact_nullable_revision_and_status(self):
        snapshot = {"sessions": [
            {"id": "first", "activity_revision": None, "status": "Loading"},
            {"id": "second", "activity_revision": "", "status": "Done"},
            {"id": "third", "activity_revision": "00000000000000000001/EXACT", "status": "Needs input"}
        ], "errors": []}
        status, output, _ = self.exercise_watch(snapshot=snapshot)
        self.assertEqual(status, 0)
        self.assertEqual(output[0], dict(snapshot, protocol_version=1))
        self.assertNotIn("protocol_version", snapshot)

    def test_stdin_io_failure_is_not_normal_eof(self):
        status, output, _ = self.exercise_watch(input_error=OSError(errno.EIO, "fixture input"))
        self.assertEqual(status, 1)
        self.assertIn("fixture input", output[-1]["errors"][0])

    def test_only_broken_output_pipe_is_normal_shutdown(self):
        self.assertEqual(self.exercise_watch(output_error=BrokenPipeError(errno.EPIPE, "closed"))[0], 0)
        for number in (errno.EIO, errno.EINVAL):
            status, _, errors = self.exercise_watch(output_error=OSError(number, "fixture output"))
            self.assertEqual(status, 1)
            self.assertIn("fixture output", errors)

    def test_real_closed_output_pipe_exits_without_buffered_shutdown_error(self):
        with temporary_directory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "scripts" / "session_probe.py"), "--watch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=isolated_environment(directory))
            try:
                process.stdout.close()
                self.assertEqual(process.wait(timeout=7), 0)
                self.assertEqual(process.stderr.read(), b"")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdin.close()
                process.stderr.close()


class CompatibilityLauncherTests(unittest.TestCase):
    def test_resolves_repository_and_installed_prebuilt_locations(self):
        with temporary_directory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            with patch.object(gadget, "__file__", str(scripts / "session_gadget.py")):
                self.assertEqual(gadget.application_path(),
                                 root / "windows-helper" / "gadget" / "bin" / "Release" / "CopilotSessions.exe")
                installed = scripts / "CopilotSessions.exe"
                installed.touch()
                self.assertEqual(gadget.application_path(), installed)

    def test_missing_build_is_actionable_and_does_not_compile(self):
        with temporary_directory() as directory, \
                patch.object(gadget, "application_path", return_value=Path(directory) / "missing.exe"), \
                patch.object(gadget.subprocess, "run") as run:
            with self.assertRaisesRegex(FileNotFoundError, "build-gadget.ps1"):
                gadget.run_window()
            run.assert_not_called()

    def test_launches_prebuilt_app_with_native_python_without_pipe(self):
        with temporary_directory() as directory:
            executable = Path(directory) / "CopilotSessions.exe"
            executable.touch()
            with patch.object(gadget, "application_path", return_value=executable), \
                    patch.object(gadget.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0)) as run:
                gadget.run_window()
                run.assert_called_once_with([str(executable), "--python", sys.executable])

    def test_nonzero_native_exit_is_reported(self):
        with temporary_directory() as directory:
            executable = Path(directory) / "CopilotSessions.exe"
            executable.touch()
            with patch.object(gadget, "application_path", return_value=executable), \
                    patch.object(gadget.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 17)):
                with self.assertRaisesRegex(OSError, "code 17"):
                    gadget.run_window()

    def test_snapshot_compatibility_does_not_launch(self):
        with patch.object(sys, "argv", ["session_gadget.py", "--snapshot"]), \
                patch.object(gadget, "Scanner") as scanner, \
                patch.object(gadget.subprocess, "run") as run, \
                patch.object(sys, "stdout", io.StringIO()) as output:
            scanner.return_value.snapshot.return_value = {"sessions": [], "errors": []}
            self.assertEqual(gadget.main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {"sessions": [], "errors": []})
            run.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Native Windows gadget host and WPF")
class WindowTests(unittest.TestCase):
    def test_notification_helper_registers_shortcut_identity(self):
        with temporary_directory() as directory:
            shortcut = Path(directory) / "Copilot Sessions Identity Test.lnk"
            helper = ROOT / "windows-helper" / "bin" / "copilot-notify.exe"
            app_id = "CopilotAgentNotify.CopilotSessions.Tests"
            result = subprocess.run([
                str(helper), "--install-shortcut", str(shortcut), str(helper),
                "--identity-test", str(directory), str(helper), app_id, "Identity test",
            ], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(shortcut.is_file())
            powershell = (Path(os.environ["WINDIR"]) / "System32" / "WindowsPowerShell" /
                          "v1.0" / "powershell.exe")
            script = (
                "$shell = New-Object -ComObject Shell.Application; "
                "$folder = $shell.Namespace($env:SHORTCUT_DIRECTORY); "
                "$item = $folder.ParseName($env:SHORTCUT_NAME); "
                "$item.ExtendedProperty('System.AppUserModel.ID')")
            environment = dict(os.environ, SHORTCUT_DIRECTORY=str(shortcut.parent),
                               SHORTCUT_NAME=shortcut.name)
            result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive",
                                     "-Command", script], capture_output=True, text=True,
                                    env=environment, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), app_id)

    def test_installer_python_probe_roundtrips_through_windows_powershell(self):
        lines = (ROOT / "windows-helper" / "install-gadget.ps1").read_text(encoding="utf-8").splitlines()
        assignment = next(line for line in lines if line.startswith("$probe = "))
        invocation = next(line.strip() for line in lines
                          if line.strip().startswith("$python = & $Python "))
        powershell = (Path(os.environ["WINDIR"]) / "System32" / "WindowsPowerShell" /
                      "v1.0" / "powershell.exe")

        def literal(value):
            return "'" + value.replace("'", "''") + "'"

        for prefix, expected in (("", 0), ("import os; os.name='posix'; ", 1),
                                 ("import sys; sys.version_info=(3, 8); ", 1)):
            with self.subTest(prefix=prefix):
                script = "\n".join((
                    "$ErrorActionPreference = 'Stop'",
                    "$ProgressPreference = 'SilentlyContinue'",
                    "$Python = " + literal(sys.executable),
                    assignment,
                    "$probe = " + literal(prefix) + " + $probe",
                    invocation,
                    "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
                    "Write-Output $python",
                ))
                # Encode only the outer PowerShell command. The installer still
                # passes its exact probe to native Python using ordinary -c.
                encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                result = subprocess.run(
                    [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if expected == 0:
                    self.assertEqual(os.path.normcase(result.stdout.strip()),
                                     os.path.normcase(sys.executable))
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertIn("Windows Python 3.9+ is required", result.stderr)
                    self.assertNotIn("SyntaxError", result.stderr)

    def test_host_source_worker_and_pipe_regressions(self):
        # GadgetHostTests replaces SourceTests (config/discovery/aggregation),
        # WorkerLifecycleTests (creation/cancellation/bootstrap/EOF), and
        # WindowPipeTests (peer-close vs genuine I/O/process failures).
        # Do not skip a missing runner on Windows: integration must supply it.
        self.assertTrue((ROOT / "tests" / "GadgetHostTests.cs").is_file())
        with temporary_directory() as directory:
            output = build_window(directory, "GadgetHostTests")
            result = subprocess.run([str(output)], capture_output=True, text=True,
                                    env=isolated_environment(directory), timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rows_topmost_selection_and_close(self):
        with temporary_directory() as directory:
            output = build_window(directory, "GadgetUiTests")
            result = subprocess.run([str(output)], capture_output=True, text=True,
                                    env=isolated_environment(directory), timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_window_consumes_protocol_and_closes_on_eof(self):
        with temporary_directory() as directory:
            executable = build_window(directory)
            with subprocess.Popen([str(executable), "--collector-stdin"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=isolated_environment(directory)) as process:
                try:
                    _, errors = process.communicate(json.dumps({"rows": [], "errors": [],
                                                               "discovering": False}).encode() + b"\n",
                                                    timeout=20)
                    self.assertEqual(process.returncode, 0, errors)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    self.fail("Native window did not close after collector EOF")

    def test_window_close_with_open_collector_pipe_exits_cleanly(self):
        with temporary_directory() as directory:
            executable = build_window(directory)
            for _ in range(3):
                process = subprocess.Popen(
                    [str(executable), "--collector-stdin"], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=isolated_environment(directory))
                try:
                    process.stdin.write(b'{"rows":[],"errors":[],"discovering":false}\n')
                    process.stdin.flush()
                    close_native_window(process)
                    self.assertEqual(process.wait(timeout=10), 0)
                    self.assertEqual(process.stderr.read(), b"")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    process.stdin.close()
                    process.stdout.close()
                    process.stderr.close()
