#!/usr/bin/env python3
"""Read-only discovery of this OS user's live Copilot root sessions."""

import argparse
import errno
import json
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import threading
import time

from activity import (Activity, BridgeStarting, StatusProvider, Transcript, atomic_json,
                      process_token, read_json, state_directory)


BACKGROUND_COMPLETION_GRACE_SECONDS = 10


class ActivityRevision(Activity):
    """Stable activity identity across polling and transcript replay."""

    def reset(self):
        super().reset()
        self.activity_revision = ""
        self.live = False
        self.completion_deadline = None

    def counts(self):
        busy, attention = super().counts()
        if (not busy and not attention and self.completion_deadline is not None
                and time.monotonic() < self.completion_deadline):
            return (1, 0)
        return (busy, attention)

    def event(self, event):
        previous = self.activity_revision
        had_children = bool(self.children)
        super().event(event)
        if any(super().counts()):
            self.completion_deadline = None
        elif self.live and had_children and not self.children:
            # A child's completion can wake its parent a few seconds later.
            self.completion_deadline = time.monotonic() + BACKGROUND_COMPLETION_GRACE_SECONDS
        self.activity_revision = previous
        kind = event.get("type")
        data = event.get("data") or {}
        meaningful = kind in ("user.message", "assistant.turn_start", "tool.execution_start")
        if kind == "hook.start":
            meaningful = (data.get("hookType") == "notification"
                          and (data.get("input") or {}).get("notification_type")
                          in ("permission_prompt", "elicitation_dialog"))
        if meaningful:
            # IDs/timestamps distinguish repeated identical prompts without
            # exposing their contents to the window or its saved dismissals.
            timestamp = event.get("timestamp")
            # Legacy records without timestamps still have a stable identity.
            # Timestamped records additionally prevent an older rewritten history
            # from being mistaken for new work after a dismissal.
            stamp = 0
            if isinstance(timestamp, str):
                moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if moment.tzinfo is not None:
                    stamp = int((moment - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000000)
            digest = hashlib.sha256(
                json.dumps(event, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
            self.activity_revision = "%020d/%s" % (stamp, digest)


def started_at(token):
    if os.name == "nt":
        return int(token) / 10000000 - 11644473600
    if sys.platform.startswith("linux"):
        boot = next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        return int(boot) + int(token) / os.sysconf("SC_CLK_TCK")
    raise OSError("Session discovery supports Windows and Linux only")


def boot_identity():
    if sys.platform.startswith("linux"):
        return "linux:" + Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    # Windows process tokens are absolute creation FILETIMEs, not boot-relative ticks.
    return sys.platform


def workspace_name(path):
    """Read only the scalar name written by Copilot, not arbitrary YAML."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        if line.startswith("name: "):
            value = line[6:].strip()
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            if isinstance(value, str) and value not in ("", "|", ">", "null", "~"):
                return " ".join(value.split())[:160]
    return None


class Scanner:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
        self.status_provider = StatusProvider(self.home)
        self.readers = {}
        self.cache_root = state_directory("").parent.parent
        scope = hashlib.sha256(str(self.home.resolve()).encode("utf-8")).hexdigest()[:24]
        self.identity_path = self.cache_root / "gadget-owners" / (scope + ".json")
        self.identities = {}
        self.identity_save_pending = False

    def registered_owners(self, errors):
        owners = set()
        for path in (self.cache_root / "zellij").glob("*/registrations.json"):
            try:
                records = read_json(path, {})
                if not isinstance(records, dict):
                    raise ValueError("invalid activity registration file")
                for record in records.values():
                    if not isinstance(record, dict):
                        raise ValueError("invalid activity registration")
                    if (isinstance(record.get("session_id"), str)
                            and isinstance(record.get("pid"), int)
                            and isinstance(record.get("token"), str)
                            and isinstance(record.get("transcript"), str)):
                        owners.add((record["session_id"], record["pid"], record["token"],
                                    str(Path(record["transcript"]).resolve())))
            except (OSError, ValueError) as error:
                errors.append("Owner registration %s: %s" % (path, error))
        return owners

    def cached_owners(self, errors):
        try:
            owners = read_json(self.identity_path, None)
            if owners is None:
                self.identity_save_pending = bool(self.identities)
                return self.identities
            if not isinstance(owners, dict) or any(
                    not isinstance(value, dict) or not isinstance(value.get("token"), str)
                    or not isinstance(value.get("boot"), str)
                    or not isinstance(value.get("lock"), list) or len(value["lock"]) != 4
                    or any(not isinstance(part, int) for part in value["lock"])
                    for value in owners.values()):
                raise ValueError("invalid process identity cache")
            return owners
        except (OSError, ValueError) as error:
            errors.append("Process identity cache: " + str(error))
            self.identity_save_pending = True
            return self.identities

    def snapshot(self):
        self.status_provider.refresh()
        sessions = []
        errors = []
        owners = {}
        tokens = {}
        births = {}
        boot = boot_identity()
        cached = self.cached_owners(errors)
        retained = {}
        registered = None
        state = self.home / "session-state"
        # A CLI can own several root sessions concurrently. Locks establish
        # liveness, not session identity; only deduplicate owners of the same root.
        for lock in state.glob("*/inuse.*.lock"):
            try:
                pid = int(lock.name.split(".")[1])
                stat = lock.stat()
                fingerprint = [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns]
                identity_key = lock.parent.name + "/" + str(pid)
                known = cached.get(identity_key)
                if known and known["lock"] == fingerprint:
                    # Retain a rejected identity while its old lock exists, so a
                    # later poll cannot accidentally re-adopt a recycled PID.
                    retained[identity_key] = known
                if pid not in tokens:
                    tokens[pid] = process_token(pid)
                token = tokens[pid]
                if not token:
                    continue
                transcript = lock.parent / "events.jsonl"
                if not transcript.is_file():
                    continue
                if known and known["lock"] == fingerprint:
                    if known["token"] != token or known["boot"] != boot:
                        continue
                else:
                    if pid not in births:
                        births[pid] = started_at(token)
                    if stat.st_mtime + 2 < births[pid]:
                        # An already-verified hook registration can recover an
                        # owner first seen by this scanner after a WSL clock shift.
                        if registered is None:
                            registered = self.registered_owners(errors)
                        if (lock.parent.name, pid, token, str(transcript.resolve())) not in registered:
                            continue
                retained[identity_key] = {"token": token, "boot": boot, "lock": fingerprint}
                owners.setdefault(lock.parent, []).append((pid, token))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                errors.append("%s: %s" % (lock.parent.name, error))
        if retained != cached or self.identity_save_pending:
            try:
                self.identity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_json(self.identity_path, retained)
                self.identity_save_pending = False
            except OSError as error:
                self.identity_save_pending = True
                errors.append("Cannot save process identities: " + str(error))
        self.identities = retained
        keep = set()
        status_owners = set()
        for directory, candidates in sorted(owners.items()):
            session_id = directory.name
            owner = min(candidates)
            key = (session_id, owner)
            keep.add(key)
            status_owners.add((str(directory), session_id, owner[0]))
            row = {"id": session_id, "pid": owner[0], "title": session_id[:8],
                   "cwd": "", "status": "Loading", "activity_revision": None, "latest_activity": ""}
            try:
                reader = self.readers.get(key)
                if reader is None:
                    path = directory / "events.jsonl"
                    with path.open(encoding="utf-8") as stream:
                        header = json.loads(stream.readline())
                    if (header.get("type") != "session.start"
                            or header.get("data", {}).get("sessionId") != session_id):
                        raise ValueError("not a root session transcript")
                    context = header["data"].get("context") or {}
                    transcript = Transcript(path, session_id)
                    transcript.state = ActivityRevision(session_id)
                    reader = (transcript, str(context.get("cwd") or ""))
                    self.readers[key] = reader
                transcript, row["cwd"] = reader
                row["title"] = workspace_name(directory / "workspace.yaml") or session_id[:8]
                ready = transcript.poll()
                if transcript.malformed:
                    row["status"] = "Unknown"
                    errors.append("%s: malformed transcript records" % session_id)
                elif ready:
                    if transcript.state.shutdown:
                        continue
                    try:
                        counts, row["latest_activity"] = self.status_provider.status(
                            directory, session_id, owner[0], transcript.display_counts)
                        busy, attention = counts
                        row["status"] = "Needs input" if attention else "In progress" if busy else "Done"
                    except BridgeStarting:
                        row["status"] = "Needs input" if transcript.display_counts[1] else "Loading"
                    except ValueError as error:
                        row["status"] = "Unknown"
                        errors.append("%s: %s" % (session_id, error))
                    row["activity_revision"] = transcript.state.activity_revision
                    # Initial/replaced history is a baseline, not a live completion.
                    transcript.state.live = True
            except (OSError, ValueError, TypeError, AttributeError) as error:
                row["status"] = "Unknown"
                errors.append("%s: %s" % (session_id, error))
                self.readers.pop(key, None)
            sessions.append(row)
        self.readers = {key: reader for key, reader in self.readers.items() if key in keep}
        self.status_provider.retain(status_owners)
        return {"sessions": sessions, "errors": errors}


def write_pipe(descriptor, data):
    if os.name != "nt":
        return os.write(descriptor, data)
    # CRT os.write maps both a disconnected pipe and invalid arguments to
    # EINVAL. Preserve Win32's exact error rather than masking genuine failures.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.WriteFile.restype = wintypes.BOOL
    written = wintypes.DWORD()
    buffer = ctypes.create_string_buffer(data)
    if not kernel32.WriteFile(msvcrt.get_osfhandle(descriptor), buffer, len(data),
                              ctypes.byref(written), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return written.value


def watch():
    """Stream versioned snapshots until the owning process closes stdin."""
    stopped = threading.Event()
    input_errors = []

    def emit(snapshot):
        payload = dict(snapshot, protocol_version=1)
        data = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        # Raw descriptors work for Windows anonymous pipes and avoid buffered
        # stream locks during interpreter shutdown with a blocked daemon reader.
        while data:
            written = write_pipe(sys.stdout.fileno(), data)
            if not written:
                raise OSError(errno.EIO, "Collector output made no progress")
            data = data[written:]

    def read_input(descriptor):
        try:
            while os.read(descriptor, 4096):
                pass
        except OSError as error:
            input_errors.append("Collector input: " + str(error))
        finally:
            stopped.set()

    try:
        try:
            descriptor = sys.stdin.fileno()
            scanner = Scanner()
        except (OSError, ValueError) as error:
            emit({"sessions": [], "errors": ["Collector startup: " + str(error)]})
            return 1
        threading.Thread(target=read_input, args=(descriptor,), daemon=True).start()
        while True:
            try:
                snapshot = scanner.snapshot()
            except (OSError, ValueError) as error:
                snapshot = {"sessions": [], "errors": ["Collector snapshot: " + str(error)]}
            emit(snapshot)
            if stopped.wait(2):
                if input_errors:
                    emit({"sessions": [], "errors": input_errors})
                    return 1
                return 0
    except OSError as error:
        if error.errno == errno.EPIPE or getattr(error, "winerror", None) in (109, 232):
            return 0
        print("Collector pipe failure: " + str(error), file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true",
                        help="Stream versioned NDJSON; close stdin to stop")
    args = parser.parse_args()
    if args.watch:
        return watch()
    print(json.dumps(Scanner().snapshot(), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
