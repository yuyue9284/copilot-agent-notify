"""Opt-in real Zellij integration; never attaches to existing user sessions."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from test_activity import Files, ROOT, activity, busy, event, finish, start


@unittest.skipUnless(os.environ.get("COPILOT_NOTIFY_LIVE_TEST") == "1",
                     "set COPILOT_NOTIFY_LIVE_TEST=1 for disposable real Zellij tests")
class LiveZellijTests(Files):
    def test_real_panes_tabs_wrappers_and_cleanup(self):
        binary = shutil.which("zellij")
        self.assertIsNotNone(binary, "Zellij is required for this opt-in test")
        session = "cni-" + uuid.uuid4().hex[:12]
        env = dict(os.environ, ZELLIJ_SESSION_NAME=session, COPILOT_NOTIFY_ICONS="1",
                   COPILOT_NOTIFY_STATE_DIR=str(self.directory / "cache"),
                   COPILOT_HOME=str(self.directory / "home"), COPILOT_NOTIFY_ZELLIJ=binary)
        env.pop("ZELLIJ", None)
        env.pop("ZELLIJ_PANE_ID", None)
        api = activity.Zellij(binary, session)
        with patch.dict(os.environ, env):
            cache = activity.state_directory(session)
        created = False

        def wait_for(condition, description):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                panes, tabs = api.snapshot()
                if condition(panes, tabs):
                    return panes, tabs
                time.sleep(0.2)
            self.fail("%s; panes=%r tabs=%r status=%r" %
                      (description, panes, tabs, activity.read_json(cache / "status.json")))

        def hook(sid, pane, kind="sessionStart", extra=None):
            if os.name == "nt":
                command = [shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive",
                           "-File", str(ROOT / "scripts/activity.ps1"), kind]
            else:
                command = ["bash", str(ROOT / "scripts/activity.sh"), kind]
            payload = {"sessionId": sid, **(extra or {})}
            result = subprocess.run(command, env=dict(env, ZELLIJ_PANE_ID=pane),
                                    input=json.dumps(payload), capture_output=True,
                                    encoding="utf-8", timeout=35)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

        def transcript(sid):
            path = Path(env["COPILOT_HOME"]) / "session-state" / sid / "events.jsonl"
            path.parent.mkdir(parents=True)
            (path.parent / ("inuse.%d.lock" % os.getpid())).touch()
            self.write_events(path, [start(sid), busy()])
            return path

        try:
            launch_env = {k: v for k, v in env.items() if k != "ZELLIJ_SESSION_NAME"}
            result = subprocess.run([binary, "attach", "--create-background", session],
                                    env=launch_env, capture_output=True, encoding="utf-8", timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            created = True
            deadline = time.monotonic() + 15
            while True:
                try:
                    panes, tabs = api.snapshot()
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)
            tab = next(iter(tabs))
            api.rename("tab", tab, "work")
            pane_a = next(k for k, v in panes.items() if str(v["tab_id"]) == tab)
            api.rename("pane", pane_a, "A")
            pane_b = api.action("new-pane", "--tab-id", tab, "--name", "B").strip()
            pane_b = pane_b.removeprefix("terminal_")
            other = api.action("new-tab", "--name", "other").strip()
            panes, tabs = api.snapshot()
            pane_c = next(k for k, v in panes.items() if str(v["tab_id"]) == other)
            api.rename("pane", pane_c, "C")
            a, b, c = (transcript(sid) for sid in ("live-a", "live-b", "live-c"))
            for sid, pane in (("live-a", pane_a), ("live-b", pane_b), ("live-c", pane_c)):
                hook(sid, pane)
            writer = activity.read_json(cache / "status.json")["pid"]
            wait_for(lambda p, t: t[tab]["name"] == activity.prefix((2, 0)) + "work"
                     and t[other]["name"] == activity.prefix((1, 0)) + "other"
                     and p[pane_a]["title"] == activity.prefix((1, 0)) + "A",
                     "overlapping sessions on separate tabs")
            print("LIVE: two sessions share a busy tab; another tab remains independent")

            self.write_events(a, finish("live-a"), "a")
            wait_for(lambda p, t: t[tab]["name"] == activity.prefix((1, 0)) + "work"
                     and p[pane_a]["title"] == "A", "A finishing must not clear B")
            self.write_events(b, [
                event("tool.execution_start", toolCallId="ask", toolName="ask_user"),
                event("hook.start", hookType="agentStop", hookInvocationId="child-stop",
                      input={"sessionId": "child"}),
                event("hook.end", hookType="agentStop", hookInvocationId="child-stop", success=True),
            ], "a")
            wait_for(lambda p, t: t[tab]["name"] == activity.prefix((0, 1)) + "work",
                     "attention and child-stop isolation")
            self.write_events(b, [event("tool.execution_complete", toolCallId="ask")], "a")
            wait_for(lambda p, t: t[tab]["name"] == activity.prefix((1, 0)) + "work",
                     "matching input completion resumes busy indicator")
            print("LIVE: A finishes without clearing B; waiting and resumption update correctly")

            api.rename("pane", pane_b, "User pane")
            api.rename("tab", tab, "User tab")
            time.sleep(2)
            panes, tabs = api.snapshot()
            self.assertEqual(panes[pane_b]["title"], "User pane")
            self.assertEqual(tabs[tab]["name"], "User tab")
            self.write_events(b, finish("live-b"), "a")
            self.write_events(c, finish("live-c"), "a")
            wait_for(lambda p, t: t[tab]["name"] == "User tab" and t[other]["name"] == "other"
                     and p[pane_b]["title"] == "User pane" and p[pane_c]["title"] == "C",
                     "idle restores bases and preserves explicit user renames")
            hook("child", pane_b, "agentStop", {"transcriptPath": str(b)})
            self.assertEqual(len(activity.read_json(cache / "registrations.json")), 3)
            self.assertEqual(activity.read_json(cache / "status.json")["pid"], writer)
            print("LIVE: idle restores names; user renames survive; child hook cannot register")

            self.write_events(a, [busy()], "a")
            wait_for(lambda p, t: t[tab]["name"] == activity.prefix((1, 0)) + "User tab",
                     "new busy interval uses the user's latest name")
            # A stale start token simulates owner death without killing any user process.
            with activity.Lock(cache / "registry.lock"):
                records = activity.read_json(cache / "registrations.json")
                for record in records.values():
                    record["token"] = "dead-test-owner"
                activity.atomic_json(cache / "registrations.json", records)
            wait_for(lambda p, t: t[tab]["name"] == "User tab" and p[pane_a]["title"] == "A",
                     "dead owner cleanup restores busy names")
            deadline = time.monotonic() + 15
            while activity.process_token(writer) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertIsNone(activity.process_token(writer))
            print("LIVE: dead-owner cleanup restores names and coordinator exits")
        finally:
            if cache.exists():
                with activity.Lock(cache / "registry.lock"):
                    activity.atomic_json(cache / "registrations.json", {})
                status = activity.read_json(cache / "status.json", {})
                deadline = time.monotonic() + 15
                while status.get("token") and activity.owner_alive(status) and time.monotonic() < deadline:
                    time.sleep(0.1)
            if created:
                result = subprocess.run([binary, "delete-session", "--force", session], env=env,
                                        capture_output=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
