import os
from pathlib import Path
import random
import shutil
import stat
import string
import subprocess
import unittest

from test_activity import Files, ROOT


class SecretHookTests(Files):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.remove_directory)
        self.shell = shutil.which("sh")
        scanner = os.environ.get("GITLEAKS_BIN") or shutil.which("gitleaks")
        if not scanner:
            scanner = str(Path.home() / ".local/bin" /
                          ("gitleaks.exe" if os.name == "nt" else "gitleaks"))
        if not self.shell or not Path(scanner).is_file():
            self.skipTest("Gitleaks and a Git-compatible sh are required")
        self.env = dict(os.environ, GITLEAKS_BIN=scanner)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                     "GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"):
            self.env.pop(name, None)
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Secret Hook Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(ROOT / ".githooks"))
        self.secret = "ghp_" + "".join(random.Random(9284).choices(
            string.ascii_letters + string.digits, k=36))

    def remove_directory(self):
        if self.directory.exists():
            if os.name == "nt":
                for path in self.directory.rglob("*"):
                    if path.is_file():
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)
            shutil.rmtree(self.directory)

    def tearDown(self):
        self.remove_directory()

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              capture_output=True, text=True, timeout=30, check=check)

    def hook(self, name):
        return subprocess.run([self.shell, str(ROOT / ".githooks" / name)], cwd=self.repo,
                              env=self.env, input="", capture_output=True, text=True, timeout=30)

    def test_clean_commit_and_history_pass(self):
        (self.repo / "safe.txt").write_text("no credentials here\n")
        self.git("add", "safe.txt")
        self.git("commit", "--quiet", "-m", "Clean fixture")
        self.assertEqual(self.hook("pre-push").returncode, 0)

    def test_staged_secret_blocks_commit_even_if_worktree_is_clean(self):
        path = self.repo / "credential.txt"
        path.write_text("token=" + self.secret + "\n")
        self.git("add", "credential.txt")
        path.write_text("removed from the worktree, but still staged\n")
        result = self.git("commit", "--quiet", "-m", "Must be blocked", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.secret, result.stdout + result.stderr)
        self.assertIn("leaks found", result.stdout + result.stderr)
        self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0)

    def test_deleted_secret_in_history_blocks_push(self):
        path = self.repo / "credential.txt"
        path.write_text("token=" + self.secret + "\n")
        self.git("add", "credential.txt")
        # Construct a deliberately bad history only in this disposable test repository.
        no_hooks = self.directory / "no-hooks"
        no_hooks.mkdir()
        self.git("-c", "core.hooksPath=" + str(no_hooks), "commit", "--quiet", "-m", "Synthetic fixture")
        path.unlink()
        self.git("add", "-u")
        self.git("commit", "--quiet", "-m", "Remove synthetic fixture")
        remote = self.directory / "remote.git"
        self.git("init", "--bare", "--quiet", str(remote))
        result = self.git("push", str(remote), "HEAD:refs/heads/main", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("leaks found", result.stdout + result.stderr)
        self.assertNotIn(self.secret, result.stdout + result.stderr)
        self.assertEqual(self.git("ls-remote", str(remote)).stdout.strip(), "")

    def test_missing_scanner_blocks_both_hooks(self):
        self.env["GITLEAKS_BIN"] = str(self.directory / "missing-gitleaks")
        for hook in ("pre-commit", "pre-push"):
            result = self.hook(hook)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Secret scan blocked", result.stderr)

    def test_scanner_errors_block_commit(self):
        self.env["GITLEAKS_CONFIG"] = str(self.directory / "missing-config.toml")
        (self.repo / "safe.txt").write_text("safe\n")
        self.git("add", "safe.txt")
        result = self.git("commit", "--quiet", "-m", "Scanner error", check=False)
        self.assertNotEqual(result.returncode, 0)
