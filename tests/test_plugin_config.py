"""Keep notification plugin installation independent of language servers."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
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
                    self.assertIn("${PLUGIN_ROOT}/scripts/", command["bash"])
                    self.assertIn("$env:PLUGIN_ROOT", command["powershell"])
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


if __name__ == "__main__":
    unittest.main()
