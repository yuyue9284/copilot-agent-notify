#!/usr/bin/env python3
"""Windows desktop view of native and WSL Copilot session activity."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from session_probe import Scanner


INTERVAL = 2
STALE_SECONDS = 15
BOOTSTRAP = """import json, sys, types
sources = json.loads(sys.stdin.readline())
for name in ('outer_progress', 'activity', 'session_probe'):
    module = types.ModuleType(name)
    module.__file__ = name + '.py'
    sys.modules[name] = module
    exec(compile(sources[name], module.__file__, 'exec'), module.__dict__)
sys.modules['session_probe'].watch()
"""


def hidden_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def load_config(path=None):
    path = Path(path) if path is not None else (
        Path(os.environ["LOCALAPPDATA"]) / "CopilotAgentNotify" / "gadget.json")
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"wsl_distros": None}
    if not isinstance(config, dict) or set(config) - {"wsl_distros"}:
        raise ValueError("%s: expected an object with only wsl_distros" % path)
    distros = config.get("wsl_distros")
    if distros is not None and (
            not isinstance(distros, list) or any(
                not isinstance(name, str) or not name.strip() or name != name.strip()
                for name in distros)):
        raise ValueError("%s: wsl_distros must be null or a list of nonempty distribution names" % path)
    return {"wsl_distros": distros}


def selected_distros(allowed):
    if allowed == []:
        return set()
    running = set(running_distros())
    if allowed is None:
        return running
    names = {name.casefold() for name in allowed}
    return {name for name in running if name.casefold() in names}


def running_distros():
    binary = shutil.which("wsl.exe")
    if binary is None:
        return []
    result = subprocess.run([binary, "--list", "--running", "--quiet"],
                            capture_output=True, timeout=8, **hidden_options())
    if result.returncode:
        raise OSError("Cannot list running WSL distributions (exit %d)" % result.returncode)
    output = result.stdout
    text = output.decode("utf-16") if output.startswith(b"\xff\xfe") else (
        output.decode("utf-16-le") if b"\x00" in output else output.decode("utf-8"))
    return [line.strip() for line in text.splitlines() if line.strip()]


class WslWorker:
    def __init__(self, distro, updates):
        self.distro = distro
        self.updates = updates
        self.process = None
        self.stopping = threading.Event()
        self.bootstrap_sent = threading.Event()
        self.close_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        source = "WSL:" + self.distro
        error_thread = None
        try:
            if self.stopping.is_set():
                return
            directory = Path(__file__).resolve().parent
            sources = {name: (directory / (name + ".py")).read_text(encoding="utf-8")
                       for name in ("outer_progress", "activity", "session_probe")}
            # Run the exact same reducer in each distro; no plugin install, UNC
            # mount assumption, shell interpolation, or distribution wake-up.
            # Serialize creation/publication with close so a completed cancellation
            # cannot miss a child that is about to be launched.
            with self.close_lock:
                if self.stopping.is_set():
                    return
                self.process = subprocess.Popen(
                    ["wsl.exe", "--distribution", self.distro, "--exec", "python3", "-u",
                     "-c", BOOTSTRAP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, **hidden_options())
            if self.stopping.is_set():
                return
            diagnostics = []

            def read_errors():
                with self.process.stderr:
                    for line in self.process.stderr:
                        diagnostics.append(line.decode("utf-8", errors="replace").strip())
                        del diagnostics[:-8]

            error_thread = threading.Thread(target=read_errors, daemon=True)
            error_thread.start()
            self.process.stdin.write((json.dumps(sources) + "\n").encode("utf-8"))
            self.process.stdin.flush()
            self.bootstrap_sent.set()
            for line in self.process.stdout:
                if self.stopping.is_set():
                    break
                snapshot = json.loads(line)
                if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sessions"), list):
                    raise ValueError("invalid WSL session response")
                with self.close_lock:
                    if self.stopping.is_set():
                        break
                    self.updates.put((source, snapshot, time.monotonic()))
            if not self.stopping.is_set():
                error_thread.join(timeout=2)
                raise OSError("WSL collector stopped. " + " ".join(diagnostics))
        except (OSError, ValueError) as error:
            with self.close_lock:
                if not self.stopping.is_set():
                    self.updates.put((source, {"sessions": [], "errors": [str(error)]}, time.monotonic()))
        finally:
            self.close()
            if self.process is not None:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
                self.process.stdout.close()
            if error_thread is not None:
                error_thread.join(timeout=3)
            elif self.process is not None:
                self.process.stderr.close()

    def close(self):
        self.stopping.set()
        with self.close_lock:
            process = self.process
            if process is not None:
                if self.bootstrap_sent.is_set() and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                elif process.poll() is None:
                    # Buffered stdin.close() can wait forever for an in-flight
                    # bootstrap write. Terminate first; the writer owns cleanup.
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


class Monitor:
    def __init__(self, wsl_distros=None):
        self.wsl_distros = wsl_distros
        self.updates = queue.Queue()
        self.stop = threading.Event()
        self.workers = {}
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        scanner = Scanner()
        next_discovery = 0
        try:
            while not self.stop.is_set():
                try:
                    snapshot = scanner.snapshot()
                except (OSError, ValueError) as error:
                    snapshot = {"sessions": [], "errors": [str(error)]}
                self.updates.put(("Windows", snapshot, time.monotonic()))
                if time.monotonic() >= next_discovery:
                    try:
                        distros = selected_distros(self.wsl_distros)
                        self.updates.put(("WSL discovery", {"sessions": [], "errors": []},
                                          time.monotonic()))
                        for distro in set(self.workers) - distros:
                            self.workers.pop(distro).close()
                            self.updates.put(("WSL:" + distro, None, time.monotonic()))
                        for distro in distros:
                            worker = self.workers.get(distro)
                            if worker is None or not worker.thread.is_alive():
                                worker = WslWorker(distro, self.updates)
                                self.workers[distro] = worker
                                self.updates.put(("WSL:" + distro,
                                                  {"sessions": [], "errors": ["Connecting..."]},
                                                  time.monotonic()))
                                worker.start()
                    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                        self.updates.put(("WSL discovery", {"sessions": [], "errors": [str(error)]},
                                          time.monotonic()))
                    next_discovery = time.monotonic() + 10
                self.stop.wait(INTERVAL)
        finally:
            for worker in self.workers.values():
                worker.close()

    def close(self):
        self.stop.set()


def display_snapshot(sources, now):
    rows = []
    errors = []
    for source, (snapshot, stamp) in sources.items():
        stale = now - stamp > STALE_SECONDS
        if stale and source != "WSL discovery":
            errors.append(source + ": collector not responding")
        errors.extend(source + ": " + str(error) for error in snapshot["errors"])
        for session in snapshot["sessions"]:
            row = dict(session, source=source)
            if stale:
                row["status"] = "Unknown"
            rows.append(row)
    order = {"Needs input": 0, "In progress": 1, "Loading": 2, "Unknown": 3, "Done": 4}
    rows.sort(key=lambda row: (order[row["status"]], row["source"], row["title"], row["id"]))
    return rows, errors


def ui_sources():
    installed = Path(__file__).resolve().parent / "gadget-ui"
    return installed if installed.is_dir() else Path(__file__).resolve().parents[1] / "windows-helper" / "gadget"


def compiler_command(output, extra_sources=()):
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    source = ui_sources()
    references = [framework / "WPF" / (name + ".dll")
                  for name in ("PresentationFramework", "PresentationCore", "WindowsBase")]
    references += [framework / "System.Xaml.dll", framework / "System.Web.Extensions.dll"]
    return [str(framework / "csc.exe"), "/nologo", "/optimize+",
            "/target:" + ("exe" if extra_sources else "winexe"), "/out:" + str(output),
            "/resource:" + str(source / "SessionWindow.xaml") + ",SessionWindow.xaml",
            "/resource:" + str(source / "sessions.png") + ",sessions.png",
            "/win32icon:" + str(source / "sessions.ico"),
            "/win32manifest:" + str(source / "app.manifest"),
            *["/reference:" + str(path) for path in references],
            str(source / "SessionWindow.cs"), *[str(path) for path in extra_sources]]


def build_window():
    source = ui_sources()
    digest = hashlib.sha256()
    for name in ("SessionWindow.cs", "SessionWindow.xaml", "app.manifest", "app.config",
                 "sessions.ico", "sessions.png"):
        digest.update((source / name).read_bytes())
    directory = Path(os.environ["LOCALAPPDATA"]) / "CopilotAgentNotify" / "gadget-build" / digest.hexdigest()[:16]
    executable = directory / "CopilotSessions.exe"
    if not executable.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        # Compile to a unique name so two simultaneous launches cannot use a
        # half-written executable. The hash keeps running versions untouched.
        import uuid
        scratch = directory / (uuid.uuid4().hex + ".exe")
        try:
            result = subprocess.run(compiler_command(scratch), capture_output=True,
                                    text=True, timeout=60, **hidden_options())
            if result.returncode:
                raise OSError("Cannot build the desktop window:\n" + result.stdout + result.stderr)
            shutil.copyfile(source / "app.config", str(executable) + ".config")
            try:
                os.replace(scratch, executable)
            except PermissionError:
                if not executable.is_file():
                    raise
        finally:
            scratch.unlink(missing_ok=True)
    return executable


def merge_update(sources, source, snapshot, stamp):
    if snapshot is None:
        sources.pop(source, None)
    else:
        # Keep last-known rows on failure, never their last-known success state.
        if snapshot["errors"] and not snapshot["sessions"] and source in sources:
            old, _ = sources[source]
            snapshot = dict(snapshot, sessions=[dict(row, status="Unknown") for row in old["sessions"]])
        sources[source] = (snapshot, stamp)


def run_window():
    config = load_config()
    executable = build_window()
    monitor = Monitor(wsl_distros=config["wsl_distros"])
    sources = {}
    window = subprocess.Popen([str(executable)], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_options())
    monitor.thread.start()
    try:
        while window.poll() is None:
            try:
                update = monitor.updates.get(timeout=INTERVAL)
                merge_update(sources, *update)
                while True:
                    merge_update(sources, *monitor.updates.get_nowait())
            except queue.Empty:
                pass
            rows, errors = display_snapshot(sources, time.monotonic())
            payload = {"rows": rows, "errors": errors, "discovering": not sources}
            try:
                window.stdin.write((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
                window.stdin.flush()
            except BrokenPipeError:
                break
    finally:
        monitor.close()
        try:
            window.stdin.close()
        except BrokenPipeError:
            pass
        monitor.thread.join(timeout=20)
        try:
            window.wait(timeout=5)
        except subprocess.TimeoutExpired:
            window.terminate()
            window.wait(timeout=5)
    if window.returncode:
        raise OSError("Desktop window exited with code %s" % window.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true", help="Print native session discovery as JSON")
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(Scanner().snapshot(), indent=2))
        return
    if os.name != "nt":
        parser.error("Launch the gadget with Windows Python; it discovers WSL automatically.")
    try:
        run_window()
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(error), "Copilot Sessions", 0x10)
        raise


if __name__ == "__main__":
    main()
