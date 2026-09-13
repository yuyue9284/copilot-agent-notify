#!/usr/bin/env python3
"""Compatibility launcher for the prebuilt native Copilot Sessions application."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from session_probe import Scanner


def application_path():
    directory = Path(__file__).resolve().parent
    installed = directory / "CopilotSessions.exe"
    if installed.is_file():
        return installed
    return directory.parent / "windows-helper" / "gadget" / "bin" / "Release" / "CopilotSessions.exe"


def run_window():
    executable = application_path()
    if not executable.is_file():
        raise FileNotFoundError(
            "Copilot Sessions has not been built. Run windows-helper\\build-gadget.ps1 "
            "from Windows PowerShell, then retry or run windows-helper\\install-gadget.ps1.")
    result = subprocess.run([str(executable), "--python", sys.executable])
    if result.returncode:
        raise OSError("Copilot Sessions exited with code %s" % result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true", help="Print native session discovery as JSON")
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(Scanner().snapshot(), indent=2))
        return 0
    if os.name != "nt":
        parser.error("Launch the prebuilt gadget on Windows; Windows Python 3.9+ remains required.")
    try:
        run_window()
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
