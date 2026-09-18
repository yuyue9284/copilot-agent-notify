import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("activity", ROOT / "scripts/activity.py")
activity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activity)


def event(kind, agent=None, **data):
    result = {"type": kind, "data": data}
    if agent:
        result["agentId"] = agent
    return result


def start(session="root"):
    return event("session.start", sessionId=session)


def busy():
    return event("assistant.turn_start", turnId="0")


def finish(session="root", invocation="stop"):
    return [
        event("assistant.message", toolRequests=[]),
        event("hook.start", hookType="agentStop", hookInvocationId=invocation,
              input={"sessionId": session}),
        event("hook.end", hookType="agentStop", hookInvocationId=invocation, success=True),
    ]


class Files(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "tests" / (".runtime-" + uuid.uuid4().hex)
        self.directory.mkdir()

    def tearDown(self):
        shutil.rmtree(self.directory)

    def write_events(self, path, events, mode="w"):
        with path.open(mode, encoding="utf-8") as stream:
            for item in events:
                stream.write(json.dumps(item) + "\n")


class StatusProviderTests(Files):
    def setUp(self):
        super().setUp()
        self.home = self.directory / "home"
        self.session = self.home / "session-state" / "synthetic"
        self.session.mkdir(parents=True)
        self.env = patch.dict(os.environ, COPILOT_NOTIFY_STATUS_BACKEND="")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.provider = activity.StatusProvider(self.home)
        self.provider.refresh()

    def snapshot(self, **changes):
        value = dict(protocol_version=1, session_id="synthetic", owner_pid=42,
                     updated_at=time.time(), state="ready", pending_shells=1, settling=False)
        value.update(changes)
        activity.atomic_json(self.session / "gadget-sdk.json", value)

    def counts(self, baseline=(0, 0)):
        return self.provider.counts(self.session, "synthetic", 42, baseline)

    def test_auto_uses_legacy_without_bridge_and_merges_sdk_activity(self):
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.counts((1, 1)), (1, 1))
        self.snapshot()
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(self.counts((0, 1)), (1, 1))
        self.snapshot(pending_shells=0, settling=True)
        self.assertEqual(self.counts(), (1, 0))
        self.snapshot(pending_shells=0)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.counts((1, 0)), (1, 0))

    def test_config_legacy_override_and_environment_precedence(self):
        config = self.home / "copilot-agent-notify.json"
        activity.atomic_json(config, {"hook_alerts": False, "status_backend": "legacy"})
        self.snapshot(state="stopped")
        self.provider.refresh()
        self.assertEqual(self.counts(), (0, 0))
        with patch.dict(os.environ, COPILOT_NOTIFY_STATUS_BACKEND="auto"):
            self.provider.refresh()
            with self.assertRaisesRegex(ValueError, "SDK bridge"):
                self.counts()
        config.write_text("invalid")
        with patch.dict(os.environ, COPILOT_NOTIFY_STATUS_BACKEND="legacy"):
            self.provider.refresh()
            self.assertEqual(self.counts(), (0, 0))

    def test_invalid_configuration_is_reported_and_recovers(self):
        config = self.home / "copilot-agent-notify.json"
        for value in (None, [], {"status_backend": "sdk"}, {"status_backend": False}):
            activity.atomic_json(config, value)
            self.provider.refresh()
            with self.assertRaisesRegex(ValueError, "Status backend:"):
                self.counts()
        activity.atomic_json(config, {})
        self.provider.refresh()
        self.assertEqual(self.counts(), (0, 0))

    def test_windows_utf8_bom_configuration_is_supported(self):
        config = self.home / "copilot-agent-notify.json"
        config.write_text('{"hook_alerts":false,"status_backend":"legacy"}', encoding="utf-8-sig")
        self.snapshot(state="stopped")
        self.provider.refresh()
        self.assertEqual(self.counts(), (0, 0))
        self.assertIsNone(self.provider.configuration_error)

    def test_bridge_loss_does_not_silently_fall_back(self):
        self.snapshot()
        self.assertEqual(self.counts(), (1, 0))
        (self.session / "gadget-sdk.json").unlink()
        with self.assertRaisesRegex(ValueError, "disappeared"):
            self.counts()
        self.assertEqual(self.provider.counts(self.session, "other", 99, (0, 0)), (0, 0))
        self.provider.retain(set())
        self.assertEqual(self.counts(), (0, 0))

    def test_freshness_boundary_and_untrusted_values(self):
        with patch.object(activity.time, "time", return_value=100):
            self.snapshot(updated_at=85)
            self.assertEqual(self.counts(), (1, 0))
            self.snapshot(updated_at=84.999)
            with self.assertRaisesRegex(ValueError, "stale"):
                self.counts()
            for value in (10 ** 400, float("nan"), float("inf"), True):
                self.snapshot(updated_at=value)
                with self.assertRaisesRegex(ValueError, "invalid snapshot"):
                    self.counts()
        self.snapshot()
        self.assertEqual(self.counts(), (1, 0))


class ReducerTests(unittest.TestCase):
    def setUp(self):
        self.state = activity.Activity("root")

    def feed(self, *events):
        for item in events:
            self.state.event(item)
        return self.state.counts()

    def test_finish_requires_stop_and_final_in_either_order(self):
        self.assertEqual(self.feed(busy(), finish()[0]), (1, 0))
        self.assertEqual(self.feed(*finish()[1:]), (0, 0))
        self.assertEqual(self.feed(busy(), *finish()[1:]), (1, 0))
        self.assertEqual(self.feed(finish()[0]), (0, 0))

    def test_tool_cycle_end_does_not_clear(self):
        self.assertEqual(self.feed(busy(), event("assistant.message", toolRequests=[{}]),
                                   event("assistant.turn_end")), (1, 0))

    def test_matching_final_message_and_turn_end_without_stop_hook(self):
        for phase in (None, "final_answer"):
            self.state.reset()
            message = event("assistant.message", turnId="5", toolRequests=[])
            if phase:
                message["data"]["phase"] = phase
            self.assertEqual(self.feed(busy(), message), (1, 0))
            self.assertEqual(self.feed(event("assistant.turn_end", turnId="5")), (0, 0))
            self.assertEqual(self.feed(busy()), (1, 0))

    def test_final_fallback_requires_matching_message_turn(self):
        for phase in (None, "final_answer"):
            self.state.reset()
            self.assertEqual(self.feed(busy(), event("assistant.message", phase=phase,
                                                    turnId="5", toolRequests=[]),
                                       event("assistant.turn_end", turnId="other")), (1, 0))
            self.assertEqual(self.feed(event("assistant.turn_end", turnId="5")), (0, 0))
        self.state.reset()
        self.assertEqual(self.feed(busy(), event("assistant.message", phase="commentary",
                                                turnId="5", toolRequests=[]),
                                   event("assistant.turn_end", turnId="5")), (1, 0))

    def test_later_tool_message_cancels_unmarked_final_candidate(self):
        self.assertEqual(self.feed(
            busy(),
            event("assistant.message", turnId="5", toolRequests=[]),
            event("assistant.message", turnId="5", toolRequests=[{}]),
            event("assistant.turn_end", turnId="5"),
        ), (1, 0))

    def test_final_fallback_preserves_children_and_pending_hooks(self):
        self.feed(busy(), event("subagent.started", agent="child"),
                  event("assistant.message", turnId="5", toolRequests=[]))
        self.assertEqual(self.feed(event("assistant.turn_end", turnId="5")), (1, 0))
        self.assertEqual(self.feed(event("subagent.completed", agent="child")), (0, 0))
        self.feed(busy(), event("assistant.message", phase="final_answer", turnId="6"),
                  finish()[1])
        self.assertEqual(self.feed(event("assistant.turn_end", turnId="6")), (1, 0))
        self.assertEqual(self.feed(finish()[2]), (0, 0))

    def test_final_fallback_rejects_child_end_and_running_root_tool(self):
        self.feed(busy(), event("assistant.message", turnId="5"))
        self.assertEqual(self.feed(event("assistant.turn_end", agent="child", turnId="5")), (1, 0))
        self.feed(event("tool.execution_start", toolCallId="running"))
        self.assertEqual(self.feed(event("assistant.turn_end", turnId="5")), (1, 0))

    def test_failed_stop_hook_does_not_keep_finished_response_busy(self):
        failed_stop = event("hook.end", hookInvocationId="stop", hookType="agentStop",
                            success=False, error={"message": "Hook command failed with code 126"})
        with self.assertLogs("copilot-notify-icons", level="WARNING"):
            self.assertEqual(self.feed(busy(), *finish()[:2], failed_stop), (0, 0))
        self.assertEqual(self.feed(busy()), (1, 0))

    def test_failed_stop_before_final_does_not_clear_work_early(self):
        with self.assertLogs("copilot-notify-icons", level="WARNING"):
            self.assertEqual(self.feed(busy(), finish()[1],
                                       event("hook.end", hookInvocationId="stop", success=False)),
                             (1, 0))
        self.assertEqual(self.feed(finish()[0]), (0, 0))

    def test_failed_stop_keeps_running_child_active(self):
        with self.assertLogs("copilot-notify-icons", level="WARNING"):
            self.assertEqual(self.feed(busy(), event("subagent.started", agent="child"),
                                       *finish()[:2],
                                       event("hook.end", hookInvocationId="stop", success=False)),
                             (1, 0))
        self.assertEqual(self.feed(event("subagent.completed", agent="child")), (0, 0))

    def test_child_stop_and_opt_in_do_not_clear_root(self):
        for opt_in in ("0", "1"):
            with patch.dict(os.environ, COPILOT_NOTIFY_SUBAGENTS=opt_in):
                self.assertEqual(self.feed(busy(), *finish("child")), (1, 0))
                self.assertEqual(self.feed(event("hook.start", hookType="subagentStop",
                                                 input={"sessionId": "root", "agentId": "child"})),
                                 (1, 0))

    def test_background_child_keeps_root_pane_busy_after_root_final(self):
        self.assertEqual(self.feed(busy(), event("subagent.started", agent="child"),
                                   *finish()), (1, 0))
        self.assertEqual(self.feed(event("subagent.completed", agent="child")), (0, 0))

    def test_child_completing_does_not_clear_active_root(self):
        self.assertEqual(self.feed(busy(), event("subagent.started", agent="child"),
                                   event("subagent.completed", agent="child")), (1, 0))

    def test_completed_child_resumes_without_subagent_started(self):
        self.feed(event("subagent.started", agent="child"),
                  event("subagent.completed", agent="child"))
        self.assertEqual(self.feed(event("assistant.turn_start", agent="child")), (1, 0))
        self.assertEqual(self.feed(event("assistant.message", agent="child",
                                        toolRequests=[{}]),
                                   event("assistant.turn_end", agent="child", turnId="7")), (1, 0))
        self.feed(event("assistant.message", agent="child", phase="final_answer",
                        toolRequests=[], turnId="8"))
        self.assertEqual(self.feed(event("assistant.turn_end", agent="child", turnId="7")), (1, 0))
        self.assertEqual(self.feed(event("assistant.turn_end", agent="child", turnId="8")), (0, 0))
        self.assertEqual(self.feed(event("assistant.turn_start", agent="child")), (1, 0))

    def test_resumed_child_stop_hook_completion_in_either_order(self):
        for reverse in (False, True):
            self.state.reset()
            self.feed(event("assistant.turn_start", agent="child"))
            message = event("assistant.message", agent="child", toolRequests=[])
            stop = [
                event("hook.start", hookType="agentStop", hookInvocationId="child-stop",
                      input={"sessionId": "child"}),
                event("hook.end", hookInvocationId="child-stop", success=True),
            ]
            self.assertEqual(self.feed(*(stop if reverse else [message])), (1, 0))
            self.assertEqual(self.feed(*([message] if reverse else stop)), (0, 0))
            self.assertEqual(self.feed(event("assistant.turn_start", agent="child"),
                                       event("hook.end", hookInvocationId="child-stop",
                                             success=True)), (1, 0))

    def test_child_completion_correlates_mixed_metadata_by_tool_call(self):
        self.assertEqual(self.feed(event("subagent.started", toolCallId="spawn")), (1, 0))
        self.assertEqual(self.feed(event("assistant.turn_start", agent="child")), (1, 0))
        self.assertEqual(self.feed(event("subagent.completed", agent="child",
                                         toolCallId="spawn")), (0, 0))

    def test_completed_child_does_not_erase_later_attention(self):
        self.feed(event("assistant.turn_start", agent="child"),
                  event("assistant.message", agent="child", turnId="1"),
                  event("assistant.turn_end", agent="child", turnId="1"))
        self.assertEqual(self.feed(event("hook.start", hookType="notification", input={
            "sessionId": "child", "notification_type": "permission_prompt",
        })), (0, 1))
        self.assertEqual(self.feed(event("tool.execution_complete", toolCallId="unrelated")), (0, 1))

    def test_completed_child_ignores_duplicate_final_turn_end(self):
        self.feed(event("assistant.turn_start", agent="child"),
                  event("assistant.message", agent="child", turnId="1"),
                  event("assistant.turn_end", agent="child", turnId="1"),
                  event("hook.start", hookType="notification", input={
                      "sessionId": "child", "notification_type": "permission_prompt",
                  }))
        self.assertEqual(self.feed(event("assistant.turn_end", agent="child", turnId="1")), (0, 1))

    def test_waiting_child_and_working_root(self):
        self.assertEqual(self.feed(busy(), event("subagent.started", agent="child"),
                                   event("tool.execution_start", agent="child",
                                         toolCallId="ask", toolName="ask_user")), (1, 1))
        self.assertEqual(self.feed(event("tool.execution_complete", toolCallId="other")), (1, 1))
        self.assertEqual(self.feed(event("tool.execution_complete", agent="child",
                                         toolCallId="ask")), (1, 0))

    def test_permission_wait_does_not_guess_tool_correlation(self):
        self.assertEqual(self.feed(busy(), event("tool.execution_start", toolCallId="other"),
                                   event("hook.start", hookType="notification",
                                         input={"sessionId": "root", "notification_type": "permission_prompt"})),
                         (0, 1))
        self.assertEqual(self.feed(event("tool.execution_complete", toolCallId="other")), (0, 1))
        self.assertEqual(self.feed(busy()), (1, 0))

    def test_child_permission_without_agent_id(self):
        self.assertEqual(self.feed(busy(), event("hook.start", hookType="notification",
                                                 input={"sessionId": "child",
                                                        "notification_type": "elicitation_dialog"})),
                         (1, 1))
        self.assertEqual(self.feed(event("assistant.turn_start", agent="child")), (1, 0))

    def test_idle_notification_not_erased_by_old_stop(self):
        self.feed(busy(), *finish())
        self.assertEqual(self.feed(event("hook.start", hookType="notification",
                                         input={"sessionId": "root", "notification_type": "permission_prompt"})),
                         (0, 1))

    def test_queued_and_background_messages_do_not_start_root(self):
        self.feed(busy(), *finish())
        self.assertEqual(self.feed(event("user.message", delivery="queued"),
                                   event("user.message", delivery="idle", source="agent-child")), (0, 0))
        # Actual observed root input has parentAgentTaskId too.
        self.assertEqual(self.feed(event("user.message", delivery="idle", parentAgentTaskId="task")),
                         (1, 0))

    def test_continuation_and_late_stop_do_not_clear_new_turn(self):
        self.feed(busy(), *finish())
        self.assertEqual(self.feed(busy()), (1, 0))
        self.assertEqual(self.feed(event("hook.end", hookInvocationId="stop", success=True)), (1, 0))
        self.assertEqual(self.feed(*finish(invocation="new-stop")), (0, 0))

    def test_cancel_shutdown_and_resume(self):
        for kind in ("abort", "session.shutdown", "session.resume"):
            self.assertEqual(self.feed(busy(), event("subagent.started", agent="child"),
                                       event(kind)), (0, 0))
        self.assertEqual(self.feed(busy()), (1, 0))

    def test_resumed_session_finishes_with_legacy_no_phase_message(self):
        self.feed(busy(), event("tool.execution_start", toolCallId="abandoned"),
                  event("session.shutdown"), event("session.resume"))
        self.assertEqual(self.feed(busy(), event("assistant.message", turnId="0",
                                                toolRequests=[]),
                                   event("assistant.turn_end", turnId="0")), (0, 0))


class TranscriptTests(Files):
    def setUp(self):
        super().setUp()
        self.path = self.directory / "events.jsonl"
        self.reader = activity.Transcript(self.path, "root")

    def test_partial_line_incremental_and_missing_file(self):
        self.assertFalse(self.reader.poll())
        self.write_events(self.path, [start(), busy()])
        self.assertTrue(self.reader.poll())
        offset = self.reader.offset
        self.assertTrue(self.reader.poll())
        self.assertEqual(offset, self.reader.offset)
        tail = json.dumps(finish()[0]).encode()
        with self.path.open("ab") as stream:
            stream.write(tail[:10])
        self.assertTrue(self.reader.poll())
        self.assertEqual(offset, self.reader.offset)
        with self.path.open("ab") as stream:
            stream.write(tail[10:] + b"\n")
        self.write_events(self.path, finish()[1:], "a")
        self.assertTrue(self.reader.poll())
        self.assertEqual(self.reader.display_counts, (0, 0))

    def test_truncate_regrow_and_resume(self):
        self.write_events(self.path, [start(), busy(), *finish(), event("session.shutdown")])
        self.reader.poll()
        self.write_events(self.path, [event("session.resume"), busy()], "a")
        self.reader.poll()
        self.assertEqual(self.reader.display_counts, (1, 0))
        self.write_events(self.path, [start(), event("user.message", delivery="queued", padding="x" * 2000)])
        self.reader.poll()
        self.assertEqual(self.reader.display_counts, (0, 0))

    def test_old_backlog_not_published_until_caught_up(self):
        self.write_events(self.path, [start(), busy(), *finish()])
        while not self.reader.poll(budget=1):
            self.assertEqual(self.reader.display_counts, (0, 0))
        self.assertEqual(self.reader.display_counts, (0, 0))

    def test_child_header_rejected(self):
        self.write_events(self.path, [start("child")])
        with self.assertRaisesRegex(ValueError, "does not own"):
            self.reader.poll()


class PrefixTests(unittest.TestCase):
    def test_prefix_uses_icons_without_counts(self):
        self.assertEqual(activity.prefix((0, 0)), "")
        self.assertEqual(activity.prefix((1, 0)), "[\u23f3] ")
        self.assertEqual(activity.prefix((12, 0)), "[\u23f3] ")
        self.assertEqual(activity.prefix((0, 1)), "[\u26a0] ")
        self.assertEqual(activity.prefix((1, 1)), "[\u23f3 | \u26a0] ")


class FakeZellij:
    def __init__(self):
        self.panes = {
            "1": {"title": "editor", "tab_id": 10},
            "2": {"title": "shell", "tab_id": 10},
            "3": {"title": "logs", "tab_id": 20},
        }
        self.tabs = {"10": {"name": "work"}, "20": {"name": "other"}}
        self.actions = []

    def rename(self, kind, item_id, title):
        self.actions.append((kind, item_id, title))
        (self.panes if kind == "pane" else self.tabs)[item_id][
            "title" if kind == "pane" else "name"] = title


class TitleTests(Files):
    def setUp(self):
        super().setUp()
        self.zellij = FakeZellij()
        self.titles = activity.Titles(self.zellij, self.directory / "titles.json")
        self.records = {name: {"pane_id": pane} for name, pane in (("a", "1"), ("b", "2"), ("c", "3"))}
        self.readers = {}
        for name in self.records:
            reader = activity.Transcript(self.directory / name, name)
            reader.validated = True
            reader.display_counts = (0, 0)
            self.readers[name] = reader

    def update(self):
        self.titles.update(self.zellij.panes, self.zellij.tabs,
                           activity.aggregate(self.records, self.readers, self.zellij.panes))

    def test_same_tab_a_finishes_b_stays_then_both_clear(self):
        self.readers["a"].display_counts = self.readers["b"].display_counts = (1, 0)
        self.update()
        self.assertEqual(self.zellij.tabs["10"]["name"], activity.prefix((2, 0)) + "work")
        self.readers["a"].display_counts = (0, 0)
        self.update()
        self.assertEqual(self.zellij.panes["1"]["title"], "editor")
        self.assertEqual(self.zellij.tabs["10"]["name"], activity.prefix((1, 0)) + "work")
        self.readers["b"].display_counts = (0, 0)
        self.update()
        self.assertEqual(self.zellij.tabs["10"]["name"], "work")

    def test_different_tabs_and_waiting_combination(self):
        self.readers["a"].display_counts = (1, 0)
        self.readers["b"].display_counts = (0, 1)
        self.readers["c"].display_counts = (1, 0)
        self.update()
        self.assertEqual(self.zellij.tabs["10"]["name"], activity.prefix((1, 1)) + "work")
        self.assertEqual(self.zellij.tabs["20"]["name"], activity.prefix((1, 0)) + "other")

    def test_move_updates_old_and_new_stable_tab_ids(self):
        self.readers["a"].display_counts = (1, 0)
        self.update()
        self.zellij.panes["1"]["tab_id"] = 20
        self.update()
        self.assertEqual(self.zellij.tabs["10"]["name"], "work")
        self.assertEqual(self.zellij.tabs["20"]["name"], activity.prefix((1, 0)) + "other")

    def test_external_rename_is_not_clobbered_active_or_idle(self):
        self.readers["a"].display_counts = (1, 0)
        self.update()
        self.zellij.tabs["10"]["name"] = "my rename"
        self.zellij.panes["1"]["title"] = "my pane"
        self.update()
        before = len(self.zellij.actions)
        self.update()
        self.assertEqual(len(self.zellij.actions), before)
        self.readers["a"].display_counts = (0, 0)
        self.update()
        self.assertEqual(self.zellij.tabs["10"]["name"], "my rename")
        self.assertEqual(self.zellij.panes["1"]["title"], "my pane")
        self.readers["a"].display_counts = (1, 0)
        self.update()
        self.assertEqual(self.zellij.tabs["10"]["name"], activity.prefix((1, 0)) + "my rename")

    def test_restart_recovers_only_journal_owned_names(self):
        self.readers["a"].display_counts = (1, 0)
        self.update()
        self.zellij.tabs["20"]["name"] = activity.prefix((1, 0)) + "user's literal title"
        recovered = activity.Titles(self.zellij, self.directory / "titles.json")
        recovered.update(self.zellij.panes, self.zellij.tabs, {})
        self.assertEqual(self.zellij.tabs["10"]["name"], "work")
        self.assertTrue(self.zellij.tabs["20"]["name"].endswith("user's literal title"))

    def test_idle_no_rename_and_exited_panes_drop_ownership(self):
        self.update()
        self.assertEqual(self.zellij.actions, [])
        self.readers["a"].display_counts = (1, 0)
        self.update()
        del self.zellij.panes["1"]
        self.update()
        self.assertNotIn("pane:1", self.titles.owned)
        self.assertEqual(self.zellij.tabs["10"]["name"], "work")

    def test_failed_rename_is_retried_instead_of_treated_as_user_rename(self):
        self.readers["a"].display_counts = (1, 0)
        with patch.object(self.zellij, "rename", side_effect=RuntimeError("RPC unavailable")):
            with self.assertRaisesRegex(RuntimeError, "RPC unavailable"):
                self.update()
        self.titles = activity.Titles(self.zellij, self.directory / "titles.json")
        self.update()
        self.assertEqual(self.zellij.tabs["10"]["name"], activity.prefix((1, 0)) + "work")
        self.assertEqual(self.zellij.panes["1"]["title"], activity.prefix((1, 0)) + "editor")

    def test_recovery_after_rename_before_journal_commit(self):
        desired = activity.prefix((1, 0)) + "work"
        activity.atomic_json(self.directory / "titles.json", {
            "tab:10": {"base": "work", "applied": "work", "pending": desired},
        })
        self.zellij.tabs["10"]["name"] = desired
        recovered = activity.Titles(self.zellij, self.directory / "titles.json")
        recovered.update(self.zellij.panes, self.zellij.tabs, {})
        self.assertEqual(self.zellij.tabs["10"]["name"], "work")


class RuntimeTests(Files):
    def setUp(self):
        super().setUp()
        self.processes = []
        self.session = "copilot-notify-test-" + uuid.uuid4().hex
        self.state = self.directory / "fake.json"
        self.env = dict(os.environ, ZELLIJ_SESSION_NAME=self.session, ZELLIJ_PANE_ID="1",
                        COPILOT_NOTIFY_ICONS="1", COPILOT_NOTIFY_STATE_DIR=str(self.directory / "cache"),
                        COPILOT_HOME=str(self.directory / "home"), FAKE_ZELLIJ_STATE=str(self.state))
        source = ROOT / "tests/fake_zellij.py"
        if os.name == "nt":
            binary = self.directory / "fake-zellij.cmd"
            binary.write_text('@echo off\n"%s" "%s" %%*\n' % (sys.executable, source))
        else:
            binary = self.directory / "fake-zellij"
            binary.write_text("#!" + sys.executable + "\n" + source.read_text())
            binary.chmod(0o700)
        self.env["COPILOT_NOTIFY_ZELLIJ"] = str(binary)
        activity.atomic_json(self.state, {
            "panes": [{"id": p, "title": "pane%d" % p, "tab_id": t,
                       "is_plugin": False, "exited": False} for p, t in ((1, 10), (2, 10), (3, 20))],
            "tabs": [{"tab_id": 10, "name": "work"}, {"tab_id": 20, "name": "other"}],
        })
        with patch.dict(os.environ, self.env):
            self.cache = activity.state_directory(self.session)

    def tearDown(self):
        for process in self.processes:
            if process.stdout and not process.stdout.closed:
                process.communicate(timeout=20)
        if self.cache.exists():
            with activity.Lock(self.cache / "registry.lock"):
                activity.atomic_json(self.cache / "registrations.json", {})
            deadline = time.monotonic() + 20
            status = {}
            while time.monotonic() < deadline:
                status = activity.read_json(self.cache / "status.json", {})
                pid = status.get("pid")
                if pid and not activity.process_token(pid):
                    break
                time.sleep(0.1)
            if pid and activity.process_token(pid) and status.get("token") == activity.process_token(pid):
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.2)
        super().tearDown()

    def transcript(self, session, events=None):
        path = Path(self.env["COPILOT_HOME"]) / "session-state" / session / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / ("inuse.%d.lock" % os.getpid())).touch()
        if events is not None:
            self.write_events(path, [start(session), *events])
        return path

    def test_sdk_shell_status_drives_terminal_and_reports_bridge_failure(self):
        path = self.transcript("a", [])
        snapshot = dict(protocol_version=1, session_id="a", owner_pid=os.getpid(),
                        updated_at=time.time(), state="ready", pending_shells=1, settling=False)
        activity.atomic_json(path.parent / "gadget-sdk.json", snapshot)
        self.assert_process(self.launch())
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((1, 0)) + "work")
        snapshot.update(state="task_query_failed", pending_shells=0, updated_at=time.time())
        activity.atomic_json(path.parent / "gadget-sdk.json", snapshot)
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((0, 1)) + "work"
                      and activity.read_json(self.cache / "status.json", {}).get("error"))
        self.assertIn("SDK bridge:", activity.read_json(self.cache / "status.json")["error"])
        snapshot.update(state="ready", updated_at=time.time())
        activity.atomic_json(path.parent / "gadget-sdk.json", snapshot)
        self.wait_for(lambda s: s["tabs"][0]["name"] == "work"
                      and activity.read_json(self.cache / "status.json", {}).get("error") is None)
        self.assertIsNone(activity.read_json(self.cache / "status.json")["error"])

    def test_legacy_backend_bypasses_sdk_in_terminal_coordinator(self):
        path = self.transcript("a", [])
        activity.atomic_json(path.parent / "gadget-sdk.json", {
            "state": "stopped",
        })
        activity.atomic_json(Path(self.env["COPILOT_HOME"]) / "copilot-agent-notify.json", {
            "status_backend": "legacy",
        })
        self.assert_process(self.launch())
        self.wait_for(lambda s: s["tabs"][0]["name"] == "work"
                      and activity.read_json(self.cache / "status.json", {}).get("status_backend") == "legacy")
        status = activity.read_json(self.cache / "status.json")
        self.assertEqual(status["status_backend"], "legacy")
        self.assertIsNone(status["error"])

    def launch(self, session="a", pane="1", hook="sessionStart", extra=None):
        payload = {"sessionId": session}
        if extra:
            payload.update(extra)
        proc = subprocess.Popen([sys.executable, str(ROOT / "scripts/activity.py"), "hook", hook],
                                env=dict(self.env, ZELLIJ_PANE_ID=pane), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        proc.stdin = None
        self.processes.append(proc)
        return proc

    def wait_for(self, condition):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = activity.read_json(self.state)
            if condition(state):
                return state
            time.sleep(0.1)
        self.fail("condition timed out; status=%r; state=%r" %
                  (activity.read_json(self.cache / "status.json"), state))

    def assert_process(self, process):
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "")

    def test_simultaneous_hooks_single_writer_aggregate_and_shutdown(self):
        a = self.transcript("a", [busy()])
        b = self.transcript("b", [busy()])
        processes = [self.launch("a", "1"), self.launch("b", "2"), self.launch("a", "1")]
        for process in processes:
            self.assert_process(process)
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((2, 0)) + "work")
        self.write_events(a, finish("a"), "a")
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((1, 0)) + "work"
                      and s["panes"][0]["title"] == "pane1")
        self.write_events(b, finish("b"), "a")
        self.wait_for(lambda s: s["tabs"][0]["name"] == "work")
        actions = [json.loads(line) for line in self.state.with_suffix(".actions").read_text().splitlines()]
        self.assertEqual(len({action["writer"] for action in actions}), 1)
        self.assert_process(self.launch("a", hook="sessionEnd"))
        self.assert_process(self.launch("b", pane="2", hook="sessionEnd"))
        self.wait_for(lambda s: not activity.read_json(self.cache / "status.json")["running"])
        self.assertFalse(activity.read_json(self.cache / "status.json")["running"])

    def test_delayed_transcript_then_shutdown(self):
        path = self.transcript("a")
        self.assert_process(self.launch())
        self.write_events(path, [start("a"), busy()])
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((1, 0)) + "work")
        self.write_events(path, [event("session.shutdown")], "a")
        self.wait_for(lambda s: s["tabs"][0]["name"] == "work")

    def test_resumed_child_restores_pane_and_tab_until_its_next_final(self):
        path = self.transcript("a", [busy(), event("subagent.started", agent="child"),
                                     *finish("a"), event("subagent.completed", agent="child")])
        self.assert_process(self.launch())
        self.write_events(path, [event("assistant.turn_start", agent="child")], "a")
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((1, 0)) + "work"
                      and s["panes"][0]["title"] == activity.prefix((1, 0)) + "pane1")
        self.write_events(path, [
            event("assistant.message", agent="child", turnId="7", toolRequests=[]),
            event("assistant.turn_end", agent="child", turnId="7"),
        ], "a")
        self.wait_for(lambda s: s["tabs"][0]["name"] == "work"
                      and s["panes"][0]["title"] == "pane1")

    def test_historical_shutdown_does_not_drop_resuming_registration(self):
        path = self.transcript("a", [busy(), *finish("a"), event("session.shutdown")])
        self.assert_process(self.launch())
        self.write_events(path, [event("session.resume"), busy()], "a")
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((1, 0)) + "work")

    def test_child_agent_stop_no_registration_or_actions(self):
        path = self.transcript("a", [busy()])
        self.assert_process(self.launch("child", hook="agentStop", extra={"transcriptPath": str(path)}))
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.state.with_suffix(".actions").exists())

    def test_dead_owner_is_cleaned_without_busy_timeout(self):
        self.transcript("a", [busy()])
        self.assert_process(self.launch())
        self.wait_for(lambda s: s["tabs"][0]["name"] != "work")
        with activity.Lock(self.cache / "registry.lock"):
            records = activity.read_json(self.cache / "registrations.json")
            for record in records.values():
                record["token"] = "not-the-process-start-token"
            activity.atomic_json(self.cache / "registrations.json", records)
        self.wait_for(lambda s: s["tabs"][0]["name"] == "work")

    def test_crashed_writer_restarts_and_restores_journal(self):
        path = self.transcript("a", [busy()])
        self.assert_process(self.launch())
        self.wait_for(lambda s: s["tabs"][0]["name"] != "work")
        status = activity.read_json(self.cache / "status.json")
        os.kill(status["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while activity.process_token(status["pid"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(activity.process_token(status["pid"]))
        self.write_events(path, finish("a"), "a")
        self.assert_process(self.launch(hook="agentStop"))
        self.wait_for(lambda s: s["tabs"][0]["name"] == "work")
        self.assertNotEqual(activity.read_json(self.cache / "status.json")["pid"], status["pid"])

    def test_real_hook_wrapper_with_fake_zellij(self):
        self.transcript("a", [busy()])
        if os.name == "nt":
            shell = shutil.which("powershell") or shutil.which("pwsh")
            commands = [[shell, "-NoProfile", "-File", str(ROOT / "scripts/activity.ps1"), "sessionStart"]]
        else:
            commands = [["/bin/bash", str(ROOT / "scripts/activity.sh"), "sessionStart"]]
            if shutil.which("pwsh"):
                commands.append([shutil.which("pwsh"), "-NoProfile", "-File",
                                 str(ROOT / "scripts/activity.ps1"), "sessionStart"])
        for command in commands:
            result = subprocess.run(command, env=self.env, input='{"sessionId":"a"}',
                                    capture_output=True, text=True, timeout=25)
            if os.name == "nt" and "AuthorizationManager check failed" in result.stderr:
                self.skipTest("Native PowerShell refuses scripts on this filesystem; no policy changed")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
        self.wait_for(lambda s: s["tabs"][0]["name"] == activity.prefix((1, 0)) + "work")

    def test_outer_terminal_progress_clears_after_failed_notification(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux outer-terminal PTY integration")
        import pty
        import select
        client_script = self.directory / "zellij"
        client_script.write_text("#!/bin/sh\nread line\n")
        client_script.chmod(0o700)
        master, slave = pty.openpty()
        self.env["WT_SESSION"] = "isolated-progress-test"
        client = subprocess.Popen([str(client_script), "attach", self.session],
                                  stdin=subprocess.PIPE, stdout=slave, stderr=slave,
                                  env=self.env)

        def wait_for_signal(expected):
            output = b""
            deadline = time.monotonic() + 15
            while expected not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    output += os.read(master, 4096)
            self.assertIn(expected, output)

        try:
            path = self.transcript("a", [busy()])
            self.assert_process(self.launch())
            wait_for_signal(b"\x1b]9;4;3;0\x07")
            self.write_events(path, [*finish("a")[:2],
                                    event("hook.end", hookInvocationId="stop", success=False)], "a")
            wait_for_signal(b"\x1b]9;4;0;0\x07\x07")
            self.wait_for(lambda s: s["tabs"][0]["name"] == "work")
        finally:
            client.stdin.close()
            client.wait(timeout=5)
            os.close(slave)
            os.close(master)

    def test_resumed_no_phase_completion_clears_tab_and_outer_terminal_progress(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux outer-terminal PTY integration")
        import pty
        import select
        client_script = self.directory / "zellij"
        client_script.write_text("#!/bin/sh\nread line\n")
        client_script.chmod(0o700)
        master, slave = pty.openpty()
        self.env["WT_SESSION"] = "isolated-progress-test"
        client = subprocess.Popen([str(client_script), "attach", self.session],
                                  stdin=subprocess.PIPE, stdout=slave, stderr=slave,
                                  env=self.env)

        def wait_for_signal(expected):
            output = b""
            deadline = time.monotonic() + 15
            while expected not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    output += os.read(master, 4096)
            self.assertIn(expected, output)

        try:
            path = self.transcript("a", [
                busy(),
                event("tool.execution_start", toolCallId="abandoned"),
                event("session.shutdown"),
                event("session.resume"),
                busy(),
            ])
            self.assert_process(self.launch())
            wait_for_signal(b"\x1b]9;4;3;0\x07")
            self.write_events(path, [
                event("assistant.message", turnId="1", toolRequests=[]),
                event("assistant.turn_end", turnId="1"),
            ], "a")
            wait_for_signal(b"\x1b]9;4;0;0\x07\x07")
            self.wait_for(lambda s: s["tabs"][0]["name"] == "work")
        finally:
            client.stdin.close()
            client.wait(timeout=5)
            os.close(slave)
            os.close(master)

    def test_non_zellij_and_disabled_do_not_create_state(self):
        self.transcript("a", [busy()])
        for changes in ({"ZELLIJ_SESSION_NAME": ""}, {"COPILOT_NOTIFY_ICONS": "0"}):
            original = self.env
            self.env = dict(self.env, **changes)
            self.assert_process(self.launch())
            self.env = original
        self.assertFalse(self.cache.exists())

    def test_missing_zellij_diagnostic(self):
        self.transcript("a", [busy()])
        self.env["COPILOT_NOTIFY_ZELLIJ"] = str(self.directory / "missing-zellij")
        process = self.launch()
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(stdout, "")
        self.assertIn("copilot-notify-icons:", stderr)
        self.assertIn("Zellij executable not found", stderr)


class ZellijLaunchTests(unittest.TestCase):
    def test_windows_actions_do_not_create_console_windows(self):
        with patch.object(activity.os, "name", "nt"), \
                patch.object(activity.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(activity.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "[]", "")
            self.assertEqual(activity.Zellij("zellij.exe", "term").action("list-panes", "--json"), "[]")
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
            self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_unix_actions_do_not_receive_windows_flags(self):
        with patch.object(activity.os, "name", "posix"), \
                patch.object(activity.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "[]", "")
            activity.Zellij("zellij", "term").action("list-tabs", "--json")
            self.assertNotIn("creationflags", run.call_args.kwargs)


class PrimitiveTests(Files):
    def test_process_identity_and_pid_reuse(self):
        token = activity.process_token(os.getpid())
        self.assertIsNotNone(token)
        self.assertTrue(activity.owner_alive({"pid": os.getpid(), "token": token}))
        self.assertFalse(activity.owner_alive({"pid": os.getpid(), "token": "old"}))
        self.assertIn(os.getpid(), activity.process_table())

    def test_os_lock_excludes_second_process(self):
        path = self.directory / "lock"
        script = ("import sys;sys.path.insert(0,sys.argv[1]);import activity;"
                  "lock=activity.Lock(activity.Path(sys.argv[2]));"
                  "ok=lock.acquire();lock.release();sys.exit(0 if ok else 7)")
        with activity.Lock(path):
            result = subprocess.run([sys.executable, "-c", script, str(ROOT / "scripts"), str(path)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 7, result.stderr)

    def test_shell_wrappers_noop_and_dependency_diagnostic(self):
        if os.name == "nt":
            self.skipTest("Bash PATH test is POSIX-only")
        script = ROOT / "scripts/activity.sh"
        env = dict(os.environ, PATH=str(self.directory), ZELLIJ_SESSION_NAME="fake",
                   ZELLIJ_PANE_ID="1", COPILOT_NOTIFY_ICONS="0")
        result = subprocess.run(["/bin/bash", str(script)], env=env, input="{}",
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        env["COPILOT_NOTIFY_ICONS"] = "1"
        result = subprocess.run(["/bin/bash", str(script)], env=env, input="{}",
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.9+", result.stderr)


if __name__ == "__main__":
    unittest.main()
