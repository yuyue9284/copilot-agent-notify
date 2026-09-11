"""Native console lifetime regression: never uses an existing user console."""

import os
import subprocess
import sys
import unittest

from test_activity import Files, ROOT


@unittest.skipUnless(os.name == "nt", "Native Windows console lifetime")
class ConsoleLifetimeTests(Files):
    def test_progress_write_does_not_break_next_subprocess(self):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        target = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        try:
            outcome = self.directory / "outcome.txt"
            code = (
                "import subprocess,sys,traceback\n"
                "from pathlib import Path\n"
                "sys.path.insert(0,sys.argv[1])\n"
                "from outer_progress import write_windows,progress_sequence\n"
                "try:\n"
                "    write_windows(int(sys.argv[2]),progress_sequence(3))\n"
                "    subprocess.run(['cmd.exe','/c','exit','0'],capture_output=True,check=True)\n"
                "    write_windows(int(sys.argv[2]),progress_sequence(0,completed=True))\n"
                "    subprocess.run(['cmd.exe','/c','exit','0'],capture_output=True,check=True)\n"
                "except Exception:\n"
                "    Path(sys.argv[3]).write_text(traceback.format_exc())\n"
                "else:\n"
                "    Path(sys.argv[3]).write_text('ok')\n"
            )
            subprocess.run(
                [sys.executable, "-c", code, str(ROOT / "scripts"), str(target.pid), str(outcome)],
                timeout=20, check=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
            self.assertEqual(outcome.read_text(), "ok")
        finally:
            target.terminate()
            target.wait(timeout=10)
