"""Build and isolation utilities for native gadget regression tests only."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "windows-helper" / "gadget"
COLLECTORS = ("outer_progress", "activity", "session_probe")
BOOTSTRAP = """import json, sys, types
sources = json.loads(sys.stdin.readline())
for name in ('outer_progress', 'activity', 'session_probe'):
    module = types.ModuleType(name)
    module.__file__ = name + '.py'
    sys.modules[name] = module
    exec(compile(sources[name], module.__file__, 'exec'), module.__dict__)
sys.exit(sys.modules['session_probe'].watch())
"""


def temporary_directory():
    # Keep Windows File.Replace tests off the WSL source share.
    directory = None if os.name == "nt" else str(ROOT / "tests")
    return tempfile.TemporaryDirectory(prefix=".runtime-gadget-", dir=directory)


def isolated_environment(directory):
    root = Path(directory)
    home = root / "copilot"
    home.mkdir(exist_ok=True)
    local = root / "local"
    local.mkdir(exist_ok=True)
    settings = local / "CopilotAgentNotify"
    settings.mkdir(exist_ok=True)
    (settings / "gadget.json").write_text('{"wsl_distros":[]}', encoding="utf-8")
    return dict(os.environ, COPILOT_HOME=str(home), LOCALAPPDATA=str(local),
                XDG_CACHE_HOME=str(root / "cache"), TEMP=str(root), TMP=str(root),
                TMPDIR=str(root))


def compiler_command(output, extra_sources=(), main=None):
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    references = [framework / "WPF" / (name + ".dll") for name in (
        "PresentationFramework", "PresentationCore", "WindowsBase",
        "UIAutomationProvider", "UIAutomationTypes")]
    references += [framework / "System.Xaml.dll", framework / "System.Web.Extensions.dll"]
    command = [str(framework / "csc.exe"), "/nologo", "/optimize+",
               "/target:" + ("exe" if extra_sources else "winexe"), "/out:" + str(output),
               "/resource:" + str(SOURCE / "SessionWindow.xaml") + ",SessionWindow.xaml",
               "/resource:" + str(SOURCE / "sessions.png") + ",sessions.png",
               "/win32icon:" + str(SOURCE / "sessions.ico"),
               "/win32manifest:" + str(SOURCE / "app.manifest")]
    command += ["/reference:" + str(path) for path in references]
    command += [str(path) for path in sorted(SOURCE.glob("*.cs"))]
    command += [str(path) for path in extra_sources]
    if main:
        command.append("/main:" + main)
    return command


def build_window(directory, runner=None):
    output = Path(directory) / ((runner or "CopilotSessions") + ".exe")
    sources = [ROOT / "tests" / (runner + ".cs")] if runner else []
    result = subprocess.run(compiler_command(output, sources, runner),
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    shutil.copyfile(SOURCE / "app.config", str(output) + ".config")
    for name in COLLECTORS:
        shutil.copyfile(ROOT / "scripts" / (name + ".py"), Path(directory) / (name + ".py"))
    return output


def close_native_window(process):
    import ctypes
    from ctypes import wintypes
    import time

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    handles = []

    @callback_type
    def find_window(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process.pid and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    deadline = time.monotonic() + 10
    while not handles and process.poll() is None and time.monotonic() < deadline:
        user32.EnumWindows(find_window, 0)
        time.sleep(.02)
    if not handles or not user32.PostMessageW(handles[0], 0x10, 0, 0):
        raise AssertionError("Could not request native window closure")
