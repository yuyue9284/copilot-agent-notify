#!/usr/bin/env python3
"""Isolated JSON Zellij test double; never invokes a real terminal."""

import json
import os
from pathlib import Path
import sys


path = Path(os.environ["FAKE_ZELLIJ_STATE"])
state = json.loads(path.read_text(encoding="utf-8"))
args = sys.argv[1:]
assert args[:3] == ["--session", os.environ["ZELLIJ_SESSION_NAME"], "action"], args
action = args[3]
if action == "list-panes":
    print(json.dumps(state["panes"]))
elif action == "list-tabs":
    print(json.dumps(state["tabs"]))
elif action in ("rename-pane", "rename-tab-by-id"):
    if action == "rename-pane":
        assert args[4] == "--pane-id"
        item_id = int(args[5].removeprefix("terminal_"))
        next(p for p in state["panes"] if p["id"] == item_id)["title"] = args[6]
    else:
        item_id = int(args[4])
        next(t for t in state["tabs"] if t["tab_id"] == item_id)["name"] = args[5]
    scratch = path.with_suffix(".write")
    scratch.write_text(json.dumps(state), encoding="utf-8")
    os.replace(scratch, path)
    writer = os.getppid()
    if os.name == "nt":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from activity import process_table
        writer = process_table()[writer][0]  # The fake .cmd launcher's parent.
    with path.with_suffix(".actions").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"action": args[3:], "writer": writer}) + "\n")
else:
    raise AssertionError(args)
