"""Exercise notification scripts with fake desktop providers."""

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "tests" / (".runtime-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.capture = self.directory / "notifications.jsonl"
        self.transcript = self.directory / "events.jsonl"
        self.transcript.write_text(json.dumps({
            "type": "session.start", "data": {"sessionId": "root-session"},
        }) + "\n")
        self.env = dict(os.environ, COPILOT_HOME=str(self.directory / "home"),
                        CAPTURE_FILE=str(self.capture), COPILOT_NOTIFY_ICONS="0")
        self.runners = {}
        bash = shutil.which("bash")
        if bash and os.name != "nt":
            self.runners["bash"] = [bash, str(ROOT / "scripts/notify.sh")]
            binary = self.directory / "bin"
            binary.mkdir()
            for name, source in {
                "uname": "#!/bin/sh\nprintf 'Linux\\n'\n",
                "grep": "#!/bin/sh\nexit 1\n",
                "notify-send": "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CAPTURE_FILE\"\n",
            }.items():
                path = binary / name
                path.write_text(source)
                path.chmod(0o700)
            self.env["PATH"] = str(binary) + os.pathsep + os.environ.get("PATH", "")
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            source = (ROOT / "scripts/notify.ps1").read_text()
            original = r"..\windows-helper\bin\copilot-notify.exe"
            self.assertEqual(source.count(original), 1)
            script = self.directory / "notify.ps1"
            script.write_text(source.replace(original, "notifier.ps1"))
            (self.directory / "notifier.ps1").write_text(
                '@($args) | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:CAPTURE_FILE\n'
                '$global:LASTEXITCODE = 0\n')
            self.runners["powershell"] = [powershell, "-NoProfile", "-File", str(script)]
        self.assertTrue(self.runners, "no supported notification wrapper runtime found")

    def tearDown(self):
        shutil.rmtree(self.directory)

    def run_hook(self, runner, hook, payload, opt_in="0"):
        self.capture.unlink(missing_ok=True)
        result = subprocess.run(self.runners[runner] + [hook], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=15,
                                env=dict(self.env, COPILOT_NOTIFY_SUBAGENTS=opt_in))
        if os.name == "nt" and "AuthorizationManager check failed" in result.stderr:
            self.skipTest("Native PowerShell refuses scripts on this filesystem; no policy changed")
        return result, self.capture.read_text() if self.capture.exists() else ""

    def test_root_completion_and_child_stop_suppression(self):
        for runner in self.runners:
            for opt_in in ("0", "1"):
                with self.subTest(runner=runner, opt_in=opt_in):
                    result, captured = self.run_hook(runner, "agentStop", {
                        "sessionId": "child-session", "transcriptPath": str(self.transcript),
                    }, opt_in)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(captured, "")
                    result, captured = self.run_hook(runner, "agentStop", {
                        "sessionId": "root-session", "transcriptPath": str(self.transcript),
                    }, opt_in)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Agent finished responding", captured)

    def test_subagent_stop_opt_in_is_independent(self):
        for runner in self.runners:
            for opt_in in ("", "0", "1"):
                with self.subTest(runner=runner, opt_in=opt_in):
                    result, captured = self.run_hook(runner, "subagentStop", {
                        "sessionId": "root-session", "agentId": "child-session", "agentName": "Explore",
                    }, opt_in)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    if opt_in == "1":
                        self.assertIn("Subagent complete: Explore", captured)
                    else:
                        self.assertEqual(captured, "")

    def test_permission_and_input_never_suppressed(self):
        for runner in self.runners:
            for opt_in in ("0", "1", "invalid"):
                for kind in ("permission_prompt", "elicitation_dialog"):
                    with self.subTest(runner=runner, opt_in=opt_in, kind=kind):
                        result, captured = self.run_hook(runner, "notification", {
                            "sessionId": "child-session", "notification_type": kind,
                            "transcriptPath": str(self.transcript),
                        }, opt_in)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue(captured)
                        self.assertEqual(result.stdout, "")

    def test_invalid_header_reports_error_without_notification(self):
        self.transcript.write_text('{"type":"not-a-session"}\n')
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, captured = self.run_hook(runner, "agentStop", {
                    "sessionId": "root-session", "transcriptPath": str(self.transcript),
                })
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid transcript session header", result.stderr)
                self.assertEqual(captured, "")

    def test_wsl_explicit_interop_and_failure_propagation(self):
        if "bash" not in self.runners:
            self.skipTest("WSL launcher routing uses Bash")
        scripts = self.directory / "scripts"
        scripts.mkdir()
        binaries = self.directory / "windows-helper/bin"
        binaries.mkdir(parents=True)
        launcher = self.directory / "interop"
        launcher.write_text('#!/bin/sh\nprintf "interop\\n" >> "$CAPTURE_FILE"\n'
                            '[ "$1" = "$2" ] || exit 99\nshift\nexec "$@"\n')
        launcher.chmod(0o700)
        notifier = binaries / "copilot-notify.exe"
        notifier.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE_FILE"\n'
                            'exit "${NOTIFIER_EXIT:-0}"\n')
        notifier.chmod(0o700)
        (scripts / "bell.sh").write_text('printf "bell\\n" >> "$CAPTURE_FILE"\n')
        source = (ROOT / "scripts/notify.sh").read_text()
        self.assertEqual(source.count("local launcher=/init"), 1)
        script = scripts / "notify.sh"
        script.write_text(source.replace("local launcher=/init",
                                         "local launcher=" + shlex.quote(str(launcher))))
        (self.directory / "bin/grep").write_text("#!/bin/sh\nexit 0\n")
        self.runners["bash"] = [self.runners["bash"][0], str(script)]
        for code in ("0", "1"):
            with self.subTest(exit_code=code):
                self.env["NOTIFIER_EXIT"] = code
                result, captured = self.run_hook("bash", "agentStop", {
                    "sessionId": "root-session", "transcriptPath": str(self.transcript),
                })
                self.assertEqual(result.returncode, int(code), result.stderr)
                self.assertEqual(captured.count("interop\n"), 1)
                self.assertIn("[#root-ses]", captured)
                self.assertIn("Agent finished responding", captured)
                self.assertEqual("bell\n" in captured, code == "0")


if __name__ == "__main__":
    unittest.main()
