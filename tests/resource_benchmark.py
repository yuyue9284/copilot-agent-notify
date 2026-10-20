"""Opt-in synthetic collector benchmarks; JSON output contains no session metadata."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def memory():
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak", "working", "quota_peak_paged", "quota_paged",
                    "quota_peak_nonpaged", "quota_nonpaged", "pagefile", "peak_pagefile")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        api = ctypes.WinDLL("psapi", use_last_error=True)
        api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        value = Counters()
        value.cb = ctypes.sizeof(value)
        if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.working, value.peak
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return None, peak
    status = Path("/proc/self/status").read_text()
    rss = int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmRSS:")))
    return rss * 1024, max(rss, peak) * 1024


def children_cpu():
    if os.name == "nt":
        return None
    import resource
    value = resource.getrusage(resource.RUSAGE_CHILDREN)
    return value.ru_utime + value.ru_stime


def measure(action, iterations):
    times = []
    cpu = time.process_time()
    children = children_cpu()
    for _ in range(iterations):
        start = time.perf_counter()
        action()
        times.append((time.perf_counter() - start) * 1000)
    return dict(
        iterations=iterations, median_ms=round(statistics.median(times), 3),
        p95_ms=round(sorted(times)[max(0, (95 * len(times) + 99) // 100 - 1)], 3),
        own_cpu_ms=round((time.process_time() - cpu) * 1000 / iterations, 3),
        child_cpu_ms=None if children is None else round(
            (children_cpu() - children) * 1000 / iterations, 3))


def worker(args):
    sys.path.insert(0, str(args.source_dir))
    from activity import Transcript
    from session_probe import ActivityRevision, Scanner
    baseline, _ = memory()
    if args.case == "scan":
        scanner = Scanner(args.home)
        try:
            def snapshot():
                value = scanner.snapshot()
                if value["errors"] or len(value["sessions"]) != args.sessions:
                    raise AssertionError(value)
            snapshot()
            result = measure(snapshot, args.iterations)
        finally:
            if hasattr(scanner, "close"):
                scanner.close()
    elif args.case in ("record", "history"):
        reader = Transcript(args.home / "events.jsonl", "synthetic-root")
        reader.state = ActivityRevision("synthetic-root")
        def catch_up():
            while not reader.poll():
                pass
        result = measure(catch_up, 1)
        if reader.malformed or reader.offset != (args.home / "events.jsonl").stat().st_size:
            raise AssertionError("benchmark did not consume a valid record")
    else:
        from activity import Zellij
        api = Zellij(args.zellij, args.zellij_session)
        deadline = time.monotonic() + 15
        while True:
            try:
                api.snapshot()
                break
            except (RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)
        result = measure(api.snapshot, args.iterations)
    rss, peak = memory()
    result.update(case=args.case, platform=sys.platform,
                  rss_mib=None if rss is None else round(rss / 1024 ** 2, 3),
                  peak_mib=round(peak / 1024 ** 2, 3),
                  baseline_rss_mib=None if baseline is None else round(baseline / 1024 ** 2, 3),
                  peak_delta_mib=None if baseline is None else round((peak - baseline) / 1024 ** 2, 3))
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "scripts")
    parser.add_argument("--case", choices=("scan", "record", "history", "zellij"), default="scan")
    parser.add_argument("--sessions", type=int, default=10)
    parser.add_argument("--archives", type=int, default=1000)
    parser.add_argument("--record-mib", type=int, default=16)
    parser.add_argument("--meaningful", action="store_true")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--zellij", default="zellij")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--zellij-session", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    with tempfile.TemporaryDirectory(prefix="notify-resource-") as temporary:
        home = Path(temporary)
        environment = dict(os.environ, COPILOT_HOME=str(home),
                           COPILOT_NOTIFY_STATE_DIR=str(home / "cache"),
                           PYTHONDONTWRITEBYTECODE="1", COPILOT_NOTIFY_STATUS_BACKEND="legacy")
        environment.pop("ZELLIJ", None)
        environment.pop("ZELLIJ_SESSION_NAME", None)
        command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:],
                   "--worker", "--home", str(home)]
        if args.case == "scan":
            for index in range(args.sessions + args.archives):
                sid = "synthetic-%04d" % index
                directory = home / "session-state" / sid
                directory.mkdir(parents=True)
                if index < args.sessions:
                    (directory / ("inuse.%d.lock" % os.getpid())).touch()
                    (directory / "workspace.yaml").write_text("name: Synthetic benchmark\n")
                    (directory / "events.jsonl").write_text(
                        json.dumps({"type": "session.start", "data": {"sessionId": sid}}) + "\n")
        elif args.case in ("record", "history"):
            with (home / "events.jsonl").open("wb") as stream:
                stream.write(b'{"type":"session.start","data":{"sessionId":"synthetic-root"}}\n')
                if args.case == "history":
                    turn = [
                        {"type": "user.message", "data": {"delivery": "idle", "content": "x" * 1024}},
                        {"type": "assistant.turn_start", "data": {}},
                        {"type": "tool.execution_start", "data": {"toolName": "test", "toolCallId": "call"}},
                        {"type": "tool.execution_complete", "data": {"toolCallId": "call"}},
                        {"type": "assistant.message", "data": {"turnId": "turn", "toolRequests": []}},
                        {"type": "assistant.turn_end", "data": {"turnId": "turn"}},
                    ]
                    block = b"".join(json.dumps(value).encode() + b"\n" for value in turn)
                    for _ in range(5000):
                        stream.write(block)
                else:
                    kind = b"user.message" if args.meaningful else b"tool.execution_complete"
                    stream.write(b'{"type":"' + kind + b'","data":{"content":"')
                    for _ in range(args.record_mib * 16):
                        stream.write(b"x" * 65536)
                    stream.write(b'"}}\n')
        else:
            session = "notify-benchmark-" + uuid.uuid4().hex[:12]
            console = None
            if os.name == "nt":
                # Native Zellij needs a console even for RPC-only test sessions.
                console = subprocess.Popen(
                    [args.zellij, "--session", session], env=environment, cwd=home,
                    creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                subprocess.run([args.zellij, "attach", "--create-background", session],
                               env=environment, check=True, capture_output=True, timeout=30)
            command += ["--zellij-session", session]
        try:
            subprocess.run(command, env=environment, check=True, timeout=300)
        finally:
            if args.case == "zellij":
                try:
                    subprocess.run([args.zellij, "delete-session", "--force", session],
                                   env=environment, check=True, capture_output=True, timeout=30)
                finally:
                    if console is not None:
                        try:
                            console.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            console.terminate()
                            console.wait(timeout=10)


if __name__ == "__main__":
    main()
