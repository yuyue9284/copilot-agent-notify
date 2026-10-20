"""Change-driven lock discovery with a complete-scan fallback."""

import ctypes
import errno
import os
from pathlib import Path
import struct
import sys
import time


def relevant(name):
    return name == "events.jsonl" or (name.startswith("inuse.") and name.endswith(".lock"))


class WatchOverflow(OSError):
    pass


class LinuxChanges:
    def __init__(self, root):
        self.root = root
        self.api = ctypes.CDLL(None, use_errno=True)
        self.api.statfs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        filesystem = ctypes.create_string_buffer(256)
        if self.api.statfs(os.fsencode(root), filesystem) != 0:
            raise OSError(ctypes.get_errno(), "cannot inspect session filesystem")
        magic = ctypes.c_long.from_buffer(filesystem).value
        # Remote/translated filesystems can accept a watch without reporting
        # changes made by another OS. Only use the index on known local types.
        if magic not in (0xEF53, 0x9123683E, 0x01021994, 0x794C7630, 0x58465342,
                         0x2FC12FC1, 0x858458F6):
            raise OSError(errno.ENOTSUP, "session filesystem requires complete scans")
        self.api.inotify_init1.argtypes = [ctypes.c_int]
        self.api.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.api.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.fd = self.api.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "cannot create directory watcher")
        self.names = {}
        try:
            self.add("")
        except OSError:
            self.close()
            raise

    def add(self, name):
        if name in self.names.values():
            return
        if len(self.names) >= 8192:
            raise OSError(errno.ENOSPC, "directory watch budget exceeded")
        path = self.root / name
        descriptor = self.api.inotify_add_watch(
            self.fd, os.fsencode(path), 0x100 | 0x200 | 0x40 | 0x80 | 0x400 | 0x800)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), "cannot watch session directory")
        if descriptor in self.names and self.names[descriptor] != name:
            raise OSError(errno.ENOTSUP, "aliased session directories require complete scans")
        self.names[descriptor] = name

    def remove(self, name):
        for descriptor, value in list(self.names.items()):
            if value == name:
                del self.names[descriptor]
                self.api.inotify_rm_watch(self.fd, descriptor)

    def drain(self):
        dirty = set()
        for _ in range(32):
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return dirty
            position = 0
            while position < len(data):
                descriptor, mask, _, length = struct.unpack_from("iIII", data, position)
                position += 16
                name = os.fsdecode(data[position:position + length].split(b"\0", 1)[0])
                position += length
                if mask & 0x4000:
                    raise WatchOverflow("directory change queue overflow")
                parent = self.names.get(descriptor)
                if parent is None:
                    continue
                if mask & (0x400 | 0x800 | 0x8000):
                    if parent == "":
                        raise WatchOverflow("watched root moved or disappeared")
                    self.remove(parent)
                    dirty.add(parent)
                    continue
                if parent == "":
                    if mask & 0x40000000:
                        dirty.add(name)
                elif relevant(name):
                    dirty.add(parent)
        raise WatchOverflow("directory change queue needs reconciliation")

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class WindowsChanges:
    def __init__(self, root):
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                        ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
                        ("event", wintypes.HANDLE)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.HANDLE]
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateEventW.restype = wintypes.HANDLE
        self.api.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(Overlapped), ctypes.c_void_p]
        self.api.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        self.api.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
        self.api.ResetEvent.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateFileW(str(root), 1, 7, None, 3, 0x42000000, None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        self.overlapped = Overlapped()
        self.overlapped.event = self.api.CreateEventW(None, True, False, None)
        self.buffer = ctypes.create_string_buffer(65536)
        self.pending = False
        try:
            if not self.overlapped.event:
                raise ctypes.WinError(ctypes.get_last_error())
            self.arm()
        except OSError:
            self.close()
            raise

    def arm(self):
        self.api.ResetEvent(self.overlapped.event)
        if not self.api.ReadDirectoryChangesW(
                self.handle, self.buffer, len(self.buffer), True, 3, None,
                ctypes.byref(self.overlapped), None):
            raise ctypes.WinError(ctypes.get_last_error())
        self.pending = True

    def add(self, name):
        pass  # One recursive OS watch covers the whole session root.

    def remove(self, name):
        pass

    def drain(self):
        from ctypes import wintypes
        dirty = set()
        for _ in range(32):
            size = wintypes.DWORD()
            if not self.api.GetOverlappedResult(
                    self.handle, ctypes.byref(self.overlapped), ctypes.byref(size), False):
                error = ctypes.get_last_error()
                if error == 996:
                    return dirty
                raise ctypes.WinError(error)
            self.pending = False
            data = self.buffer.raw[:size.value]
            self.arm()
            if not data:
                raise WatchOverflow("directory change queue overflow")
            position = 0
            while True:
                following, _, length = struct.unpack_from("III", data, position)
                parts = data[position + 12:position + 12 + length].decode("utf-16-le").split("\\")
                if len(parts) == 1 or (len(parts) == 2 and relevant(parts[1])):
                    dirty.add(parts[0])
                if not following:
                    break
                position += following
        raise WatchOverflow("directory change queue needs reconciliation")

    def close(self):
        if self.handle:
            if self.pending:
                self.api.CancelIoEx(self.handle, ctypes.byref(self.overlapped))
                # Keep the OVERLAPPED and buffer alive until cancellation completes.
                from ctypes import wintypes
                size = wintypes.DWORD()
                self.api.GetOverlappedResult(
                    self.handle, ctypes.byref(self.overlapped), ctypes.byref(size), True)
            self.api.CloseHandle(self.handle)
            self.handle = None
        if self.overlapped.event:
            self.api.CloseHandle(self.overlapped.event)
            self.overlapped.event = None


class DiscoveryIndex:
    def __init__(self, root):
        self.root = Path(root)
        self.watcher = None
        self.identity = None
        self.entries = {}
        self.retry_at = 0
        self.error = None

    def close(self):
        if self.watcher:
            self.watcher.close()
            self.watcher = None
        self.identity = None
        self.entries.clear()

    def __del__(self):
        self.close()

    def scan_directory(self, name):
        path = self.root / name
        try:
            if not path.is_dir():
                self.entries.pop(name, None)
                if self.watcher:
                    self.watcher.remove(name)
                return
            if self.watcher:
                metadata = path.stat()
                if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise OSError(errno.ENOTSUP, "linked session directories require complete scans")
                if metadata.st_dev != self.identity[0]:
                    raise OSError(errno.ENOTSUP, "mounted session directories require complete scans")
                self.watcher.add(name)
            with os.scandir(path) as files:
                self.entries[name] = tuple(path / file.name for file in files
                                           if file.name.startswith("inuse.")
                                           and file.name.endswith(".lock"))
        except FileNotFoundError:
            self.entries.pop(name, None)

    def full_scan(self):
        with os.scandir(self.root) as directories:
            names = {entry.name for entry in directories if entry.is_dir()}
        for name in set(self.entries) - names:
            self.entries.pop(name)
            if self.watcher:
                self.watcher.remove(name)
        for name in names:
            self.scan_directory(name)

    def locks(self):
        try:
            stat = self.root.stat()
        except FileNotFoundError:
            self.close()
            self.error = None
            return ()
        identity = (stat.st_dev, stat.st_ino)
        if identity != self.identity:
            self.close()
            self.identity = identity
            self.retry_at = 0
        try:
            if self.watcher is None:
                if time.monotonic() >= self.retry_at:
                    if os.name == "nt":
                        self.watcher = WindowsChanges(self.root)
                    elif sys.platform.startswith("linux"):
                        self.watcher = LinuxChanges(self.root)
                    else:
                        raise OSError(errno.ENOTSUP, "directory notifications unsupported")
                    self.error = None
                self.full_scan()
            if self.watcher:
                for _ in range(4):
                    dirty = self.watcher.drain()
                    if not dirty:
                        break
                    for name in dirty:
                        self.scan_directory(name)
                else:
                    raise WatchOverflow("directory changes exceeded reconciliation budget")
        except OSError as error:
            self.close()
            self.identity = identity
            self.retry_at = time.monotonic() + 60
            self.error = "Directory notifications unavailable; using complete scans: " + str(error)
            self.full_scan()
        return tuple(path for paths in self.entries.values() for path in paths)
