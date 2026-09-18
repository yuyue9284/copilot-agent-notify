"""Windows Terminal progress sent to attached Zellij clients, outside their panes."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def progress_state(counts):
    if counts[1]:
        return 4
    return 3 if counts[0] else 0


def progress_sequence(state, completed=False):
    if completed and state != 0:
        raise ValueError("completion alert requires idle progress")
    sequence = "\x1b]9;4;%d;%d\x07" % (state, 100 if state == 4 else 0)
    return sequence + ("\x07" if completed else "")


def attaches_to(args, session):
    # Only explicitly named, interactive clients are safe to associate with a session.
    args = args[1:]
    if any(arg in ("--server", "action", "run", "plugin", "--create-background")
           for arg in args):
        return False
    for index, arg in enumerate(args):
        if arg in ("--session", "-s") and args[index + 1:index + 2] == [session]:
            return True
        if arg.startswith("--session=") and arg.split("=", 1)[1] == session:
            return True
        if arg in ("attach", "a"):
            remaining = [value for value in args[index + 1:]
                         if value not in ("--create", "-c", "--force-run-commands", "-f")]
            return remaining == [session]
    return False


def windows_args(command):
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(argv)


def write_windows(pid, text):
    # Attaching/freeing a console invalidates a console-backed coordinator's standard
    # handles. Keep that side effect in a disposable process with explicit pipe handles.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(pid), text],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
        timeout=8, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OSError("outer console helper failed: " + result.stderr.strip())


def _write_windows_console(pid, text):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WriteConsoleW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        handle = kernel.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            mode = wintypes.DWORD()
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.SetConsoleMode(handle, mode.value | 4):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                written = wintypes.DWORD()
                if not kernel.WriteConsoleW(handle, text, len(text), ctypes.byref(written), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if written.value != len(text):
                    raise OSError("incomplete outer progress write")
            finally:
                if not kernel.SetConsoleMode(handle, mode.value):
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    finally:
        kernel.FreeConsole()


class OuterProgress:
    def __init__(self, journal, session, process_table, process_token, read_json, atomic_json, log):
        enabled = os.environ.get("COPILOT_NOTIFY_OUTER_PROGRESS", "1") or "1"
        if enabled not in ("0", "1"):
            raise ValueError("COPILOT_NOTIFY_OUTER_PROGRESS must be 0 or 1")
        self.enabled = enabled == "1" and bool(os.environ.get("WT_SESSION"))
        self.journal = journal
        self.session = session
        self.process_table = process_table
        self.process_token = process_token
        self.atomic_json = atomic_json
        self.log = log
        self.owned = read_json(journal, {})
        self.targets = {}
        self.applied = {}
        self.next_discovery = 0
        self.last_error = None

    def discover(self):
        table = self.process_table()
        candidates = {pid: parent for pid, (parent, name) in table.items()
                      if Path(name).name.lower() in ("zellij", "zellij.exe")}
        commands = {}
        if os.name == "nt" and candidates:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; "
                 "Get-CimInstance Win32_Process -Filter \"Name='zellij.exe'\" | "
                 "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, encoding="utf-8", timeout=8, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            rows = json.loads(result.stdout) if result.stdout.strip() else []
            if isinstance(rows, dict):
                rows = [rows]
            commands = {row["ProcessId"]: windows_args(row["CommandLine"])
                        for row in rows if row.get("CommandLine")}
        result = {}
        for pid, parent in candidates.items():
            token = self.process_token(pid)
            if not token:
                continue
            if os.name == "nt":
                ancestors = set()
                ancestor = parent
                while ancestor in table and ancestor not in ancestors:
                    ancestors.add(ancestor)
                    if table[ancestor][1].lower() == "windowsterminal.exe":
                        break
                    ancestor = table[ancestor][0]
                else:
                    continue
                args = commands.get(pid, [])
                extra = {}
            elif sys.platform.startswith("linux"):
                proc = Path("/proc", str(pid))
                try:
                    if proc.stat().st_uid != os.getuid():
                        continue
                    env = (proc / "environ").read_bytes().split(b"\0")
                    if not any(value.startswith(b"WT_SESSION=") and value[11:] for value in env):
                        continue
                    args = [os.fsdecode(value) for value in (proc / "cmdline").read_bytes().split(b"\0")
                            if value]
                    terminal = proc / "fd/1"
                    stat = terminal.stat()
                    extra = {"terminal": str(terminal.resolve()), "device": stat.st_rdev}
                except (FileNotFoundError, ProcessLookupError):
                    continue
            else:
                continue
            if not attaches_to(args, self.session):
                continue
            result[str(pid)] = dict(pid=pid, token=token, parent=parent,
                                    parent_token=self.process_token(parent), **extra)
        return result

    def write(self, target, state, completed=False):
        pid = target["pid"]
        if self.process_token(pid) != target["token"]:
            if state or completed:
                self.next_discovery = 0
                raise ProcessLookupError("Zellij client exited before progress write")
            # After detach, clear only through the original, still-live parent console.
            pid = target["parent"]
            if not target["parent_token"] or self.process_token(pid) != target["parent_token"]:
                self.log.warning("outer progress restoration unavailable: client and parent exited")
                return
        text = progress_sequence(state, completed)
        if os.name == "nt":
            write_windows(pid, text)
            return
        path = Path("/proc", str(pid), "fd/1")
        if str(path.resolve()) != target["terminal"]:
            raise OSError("outer terminal changed; refusing progress write")
        fd = os.open(path, os.O_WRONLY | os.O_NOCTTY)
        try:
            if not os.isatty(fd) or os.fstat(fd).st_rdev != target["device"]:
                raise OSError("outer progress target is no longer the same terminal")
            data = text.encode("ascii")
            if os.write(fd, data) != len(data):
                raise OSError("incomplete outer progress write")
        finally:
            os.close(fd)

    def update(self, counts, closing=False, notify_completion=True):
        try:
            self._update(counts, closing, notify_completion)
            self.last_error = None
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            message = str(error)
            if message != self.last_error:
                self.log.error("outer progress: %s", message)
            self.last_error = message
        return self.last_error

    def _update(self, counts, closing, notify_completion):
        state = progress_state(counts) if self.enabled and not closing else 0
        if closing or not self.enabled:
            self.targets = {}
        elif time.monotonic() >= self.next_discovery:
            self.targets = self.discover()
            self.next_discovery = time.monotonic() + 5
        for key, target in list(self.owned.items()):
            if self.targets.get(key) != target:
                self.write(target, 0)
                del self.owned[key]
                self.applied.pop(key, None)
                self.atomic_json(self.journal, self.owned)
        for key, target in self.targets.items():
            if self.applied.get(key) == state:
                continue
            if state:
                # Record ownership before emitting so a restarted coordinator can clear it.
                self.owned[key] = target
                self.atomic_json(self.journal, self.owned)
            if state or key in self.owned:
                completed = notify_completion and state == 0 and self.applied.get(key) in (3, 4)
                if completed:
                    self.write(target, state, completed=True)
                else:
                    self.write(target, state)
                self.log.info("outer progress client=%s state=%d completed=%s", key, state, completed)
            self.applied[key] = state
            if not state and key in self.owned:
                del self.owned[key]
                self.atomic_json(self.journal, self.owned)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    parser.add_argument("sequence", choices=[progress_sequence(state) for state in (0, 3, 4)]
                        + [progress_sequence(0, completed=True)])
    args = parser.parse_args()
    if os.name != "nt" or args.pid <= 0:
        parser.error("a positive native Windows console PID is required")
    try:
        _write_windows_console(args.pid, args.sequence)
    except OSError as error:
        print("copilot-notify-progress: " + str(error), file=sys.stderr)
        sys.exit(1)
