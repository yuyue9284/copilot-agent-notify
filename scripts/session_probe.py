#!/usr/bin/env python3
"""Read-only discovery of this OS user's live Copilot root sessions."""

import json
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
import select
import sys
import time

from activity import Activity, Transcript, process_token


class ActivityRevision(Activity):
    """Stable activity identity across polling and transcript replay."""

    def reset(self):
        super().reset()
        self.activity_revision = ""

    def event(self, event):
        previous = self.activity_revision
        super().event(event)
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
        self.readers = {}

    def snapshot(self):
        sessions = []
        errors = []
        owners = {}
        tokens = {}
        state = self.home / "session-state"
        # A CLI can own several root sessions concurrently. Locks establish
        # liveness, not session identity; only deduplicate owners of the same root.
        for lock in state.glob("*/inuse.*.lock"):
            try:
                pid = int(lock.name.split(".")[1])
                if pid not in tokens:
                    token = process_token(pid)
                    tokens[pid] = (token, started_at(token)) if token else (None, 0)
                token, born = tokens[pid]
                stamp = lock.stat().st_mtime
                if not token or stamp + 2 < born:
                    continue
                if not (lock.parent / "events.jsonl").is_file():
                    continue
                owners.setdefault(lock.parent, []).append((pid, token))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                errors.append("%s: %s" % (lock.parent.name, error))
        keep = set()
        for directory, candidates in sorted(owners.items()):
            session_id = directory.name
            owner = min(candidates)
            key = (session_id, owner)
            keep.add(key)
            row = {"id": session_id, "pid": owner[0], "title": session_id[:8],
                   "cwd": "", "status": "Loading", "activity_revision": None}
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
                    busy, attention = transcript.display_counts
                    row["status"] = "Needs input" if attention else "In progress" if busy else "Done"
                    row["activity_revision"] = transcript.state.activity_revision
            except (OSError, ValueError, TypeError, AttributeError) as error:
                row["status"] = "Unknown"
                errors.append("%s: %s" % (session_id, error))
                self.readers.pop(key, None)
            sessions.append(row)
        self.readers = {key: reader for key, reader in self.readers.items() if key in keep}
        return {"sessions": sessions, "errors": errors}


def watch():
    """WSL worker: closing its input pipe ends it without a resident service."""
    scanner = Scanner()
    while True:
        print(json.dumps(scanner.snapshot(), ensure_ascii=True), flush=True)
        if select.select([sys.stdin], [], [], 2)[0]:
            if not sys.stdin.readline():
                return


if __name__ == "__main__":
    print(json.dumps(Scanner().snapshot(), ensure_ascii=True))
