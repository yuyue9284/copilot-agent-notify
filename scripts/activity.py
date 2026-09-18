#!/usr/bin/env python3
"""OS-local, single-writer Zellij activity indicators. Python standard library only."""

import argparse
import collections
import ctypes
import hashlib
import json
import logging
import logging.handlers
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from outer_progress import OuterProgress


LOG = logging.getLogger("copilot-notify-icons")
POLL_SECONDS = 1


def read_json(path, default=None):
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.025)


def atomic_json(path, value):
    scratch = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with scratch.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(scratch, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(0.025)
    finally:
        scratch.unlink(missing_ok=True)


class StatusProvider:
    """Shared transcript baseline with an optional, fail-closed SDK overlay."""

    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
        self.backend = "auto"
        self.configuration_error = None
        self.sdk_sessions = set()

    def refresh(self):
        try:
            backend = os.environ.get("COPILOT_NOTIFY_STATUS_BACKEND")
            if not backend:
                config = read_json(self.home / "copilot-agent-notify.json", {})
                if not isinstance(config, dict):
                    raise ValueError("expected a configuration object")
                backend = config.get("status_backend", "auto")
            if backend not in ("auto", "legacy"):
                raise ValueError("status_backend must be auto or legacy")
            self.backend = backend
            self.configuration_error = None
        except (OSError, ValueError) as error:
            self.configuration_error = "Status backend: " + str(error)

    def counts(self, directory, session_id, pid, baseline):
        return self.status(directory, session_id, pid, baseline)[0]

    def status(self, directory, session_id, pid, baseline):
        if self.configuration_error:
            raise ValueError(self.configuration_error)
        if self.backend == "legacy":
            return baseline, ""
        key = (str(directory), session_id, pid)
        missing = object()
        try:
            value = read_json(directory / "gadget-sdk.json", missing)
        except (OSError, ValueError) as error:
            self.sdk_sessions.add(key)
            raise ValueError("SDK bridge: cannot read status") from error
        if value is missing:
            if key in self.sdk_sessions:
                raise ValueError("SDK bridge: status disappeared; check the extension")
            return baseline, ""
        self.sdk_sessions.add(key)
        if (not isinstance(value, dict) or type(value.get("protocol_version")) is not int
                or value["protocol_version"] != 1 or value.get("session_id") != session_id
                or type(value.get("owner_pid")) is not int or value["owner_pid"] != pid
                or type(value.get("pending_shells")) is not int or value["pending_shells"] < 0
                or type(value.get("settling")) is not bool
                or type(value.get("updated_at")) not in (int, float)
                or not 0 <= value["updated_at"] <= 1e12):
            raise ValueError("SDK bridge: invalid snapshot or owner mismatch")
        age = time.time() - value["updated_at"]
        if age < -5 or age > 15:
            raise ValueError("SDK bridge: stale status; check the extension")
        if value.get("state") != "ready":
            raise ValueError("SDK bridge: unavailable; check the extension")
        latest = value.get("latest_activity", "")
        if not isinstance(latest, str) or len(latest) > 240:
            raise ValueError("SDK bridge: invalid latest activity")
        busy, attention = baseline
        return (int(bool(busy or value["pending_shells"] or value["settling"])), attention), latest

    def retain(self, owners):
        self.sdk_sessions.intersection_update(owners)


class Lock:
    def __init__(self, path):
        self.path = path
        self.stream = None
        self.handle = None

    def acquire(self, timeout=0):
        if os.name == "nt":
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            name = hashlib.sha256(str(self.path.resolve()).casefold().encode()).hexdigest()
            self.handle = self.kernel.CreateMutexW(None, False, "Global\\CopilotNotify-" + name)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            result = self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000))
            if result in (0, 0x80):  # Acquired, including an abandoned owner's mutex.
                return True
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            if result == 0x102:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b"\0")
            self.stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as error:
                if error.errno not in (11, 13, 35, 36):
                    self.stream.close()
                    self.stream = None
                    raise
                if time.monotonic() >= deadline:
                    self.stream.close()
                    self.stream = None
                    return False
                time.sleep(0.05)

    def release(self):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.stream:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        if not self.acquire(8):
            raise TimeoutError("timed out acquiring " + str(self.path))
        return self

    def __exit__(self, *args):
        self.release()


def process_token(pid):
    """Start identity, not just PID existence (zombies are not live owners)."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            fields = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
            return fields[19] if fields[0] != "Z" else None
        except (FileNotFoundError, ProcessLookupError):
            return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                            capture_output=True, text=True, timeout=3, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value and "Z" not in value.split()[-1] else None


def process_table():
    if os.name == "nt":
        from ctypes import wintypes

        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("ppid", wintypes.DWORD), ("priority", wintypes.LONG),
                        ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("cannot enumerate CLI ancestors")
        table = {}
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(entry)
            more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                table[entry.pid] = (entry.ppid, entry.exe.lower())
                more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        return table
    result = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True,
                            text=True, timeout=3, check=True)
    return {int(p): (int(parent), name) for p, parent, name in
            (line.strip().split(None, 2) for line in result.stdout.splitlines())}


def find_owner(transcript):
    table = process_table()
    ancestors = []
    pid = os.getppid()
    while pid in table and pid not in ancestors:
        ancestors.append(pid)
        pid = table[pid][0]
    # Never bind to an unrelated CLI that happens to have a stale lock file.
    for pid in ancestors:
        if (transcript.parent / ("inuse.%d.lock" % pid)).exists():
            token = process_token(pid)
            if token:
                return {"pid": pid, "token": token}
    # sessionStart can precede creation of both the transcript and its lock.
    for pid in ancestors:
        name = Path(table[pid][1]).name.lower()
        if name in ("copilot", "copilot.exe", "node", "node.exe"):
            token = process_token(pid)
            if token:
                return {"pid": pid, "token": token}
    raise RuntimeError("cannot identify the CLI owner; retry on the next prompt/stop hook")


def owner_alive(record):
    return process_token(record["pid"]) == record["token"]


class Activity:
    def __init__(self, session_id):
        self.session_id = session_id
        self.reset()

    def reset(self):
        self.root_busy = False
        self.children = set()
        self.child_tools = {}
        self.child_final = set()
        self.child_final_turns = {}
        self.child_stopped = set()
        self.child_stop_hooks = {}
        self.waiting = {}
        self.tools = {}
        self.stop_hooks = set()
        self.final = False
        self.final_turn = None
        self.stopped = False
        self.shutdown = False

    def resume_actor(self, actor):
        self.waiting.pop(actor, None)

    def complete_child(self, child):
        self.children.discard(child)
        self.resume_actor(child)
        self.child_final.discard(child)
        self.child_final_turns.pop(child, None)
        self.child_stopped.discard(child)
        self.child_stop_hooks = {key: value for key, value in self.child_stop_hooks.items()
                                 if value != child}

    def event(self, event):
        kind = event.get("type")
        data = event.get("data") or {}
        actor = event.get("agentId") or self.session_id
        root = actor == self.session_id
        if kind in ("session.start", "session.resume") and root:
            self.reset()
        elif kind == "session.shutdown" and root:
            self.reset()
            self.shutdown = True
        elif kind == "abort":
            if root:
                self.reset()
            else:
                self.complete_child(actor)
        elif kind == "subagent.started":
            child = event.get("agentId") or data.get("agentId") or data.get("toolCallId")
            if child:
                self.children.add(child)
                if data.get("toolCallId"):
                    self.child_tools[data["toolCallId"]] = child
        elif kind in ("subagent.completed", "subagent.failed"):
            child = self.child_tools.pop(data.get("toolCallId"), None) or (
                event.get("agentId") or data.get("agentId") or data.get("toolCallId"))
            self.complete_child(child)
            explicit_child = event.get("agentId") or data.get("agentId")
            if explicit_child:
                self.complete_child(explicit_child)
        elif kind == "user.message" and root:
            # Queued/background deliveries are not the start of a new root request.
            if data.get("delivery") == "idle" and data.get("source") in (None, "", "user"):
                self.root_busy = True
                self.final = self.stopped = False
                self.final_turn = None
                self.stop_hooks.clear()
                self.resume_actor(actor)
        elif kind == "assistant.turn_start":
            self.resume_actor(actor)
            if root:
                self.root_busy = True
                self.final = self.stopped = False
                self.final_turn = None
                self.stop_hooks.clear()
            else:
                # A completed background agent can receive another message and resume
                # without another subagent.started event.
                self.children.add(actor)
                self.child_final.discard(actor)
                self.child_final_turns.pop(actor, None)
                self.child_stopped.discard(actor)
                self.child_stop_hooks = {key: value for key, value in self.child_stop_hooks.items()
                                         if value != actor}
        elif kind == "assistant.message" and root:
            self.final = not bool(data.get("toolRequests"))
            self.final_turn = (data.get("turnId") if self.final
                               and data.get("phase") in (None, "final_answer") else None)
        elif kind == "assistant.message":
            if data.get("toolRequests"):
                self.child_final.discard(actor)
                self.child_final_turns.pop(actor, None)
            else:
                self.child_final.add(actor)
                if (data.get("turnId") is not None
                        and data.get("phase") in (None, "final_answer")):
                    self.child_final_turns[actor] = data["turnId"]
                else:
                    self.child_final_turns.pop(actor, None)
        elif kind == "assistant.turn_end" and root:
            if (self.final_turn is not None and data.get("turnId") == self.final_turn
                    and not self.stop_hooks and not self.tools.get(self.session_id)):
                self.stopped = True
        elif kind == "assistant.turn_end":
            if (actor in self.child_final_turns
                    and data.get("turnId") == self.child_final_turns[actor]
                    and actor not in self.child_stop_hooks.values() and not self.tools.get(actor)):
                self.child_stopped.add(actor)
        elif kind == "hook.start":
            payload = data.get("input") or {}
            hook = data.get("hookType")
            hook_actor = payload.get("sessionId") or actor
            if hook == "agentStop" and hook_actor == self.session_id:
                self.stop_hooks.add(data.get("hookInvocationId"))
                if payload.get("stopReason") in ("abort", "cancelled", "canceled", "user_cancelled"):
                    self.final = True
            elif hook == "agentStop":
                self.child_stop_hooks[data.get("hookInvocationId")] = hook_actor
            elif hook == "notification" and payload.get("notification_type") in (
                    "permission_prompt", "elicitation_dialog"):
                # Notifications may belong to a child even without top-level agentId.
                tool_id = payload.get("toolCallId")
                if tool_id:
                    self.waiting[hook_actor] = {tool_id}
                elif hook_actor not in self.waiting:
                    # Without a correlation ID only this actor's next turn proves
                    # permission was answered. An unrelated tool completion does not.
                    self.waiting[hook_actor] = None
        elif kind == "hook.end" and data.get("hookInvocationId") in self.stop_hooks:
            self.stop_hooks.discard(data.get("hookInvocationId"))
            self.stopped = True
            if data.get("success") is False:
                LOG.warning("agentStop hook failed for session %s; stop completion is "
                            "independent of hook success", self.session_id)
        elif kind == "hook.end" and data.get("hookInvocationId") in self.child_stop_hooks:
            child = self.child_stop_hooks.pop(data["hookInvocationId"])
            self.child_stopped.add(child)
            if data.get("success") is False:
                LOG.warning("child agentStop hook failed for session %s", child)
        elif kind == "tool.execution_start":
            tool_id = data.get("toolCallId")
            self.tools.setdefault(actor, set()).add(tool_id)
            if data.get("toolName", "").split(".")[-1] == "ask_user":
                self.waiting[actor] = {tool_id}
        elif kind == "tool.execution_complete":
            tool_id = data.get("toolCallId")
            self.tools.get(actor, set()).discard(tool_id)
            waiting = self.waiting.get(actor)
            if waiting and tool_id in waiting:
                waiting.discard(tool_id)
                if not waiting:
                    self.resume_actor(actor)
        if self.root_busy and self.final and self.stopped:
            self.root_busy = False
            self.resume_actor(self.session_id)
        for child in self.child_final & self.child_stopped:
            self.complete_child(child)

    def counts(self):
        actors = self.children | ({self.session_id} if self.root_busy else set())
        return (int(bool(actors - self.waiting.keys())), int(bool(self.waiting)))


class Transcript:
    def __init__(self, path, session_id, start_offset=0):
        self.path = Path(path)
        self.state = Activity(session_id)
        self.offset = 0
        self.identity = None
        self.anchor = b""
        self.validated = False
        self.malformed = False
        self.display_counts = (0, 0)
        self.start_offset = start_offset
        self.new_shutdown = False

    def poll(self, budget=4 * 1024 * 1024):
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return False
        with stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            stream.seek(max(0, self.offset - len(self.anchor)))
            changed = stream.read(len(self.anchor)) != self.anchor
            if identity != self.identity or stat.st_size < self.offset or changed:
                if self.identity is not None:
                    self.start_offset = 0
                self.offset = 0
                self.anchor = b""
                self.state.reset()
                self.validated = False
                self.identity = identity
                self.new_shutdown = False
            stream.seek(self.offset)
            start = self.offset
            caught_up = True
            while True:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                self.offset = stream.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    if not self.malformed:
                        LOG.warning("malformed transcript record: %s", self.path)
                        self.malformed = True
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("data", {}), dict):
                    raise ValueError("invalid transcript event shape")
                if not self.validated:
                    if event.get("type") != "session.start":
                        raise ValueError("transcript does not begin with session.start")
                    if event.get("data", {}).get("sessionId") != self.state.session_id:
                        raise ValueError("child session does not own this transcript")
                    self.validated = True
                self.state.event(event)
                if event.get("type") == "session.shutdown" and not event.get("agentId"):
                    self.new_shutdown = self.offset > self.start_offset
                if self.offset - start >= budget:
                    caught_up = self.offset >= stat.st_size
                    break
            stream.seek(max(0, self.offset - 256))
            self.anchor = stream.read(min(self.offset, 256))
            if self.validated and caught_up:
                self.display_counts = self.state.counts()
                return True
            return False


class Zellij:
    def __init__(self, binary, session):
        self.binary = binary
        self.session = session

    def action(self, *args):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run([self.binary, "--session", self.session, "action", *args],
                                capture_output=True, encoding="utf-8", timeout=4, check=False,
                                **options)
        if result.returncode:
            raise RuntimeError("zellij %s failed: %s" % (args[0], result.stderr.strip()[:500]))
        return result.stdout

    def snapshot(self):
        panes = json.loads(self.action("list-panes", "--json"))
        tabs = json.loads(self.action("list-tabs", "--json"))
        if not isinstance(panes, list) or not isinstance(tabs, list):
            raise ValueError("Zellij JSON listing requires a supported Zellij version")
        return ({str(p["id"]): p for p in panes if not p["is_plugin"] and not p["exited"]},
                {str(t["tab_id"]): t for t in tabs})

    def rename(self, kind, item_id, title):
        if kind == "pane":
            self.action("rename-pane", "--pane-id", "terminal_" + item_id, title)
        else:
            self.action("rename-tab-by-id", item_id, title)


def prefix(counts):
    busy, attention = counts
    parts = []
    if busy:
        parts.append("\u23f3")
    if attention:
        parts.append("\u26a0")
    return "[" + " | ".join(parts) + "] " if parts else ""


class Titles:
    def __init__(self, zellij, journal):
        self.zellij = zellij
        self.journal = journal
        self.owned = read_json(journal, {})

    def save(self):
        atomic_json(self.journal, self.owned)

    def update(self, panes, tabs, counts):
        current = {"pane:" + k: p["title"] for k, p in panes.items()}
        current.update({"tab:" + k: t["name"] for k, t in tabs.items()})
        for key in set(current) | set(self.owned):
            if key not in current:
                if key in self.owned:
                    del self.owned[key]
                    self.save()
                continue
            title = current[key]
            decoration = prefix(counts.get(key, (0, 0)))
            entry = self.owned.get(key)
            if entry and entry.get("pending") == title:
                entry["applied"] = title
                del entry["pending"]
                self.save()
            if entry and title not in (entry["applied"], entry["base"]):
                # Respect a rename by another writer for the rest of this busy period.
                entry.pop("pending", None)
                entry.update(base=title, applied=title, suppressed=True)
                self.save()
            elif entry and title == entry["base"] and entry["applied"] != title:
                # A user explicitly restored the base name; don't fight them either.
                entry.pop("pending", None)
                entry.update(applied=title, suppressed=True)
                self.save()
            if not decoration:
                if entry:
                    if title == entry["applied"] and not entry.get("suppressed"):
                        kind, item_id = key.split(":")
                        self.zellij.rename(kind, item_id, entry["base"])
                    del self.owned[key]
                    self.save()
                continue
            if entry and entry.get("suppressed"):
                continue
            if not entry:
                entry = {"base": title, "applied": title, "suppressed": False}
                self.owned[key] = entry
            desired = decoration + entry["base"]
            if desired != title:
                # Write-ahead ownership survives a coordinator crash after the rename.
                entry["pending"] = desired
                self.save()
                kind, item_id = key.split(":")
                self.zellij.rename(kind, item_id, desired)
                entry["applied"] = desired
                del entry["pending"]
                self.save()


def aggregate(registrations, readers, panes):
    result = collections.defaultdict(lambda: [0, 0])
    for key, record in registrations.items():
        pane = panes.get(record["pane_id"])
        reader = readers.get(key)
        if not pane or not reader or not reader.validated:
            continue
        values = reader.display_counts
        for target in ("pane:" + record["pane_id"], "tab:" + str(pane["tab_id"])):
            for index, value in enumerate(values):
                result[target][index] += value
    return dict(result)


def state_directory(session):
    if os.environ.get("COPILOT_NOTIFY_STATE_DIR"):
        base = Path(os.environ["COPILOT_NOTIFY_STATE_DIR"])
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CopilotAgentNotify"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "copilot-agent-notify"
    # Even an explicitly shared directory must not merge WSL and native Windows.
    key = hashlib.sha256((sys.platform + "\0" + session).encode()).hexdigest()[:24]
    return base / "zellij" / key


def setup_log(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.handlers.RotatingFileHandler(directory / "coordinator.log",
                                                  maxBytes=262144, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def run_worker(directory, session, binary):
    setup_log(directory)
    writer = Lock(directory / "writer.lock")
    readers = {}
    identities = {}
    last_error = None
    provider = StatusProvider()
    previous_status_error = False
    with Lock(directory / "registry.lock"):
        if not writer.acquire():
            return 0
    LOG.info("coordinator started pid=%d session=%s", os.getpid(), session)
    try:
        zellij = Zellij(binary, session)
        titles = Titles(zellij, directory / "titles.json")
        outer = OuterProgress(directory / "outer-progress.json", session,
                              process_table, process_token, read_json, atomic_json, LOG)
        while True:
            try:
                with Lock(directory / "registry.lock"):
                    records = read_json(directory / "registrations.json", {})
                    for key, record in list(records.items()):
                        if not owner_alive(record):
                            del records[key]
                    atomic_json(directory / "registrations.json", records)
                    if not records:
                        cleanup_error = None
                        try:
                            panes, tabs = zellij.snapshot()
                            titles.update(panes, tabs, {})
                        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                            cleanup_error = str(error)
                            LOG.error("final restoration unavailable: %s", cleanup_error)
                        outer_error = outer.update((0, 0), closing=True)
                        atomic_json(directory / "status.json", {
                            "pid": os.getpid(), "heartbeat": time.time(), "running": False,
                            "registrations": 0, "error": cleanup_error, "outer_error": outer_error})
                        # Release before letting another registration through.
                        writer.release()
                        LOG.info("coordinator stopped: no live registrations")
                        return 1 if cleanup_error or outer_error else 0
                panes, tabs = zellij.snapshot()
                provider.refresh()
                ready = {}
                obsolete = {}
                status_errors = []
                status_owners = set()
                for key, record in records.items():
                    identity = (record["pid"], record["token"], record["transcript"])
                    if identities.get(key) != identity:
                        readers[key] = Transcript(record["transcript"], record["session_id"],
                                                  record["start_offset"])
                        identities[key] = identity
                    reader = readers[key]
                    if record["pane_id"] not in panes:
                        obsolete[key] = record
                        continue
                    try:
                        caught_up = reader.poll()
                    except ValueError as error:
                        LOG.warning("discard registration %s: %s", key, error)
                        obsolete[key] = record
                        continue
                    if not reader.validated and time.time() - record["created"] > 60:
                        LOG.warning("discard registration %s: transcript unavailable after startup grace", key)
                        obsolete[key] = record
                        continue
                    if caught_up:
                        if reader.state.shutdown and reader.new_shutdown:
                            obsolete[key] = record
                        else:
                            status_directory = Path(record["transcript"]).parent
                            status_owners.add((str(status_directory), record["session_id"], record["pid"]))
                            try:
                                reader.display_counts = provider.counts(
                                    status_directory, record["session_id"], record["pid"],
                                    reader.display_counts)
                                reader.status_error = None
                            except ValueError as error:
                                reader.status_error = "%s: %s" % (record["session_id"], error)
                                status_errors.append(reader.status_error)
                                reader.display_counts = (reader.display_counts[0], 1)
                            ready[key] = record
                    elif reader.validated:
                        ready[key] = record
                        if getattr(reader, "status_error", None):
                            status_errors.append(reader.status_error)
                        status_owners.add((str(Path(record["transcript"]).parent),
                                           record["session_id"], record["pid"]))
                provider.retain(status_owners)
                if obsolete:
                    with Lock(directory / "registry.lock"):
                        latest = read_json(directory / "registrations.json", {})
                        for key, record in obsolete.items():
                            if latest.get(key) == record:
                                del latest[key]
                        atomic_json(directory / "registrations.json", latest)
                titles.update(panes, tabs, aggregate(ready, readers, panes))
                outer_counts = tuple(sum(readers[key].display_counts[index] for key in ready)
                                     for index in (0, 1))
                outer_error = outer.update(
                    outer_counts, notify_completion=not status_errors and not previous_status_error)
                # Keep recovery quiet if clearing a terminal failed and must be retried.
                previous_status_error = bool(status_errors) or (previous_status_error and bool(outer_error))
                for key in set(readers) - set(records):
                    del readers[key]
                    del identities[key]
                status_error = "; ".join(status_errors) or None
                if status_error and status_error != last_error:
                    LOG.error("status: %s", status_error)
                atomic_json(directory / "status.json", {
                    "pid": os.getpid(), "token": process_token(os.getpid()),
                    "heartbeat": time.time(), "running": True,
                    "registrations": len(records) - len(obsolete), "error": status_error,
                    "outer_error": outer_error, "status_backend": provider.backend})
                last_error = status_error
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                message = str(error)
                if message != last_error:
                    LOG.error("coordinator: %s", message)
                    last_error = message
                atomic_json(directory / "status.json", {
                    "pid": os.getpid(), "token": process_token(os.getpid()),
                    "heartbeat": time.time(), "running": True, "error": message})
                # Retain ownership on transient RPC failure; no stale-work timeouts.
            time.sleep(POLL_SECONDS)
    except Exception:
        LOG.exception("coordinator terminated unexpectedly")
        raise
    finally:
        writer.release()


def hook(event, payload):
    session = os.environ.get("ZELLIJ_SESSION_NAME")
    pane = os.environ.get("ZELLIJ_PANE_ID")
    if not session or pane is None:
        return 0
    enabled = os.environ.get("COPILOT_NOTIFY_ICONS", "1") or "1"
    if enabled == "0":
        return 0
    if enabled != "1":
        raise ValueError("COPILOT_NOTIFY_ICONS must be 0 or 1")
    if not pane.isdigit():
        raise ValueError("ZELLIJ_PANE_ID must be a terminal pane number")
    session_id = payload.get("sessionId", "")
    if not session_id or Path(session_id).name != session_id or session_id in (".", ".."):
        raise ValueError("missing or invalid sessionId")
    home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
    transcript = Path(payload.get("transcriptPath") or home / "session-state" / session_id / "events.jsonl")
    try:
        with transcript.open("rb") as stream:
            line = stream.readline()
        if line.endswith(b"\n"):
            header = json.loads(line)
            if header.get("type") != "session.start":
                raise ValueError("invalid transcript session header")
            if header.get("data", {}).get("sessionId") != session_id:
                return 0
    except FileNotFoundError:
        pass
    owner = find_owner(transcript)
    binary = os.environ.get("COPILOT_NOTIFY_ZELLIJ") or shutil.which("zellij")
    if not binary or not shutil.which(binary):
        raise RuntimeError("Zellij executable not found; set COPILOT_NOTIFY_ZELLIJ")
    directory = state_directory(session)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(session_id.encode()).hexdigest()
    try:
        start_offset = transcript.stat().st_size
    except FileNotFoundError:
        start_offset = 0
    record = dict(owner, session_id=session_id, pane_id=pane, start_offset=start_offset,
                  transcript=str(transcript.resolve()), created=time.time())
    with Lock(directory / "registry.lock"):
        records = read_json(directory / "registrations.json", {})
        if event == "sessionEnd":
            previous = records.get(key)
            if previous and all(previous.get(k) == owner[k] for k in ("pid", "token")):
                del records[key]
        else:
            previous = records.get(key)
            if previous and all(previous.get(k) == owner[k] for k in ("pid", "token")):
                record["created"] = previous["created"]
                record["start_offset"] = previous["start_offset"]
            records[key] = record
        atomic_json(directory / "registrations.json", records)
    command = [sys.executable, str(Path(__file__).resolve()), "worker",
               "--directory", str(directory.resolve()), "--session", session, "--zellij", binary]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    child = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = read_json(directory / "status.json", {})
        if (status.get("running") and status.get("heartbeat", 0) > time.time() - 5
                and status.get("token") and owner_alive(status)):
            if status.get("error"):
                raise RuntimeError(status["error"] + "; see " + str(directory / "coordinator.log"))
            return 0
        if event == "sessionEnd" and child.poll() == 0:
            return 0
        if child.poll() not in (None, 0):
            break
        time.sleep(0.05)
    raise RuntimeError("coordinator startup not confirmed; see " + str(directory / "coordinator.log"))


def main():
    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9+ is required; install it or set COPILOT_NOTIFY_ICONS=0")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("hook")
    h.add_argument("event", choices=["sessionStart", "userPromptSubmitted", "agentStop", "sessionEnd"])
    worker = sub.add_parser("worker")
    worker.add_argument("--directory", type=Path, required=True)
    worker.add_argument("--session", required=True)
    worker.add_argument("--zellij", required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "worker":
        return run_worker(args.directory, args.session, args.zellij)
    if args.command == "status":
        directory = state_directory(os.environ.get("ZELLIJ_SESSION_NAME", ""))
        status = read_json(directory / "status.json", {})
        status["alive"] = bool(status.get("token") and owner_alive(status))
        print(json.dumps(status, indent=2))
        return 0
    return hook(args.event, json.load(sys.stdin))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("copilot-notify-icons: " + str(exc), file=sys.stderr)
        sys.exit(1)
