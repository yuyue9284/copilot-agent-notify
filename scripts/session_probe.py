#!/usr/bin/env python3
"""Read-only discovery of this OS user's live Copilot root sessions."""

import json
import os
from pathlib import Path
import select
import sys
import time

from activity import Transcript, process_token


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
        # Choose the newest root lock per process: resume can leave the original
        # session directory locked, with or without its own transcript.
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
                key = (pid, token)
                if key not in owners or stamp > owners[key][0]:
                    owners[key] = (stamp, lock.parent)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                errors.append("%s: %s" % (lock.parent.name, error))
        keep = set()
        seen = set()
        for owner, (_, directory) in sorted(owners.items()):
            session_id = directory.name
            if session_id in seen:
                continue
            seen.add(session_id)
            key = (session_id, owner)
            keep.add(key)
            row = {"id": session_id, "pid": owner[0], "title": session_id[:8],
                   "cwd": "", "status": "Loading"}
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
                    reader = (Transcript(path, session_id), str(context.get("cwd") or ""))
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
