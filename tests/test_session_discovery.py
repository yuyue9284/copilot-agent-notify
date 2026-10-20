import errno
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from session_discovery import DiscoveryIndex, WatchOverflow


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "session-state"
        self.root.mkdir()
        self.index = DiscoveryIndex(self.root)
        self.addCleanup(self.index.close)

    def lock(self, name, pid=42):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        path = directory / ("inuse.%d.lock" % pid)
        path.touch()
        return path

    def changed(self, expected):
        # Windows completes directory notification requests asynchronously.
        deadline = time.monotonic() + 2
        while True:
            paths = set(self.index.locks())
            if paths == expected:
                return
            if time.monotonic() >= deadline:
                self.fail("directory notifications did not reconcile")
            time.sleep(.01)

    def test_warm_index_never_enumerates_unchanged_archives(self):
        for i in range(100):
            (self.root / ("archive-%d" % i)).mkdir()
        lock = self.lock("live")
        self.changed({lock})
        self.assertIsNotNone(self.index.watcher, self.index.error)
        with patch.object(self.index, "full_scan", wraps=self.index.full_scan) as full, \
                patch.object(self.index, "scan_directory", wraps=self.index.scan_directory) as scan:
            self.changed({lock})
            self.assertEqual(full.call_count, 0)
            self.assertEqual(scan.call_count, 0)

    def test_new_lock_in_old_directory_removal_and_move(self):
        (self.root / "old").mkdir()
        self.changed(set())
        lock = self.lock("old")
        self.changed({lock})
        second = self.lock("new", 43)
        self.changed({lock, second})
        lock.unlink()
        self.changed({second})
        second.parent.rename(self.root / "moved")
        self.changed({self.root / "moved" / second.name})

    def test_transcript_appends_and_sdk_replacements_do_not_scan_archives(self):
        lock = self.lock("live")
        self.changed({lock})
        transcript = lock.parent / "events.jsonl"
        transcript.write_text("{}\n")
        self.changed({lock})
        time.sleep(.02)
        with patch.object(self.index, "full_scan", wraps=self.index.full_scan) as full:
            with transcript.open("a") as stream:
                stream.write("{}\n")
            (lock.parent / "sdk.tmp").write_text("{}")
            os.replace(lock.parent / "sdk.tmp", lock.parent / "gadget-sdk.json")
            self.changed({lock})
            self.assertEqual(full.call_count, 0)

    def test_missing_and_replaced_root(self):
        old = self.lock("old")
        self.changed({old})
        self.root.rename(self.root.with_name("old-root"))
        self.root.mkdir()
        new = self.lock("new")
        self.changed({new})

    def test_overflow_and_failure_scan_correctly_and_recover(self):
        old = self.lock("old")
        self.changed({old})
        watcher = self.index.watcher
        self.assertIsNotNone(watcher, self.index.error)
        new = self.lock("new")
        with patch.object(watcher, "drain", side_effect=WatchOverflow("synthetic overflow")):
            self.changed({old, new})
        self.assertIn("synthetic overflow", self.index.error)
        self.assertIsNone(self.index.watcher)
        another = self.lock("another")
        self.changed({old, new, another})
        self.index.retry_at = 0
        self.changed({old, new, another})
        self.assertIsNone(self.index.error)
        self.assertIsNotNone(self.index.watcher)

    def test_full_scan_errors_are_not_false_empty_success(self):
        self.lock("live")
        with patch("session_discovery.os.scandir", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(PermissionError):
                self.index.locks()

    def test_unsupported_notifications_keep_scanning_without_retry_churn(self):
        old = self.lock("old")
        backend = "WindowsChanges" if os.name == "nt" else "LinuxChanges"
        with patch("session_discovery." + backend,
                   side_effect=OSError(errno.ENOTSUP, "unsupported filesystem")) as create:
            self.changed({old})
            new = self.lock("new")
            self.changed({old, new})
            self.assertEqual(create.call_count, 1)
            self.assertIsNone(self.index.watcher)
            self.assertIn("unsupported filesystem", self.index.error)

    @unittest.skipIf(os.name == "nt", "creating native symlinks requires extra privileges")
    def test_linked_directories_use_complete_scans(self):
        directory = self.root.parent / "linked"
        directory.mkdir()
        (self.root / "alias").symlink_to(directory, target_is_directory=True)
        self.changed(set())
        self.assertIsNone(self.index.watcher)
        lock = directory / "inuse.42.lock"
        lock.touch()
        self.changed({self.root / "alias" / lock.name})
        self.assertIn("linked session directories", self.index.error)

    def test_close_is_idempotent_and_releases_resources(self):
        self.lock("live")
        self.index.locks()
        watcher = self.index.watcher
        self.index.close()
        self.index.close()
        if hasattr(watcher, "fd"):
            self.assertEqual(watcher.fd, -1)
        else:
            self.assertIsNone(watcher.handle)

    def test_repeated_start_stop_does_not_leak_os_handles(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.GetCurrentProcess.restype = wintypes.HANDLE
            api.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

            def count():
                result = wintypes.DWORD()
                if not api.GetProcessHandleCount(api.GetCurrentProcess(), ctypes.byref(result)):
                    raise ctypes.WinError(ctypes.get_last_error())
                return result.value
        else:
            def count():
                return len(list(Path("/proc/self/fd").iterdir()))
        self.lock("live")
        before = count()
        for _ in range(40):
            index = DiscoveryIndex(self.root)
            try:
                self.assertEqual(len(index.locks()), 1)
            finally:
                index.close()
        self.assertLessEqual(count(), before + 1)


if __name__ == "__main__":
    unittest.main()
