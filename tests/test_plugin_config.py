"""Keep notification plugin installation independent of language servers."""

import json
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PluginConfigTests(unittest.TestCase):
    def test_manifest_isolates_plugin_from_project_configuration(self):
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["$schema"],
                         "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        self.assertNotIn("lspServers", manifest)
        self.assertNotIn("hooks", manifest)

    def test_no_plugin_language_server_config(self):
        self.assertFalse((ROOT / "com.github.copilot/lsp.json").exists())

    def test_project_development_keeps_csharp_enabled(self):
        config = json.loads((ROOT / ".github/lsp.json").read_text(encoding="utf-8"))
        server = config["lspServers"]["csharp"]
        self.assertFalse(server.get("disabled", False))
        self.assertEqual(server["fileExtensions"][".cs"], "csharp")
        self.assertTrue(server["command"])

    def test_namespaced_hooks_preserve_lifecycle_events(self):
        config = json.loads((ROOT / "com.github.copilot/hooks/hooks.json").read_text(
            encoding="utf-8"))
        self.assertEqual(config["version"], 1)
        self.assertEqual(set(config["hooks"]), {
            "sessionStart", "userPromptSubmitted", "sessionEnd",
            "agentStop", "subagentStop", "notification",
        })
        for event, commands in config["hooks"].items():
            for command in commands:
                with self.subTest(event=event, command=command):
                    self.assertEqual(command["type"], "command")
                    self.assertEqual(command["exec"],
                                     "${PLUGIN_ROOT}/windows-helper/bin/copilot-notify")
                    self.assertEqual(command["args"][0], "--hook")
                    self.assertIn(command["args"][1], ("activity", "notify"))
                    self.assertEqual(command["args"][2], event)
                    self.assertFalse({"bash", "powershell", "command"} & command.keys())
        self.assertFalse((ROOT / "hooks.json").exists())

    def test_installed_plugin_does_not_contribute_project_lsp(self):
        copilot = shutil.which("copilot")
        if not copilot:
            self.skipTest("Copilot CLI is required for isolated plugin installation")
        launcher = [copilot]
        if Path(copilot).suffix.lower() in (".cmd", ".bat"):
            launcher = ["cmd.exe", "/d", "/c", copilot]
        with tempfile.TemporaryDirectory(prefix="plugin-config-") as temporary:
            directory = Path(temporary)
            self.assertFalse(directory.resolve().is_relative_to(ROOT),
                             "The simulated plugin host must be outside the development repository")
            source = directory / "source"
            for relative in ("plugin.json", ".github/lsp.json",
                             "com.github.copilot/hooks/hooks.json"):
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            environment = dict(os.environ, COPILOT_HOME=str(directory / "home"),
                               COPILOT_CACHE_HOME=str(directory / "cache"))
            for args in (["plugin", "install", str(source)],
                         ["plugin", "list", "--json"]):
                result = subprocess.run(launcher + args, cwd=directory, env=environment,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
            self.assertTrue(any(plugin["name"] == manifest["name"] and plugin["enabled"]
                                for plugin in json.loads(result.stdout)))
            user_config = directory / "home/lsp-config.json"
            for disabled in (True, False):
                with self.subTest(host_csharp_disabled=disabled):
                    config = {"lspServers": {"csharp": {
                        "disabled": disabled, "command": "dotnet",
                        "fileExtensions": {".cs": "csharp"},
                    }}}
                    user_config.write_text(json.dumps(config), encoding="utf-8")
                    result = subprocess.run(launcher + ["lsp", "list", "--json"],
                                            cwd=directory, env=environment,
                                            capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    servers = json.loads(result.stdout)
                    self.assertFalse(any(server.get("sourcePlugin") == manifest["name"]
                                         for server in servers), servers)
                    self.assertEqual(any(server["id"] == "csharp" for server in servers),
                                     not disabled, servers)
                    self.assertEqual(json.loads(user_config.read_text(encoding="utf-8")),
                                     config)

    def test_direct_hooks_in_offline_cli_with_spaces_in_plugin_path(self):
        copilot = shutil.which("copilot")
        if not copilot:
            self.skipTest("Copilot CLI is required for direct hook execution")
        class Provider(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, reason in (({"role": "assistant", "content": "Synthetic response."}, None),
                                      ({}, "stop")):
                    chunk = {"id": "synthetic-completion", "object": "chat.completion.chunk",
                             "created": 0, "model": "gpt-4.1", "choices": [
                                 {"index": 0, "delta": delta, "finish_reason": reason}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory(prefix="plugin hooks ") as temporary:
            directory = Path(temporary)
            source = directory / "plugin with spaces"
            for relative in (
                "plugin.json", "com.github.copilot/hooks/hooks.json",
                "windows-helper/bin/copilot-notify", "windows-helper/bin/copilot-notify.exe",
                "scripts/activity.sh", "scripts/notify.sh",
            ):
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            home = directory / "home"
            home.mkdir()
            (home / "copilot-agent-notify.json").write_text('{"hook_alerts":false}')
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                environment = {key: value for key, value in os.environ.items()
                               if key.upper() in ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC")}
                environment.update({
                    "HOME": str(home), "USERPROFILE": str(home), "COPILOT_HOME": str(home),
                    "COPILOT_CACHE_HOME": str(directory / "cache"),
                    "LOCALAPPDATA": str(directory / "local"), "APPDATA": str(directory / "roaming"),
                    "XDG_CONFIG_HOME": str(directory / "config"), "XDG_CACHE_HOME": str(directory / "cache"),
                    "TEMP": str(directory), "TMP": str(directory), "TMPDIR": str(directory),
                    "COPILOT_OFFLINE": "true", "COPILOT_AUTO_UPDATE": "false",
                    "COPILOT_PROVIDER_BASE_URL": "http://127.0.0.1:" + str(server.server_port),
                    "COPILOT_NOTIFY_ICONS": "0",
                })
                launcher = [copilot]
                if Path(copilot).suffix.lower() in (".cmd", ".bat"):
                    launcher = ["cmd.exe", "/d", "/c", copilot]
                result = subprocess.run(launcher + [
                    "-p", "Synthetic hook fixture.", "--model", "gpt-4.1",
                    "--plugin-dir", str(source), "--disable-builtin-mcps",
                    "--disable-client-telemetry", "--no-auto-update", "--no-remote-export",
                ], cwd=directory, env=environment, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                events = [json.loads(line) for path in (home / "session-state").glob("*/events.jsonl")
                          for line in path.read_text(encoding="utf-8").splitlines()]
                started = {event["data"]["hookInvocationId"]: event["data"]["hookType"]
                           for event in events if event["type"] == "hook.start"}
                completed = {started[event["data"]["hookInvocationId"]]
                             for event in events if event["type"] == "hook.end"
                             and event["data"].get("success")}
                self.assertTrue({"sessionStart", "userPromptSubmitted", "agentStop"} <= completed,
                                completed)
                self.assertFalse(any(event["type"] == "hook.end" and not event["data"].get("success")
                                     for event in events))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
