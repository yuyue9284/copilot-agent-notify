using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Threading;
using CopilotSessions;

public static class GadgetHostTests
{
    private const string Valid = "{\"protocol_version\":1,\"sessions\":[{\"id\":\"synthetic\",\"pid\":42,\"title\":\"Synthetic session\",\"cwd\":\"C:/synthetic\",\"status\":\"Done\",\"activity_revision\":\"exact:001\"}],\"errors\":[]}";
    private const string StartupError = "{\"protocol_version\":1,\"sessions\":[],\"errors\":[\"specific startup cause\"]}";
    private static int passed;
    private static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static void Eventually(Func<bool> condition, string message)
    {
        var clock = Stopwatch.StartNew();
        while (!condition() && clock.ElapsedMilliseconds < 5000) Thread.Sleep(10);
        Check(condition(), message);
    }
    private static void Reject(Action action, string message)
    {
        try { action(); }
        catch (ArgumentException) { return; }
        catch (InvalidOperationException) { return; }
        throw new Exception("Accepted invalid input: " + message);
    }
    private static void Test(string name, Action action)
    {
        action(); passed++; Console.WriteLine("PASS " + name);
    }
    private static CollectorSnapshot Sample() { return SnapshotProtocol.ParseCollector(Valid); }

    private static void Configuration()
    {
        string[] running = { "Ubuntu", "Debian", "ubuntu" };
        Check(GadgetConfiguration.Parse("{}").SelectRunning(running).SequenceEqual(new[] { "Ubuntu", "Debian" }), "Default all-running selection");
        Check(GadgetConfiguration.Parse("{\"wsl_distros\":null}").SelectRunning(running).Length == 2, "null allowlist");
        Check(GadgetConfiguration.Parse("{\"wsl_distros\":[]}").WslDisabled, "empty list disables discovery");
        Check(GadgetConfiguration.Parse("{\"wsl_distros\":[\"uBuNtU\",\"Stopped\"]}").SelectRunning(running).SequenceEqual(new[] { "Ubuntu" }), "case-insensitive running-only allowlist");
        foreach (string invalid in new[] { "null", "[]", "{\"extra\":true}", "{\"wsl_distros\":\"Ubuntu\"}", "{\"wsl_distros\":[1]}",
            "{\"wsl_distros\":[null]}", "{\"wsl_distros\":[\"\"]}", "{\"wsl_distros\":[\" Ubuntu\"]}", "{\"wsl_distros\":[\"\\t\"]}" })
            Reject(() => GadgetConfiguration.Parse(invalid), invalid);
        string missing = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "missing-" + Guid.NewGuid() + ".json");
        Check(GadgetConfiguration.Load(missing).SelectRunning(running).Length == 2, "Missing config defaults");
        var original = new[] { "Ubuntu" };
        var config = new GadgetConfiguration(original);
        original[0] = "Debian";
        Check(config.SelectRunning(running).Single() == "Ubuntu", "config clone");
    }

    private static void Protocol()
    {
        var parsed = Sample();
        Check(parsed.Sessions[0].State == SessionStatus.Done, "enum status");
        Check(parsed.Sessions[0].activity_revision == "exact:001", "revision changed");
        foreach (string invalid in new[] { "null", "[]", "{", "{}",
            Valid.Replace("\"protocol_version\":1", "\"protocol_version\":\"1\""),
            Valid.Replace("\"protocol_version\":1", "\"protocol_version\":true"),
            Valid.Replace("\"protocol_version\":1", "\"protocol_version\":1.0"),
            Valid.Replace("\"protocol_version\":1", "\"protocol_version\":2"),
            Valid.Replace("\"Done\"", "\"done\""), Valid.Replace("\"pid\":42", "\"pid\":\"42\""),
            Valid.Replace("\"pid\":42", "\"pid\":0"), Valid.Replace("\"title\":\"Synthetic session\"", "\"title\":null"),
            Valid.Replace("\"activity_revision\":\"exact:001\"", "\"activity_revision\":2"),
            Valid.Replace(",\"activity_revision\":\"exact:001\"", ""),
            Valid.Replace("\"errors\":[]", "\"errors\":[{}]"),
            Valid.Replace("\"errors\":[]", "\"errors\":null"),
            "{\"protocol_version\":1,\"sessions\":{},\"errors\":[]}",
            "{\"protocol_version\":1,\"sessions\":[null],\"errors\":[]}" })
            Reject(() => SnapshotProtocol.ParseCollector(invalid), invalid);
        string row = Valid.Substring(Valid.IndexOf("[{") + 1, Valid.IndexOf("}],") - Valid.IndexOf("[{"));
        Reject(() => SnapshotProtocol.ParseCollector("{\"protocol_version\":1,\"sessions\":[" + row + "," + row + "],\"errors\":[]}"), "duplicate identity");
        Check(SnapshotProtocol.ParseCollector(Valid.Replace("\"exact:001\"", "null")).Sessions[0].activity_revision == null, "nullable revision");
        Reject(() => new SessionData { status = "unchecked" }, "unchecked string status");
        Reject(() => new SessionData { State = (SessionStatus)100 }, "unchecked enum");
        SnapshotProtocol.ParseUi("{\"rows\":[],\"errors\":[],\"discovering\":false}");
        Reject(() => SnapshotProtocol.ParseUi("{\"rows\":[],\"errors\":[],\"discovering\":\"false\"}"), "UI boolean");
        Reject(() => SnapshotProtocol.ParseUi(Valid), "collector envelope in UI mode");
        parsed.Sessions[0].status = "Unknown";
        Check(parsed.Sessions[0].status == "Done", "collector snapshot must be immutable");
    }

    private static void Aggregation()
    {
        var state = new SourceAggregator();
        Check(state.Display(0).discovering, "initial discovery");
        state.Connecting("Windows", 0);
        Check(state.Display(0).discovering, "connecting discovery");
        state.Update("Windows", Sample(), 10);
        state.Update("WSL:Ubuntu", Sample(), 10);
        state.Update("WSL:Ubuntu", CollectorSnapshot.Failure("synthetic failure"), 12);
        var failed = state.Display(12);
        Check(failed.rows.Single(row => row.source == "WSL:Ubuntu").status == "Unknown", "failure retained false success");
        Check(failed.rows.Single(row => row.source == "Windows").status == "Done", "failure hid healthy Windows");
        Check(failed.errors.Single().Contains("synthetic failure"), "failure not reported");
        failed.rows[0].status = "In progress";
        Check(state.Display(12).rows.Single(row => row.source == "WSL:Ubuntu").status == "Unknown", "UI mutation leaked");
        state.Update("WSL:Ubuntu", Sample(), 20);
        Check(state.Display(20).rows.All(row => row.status == "Done"), "recovery");
        state.Update("WSL:ubuntu", Sample(), 20);
        Check(state.Display(20).rows.Length == 2, "case-only distribution spelling duplicated source");
        state.Update("WSL discovery", new CollectorSnapshot(new SessionData[0], new string[0]), 0);
        var stale = state.Display(36);
        Check(stale.rows.All(row => row.status == "Unknown"), "stale successes must be unknown");
        Check(stale.errors.Length == 2, "only collector freshness errors");
        state.Update("WSL:Ubuntu", null, 37);
        Check(state.Display(37).rows.Length == 1, "removed source still visible");
        state.Update("Windows", new CollectorSnapshot(new SessionData[0], new string[0]), 38);
        Check(state.Display(38).rows.Length == 0, "healthy empty snapshot must clear rows");
    }

    private sealed class BlockingReader : TextReader
    {
        private readonly WaitHandle exited;
        public BlockingReader(WaitHandle value) { exited = value; }
        public override int Read() { exited.WaitOne(); return -1; }
        public override int Read(char[] buffer, int index, int count) { exited.WaitOne(); return 0; }
    }
    private sealed class FakeChild : ICollectorChild
    {
        public readonly ManualResetEvent Exited = new ManualResetEvent(false);
        public readonly ManualResetEvent Writing = new ManualResetEvent(false);
        public bool BlockWrite;
        public bool CloseFails;
        public bool ExitBeforeCloseFailure;
        public int Kills;
        public int Closes;
        public readonly StringBuilder Written = new StringBuilder();
        private readonly FakeInput input;
        public FakeChild() { input = new FakeInput(this); }
        public TextReader Output { get { return new BlockingReader(Exited); } }
        public TextReader Error { get { return new BlockingReader(Exited); } }
        public TextWriter Input { get { return input; } }
        public bool HasExited { get { return Exited.WaitOne(0); } }
        public int ExitCode { get { return 0; } }
        public bool WaitForExit(int milliseconds) { return Exited.WaitOne(milliseconds); }
        public void Kill() { Interlocked.Increment(ref Kills); Exited.Set(); }
        public void Dispose() { }
        private sealed class FakeInput : TextWriter
        {
            private readonly FakeChild owner;
            public FakeInput(FakeChild process) { owner = process; }
            public override Encoding Encoding { get { return Encoding.UTF8; } }
            public override void Write(string value)
            {
                owner.Writing.Set();
                if (owner.BlockWrite) { owner.Exited.WaitOne(); throw new IOException("Reader terminated"); }
                lock (owner.Written) owner.Written.Append(value);
            }
            public override void Close()
            {
                Interlocked.Increment(ref owner.Closes);
                if (owner.CloseFails)
                {
                    if (owner.ExitBeforeCloseFailure) owner.Exited.Set();
                    throw new IOException("synthetic genuine close error");
                }
                owner.Exited.Set();
            }
        }
    }
    private sealed class FakeLauncher : ICollectorLauncher
    {
        public readonly FakeChild Child = new FakeChild();
        public readonly ManualResetEvent Entered = new ManualResetEvent(false);
        public readonly ManualResetEvent Release = new ManualResetEvent(true);
        public int Calls;
        public ProcessStartInfo Command;
        public ICollectorChild Start(ProcessStartInfo info) { Command = info; Interlocked.Increment(ref Calls); Entered.Set(); Release.WaitOne(); return Child; }
    }
    private static CollectorProcess FakeWorker(FakeLauncher launcher, Func<string> payload)
    {
        return new CollectorProcess(() => NativeGadgetEnvironment.Command("synthetic.exe", ""), payload, launcher,
            snapshot => { lock (events) events.Add(snapshot); });
    }
    private static readonly List<CollectorSnapshot> events = new List<CollectorSnapshot>();
    private static void Cancellation()
    {
        var before = new FakeLauncher();
        var cancelled = FakeWorker(before, null);
        cancelled.RequestStop(); cancelled.Start();
        Check(cancelled.Wait(3000) && before.Calls == 0, "pre-cancel launched child");
        var readEntered = new ManualResetEvent(false);
        var readRelease = new ManualResetEvent(false);
        var reading = new FakeLauncher();
        var readingWorker = FakeWorker(reading, delegate { readEntered.Set(); readRelease.WaitOne(); return "{}"; });
        readingWorker.Start();
        Check(readEntered.WaitOne(3000), "bootstrap read did not start");
        readingWorker.RequestStop(); readRelease.Set();
        Check(readingWorker.Wait(3000) && reading.Calls == 0, "cancel during source read launched child");
        var during = new FakeLauncher();
        during.Release.Reset();
        var launching = FakeWorker(during, null);
        launching.Start();
        Check(during.Entered.WaitOne(3000), "launch did not begin");
        var elapsed = Stopwatch.StartNew();
        launching.RequestStop();
        Check(elapsed.ElapsedMilliseconds < 500, "RequestStop blocked on process creation");
        during.Release.Set();
        Check(launching.Wait(3000) && during.Child.Kills == 1, "in-flight launch orphaned child");
        var blocked = new FakeLauncher();
        blocked.Child.BlockWrite = true;
        var blockedWorker = FakeWorker(blocked, () => "{}");
        blockedWorker.Start();
        Check(blocked.Child.Writing.WaitOne(3000), "write did not start");
        blockedWorker.RequestStop();
        Check(blockedWorker.Wait(3000) && blocked.Child.Kills == 1 && blocked.Child.Closes == 0, "blocked writer cleanup must kill before closing");
    }
    private static void GracefulAndErrors()
    {
        var graceful = new FakeLauncher();
        var worker = FakeWorker(graceful, () => "{}");
        worker.Start();
        Check(graceful.Child.Writing.WaitOne(3000), "worker not ready");
        Thread.Sleep(50);
        worker.RequestStop();
        Check(worker.Wait(3000) && graceful.Child.Closes == 1 && graceful.Child.Kills == 0, "ready collector did not receive graceful EOF");
        foreach (bool exited in new[] { false, true })
        {
            var launcher = new FakeLauncher();
            launcher.Child.CloseFails = true;
            launcher.Child.ExitBeforeCloseFailure = exited;
            var failure = FakeWorker(launcher, () => "{}");
            failure.Start(); Check(launcher.Child.Writing.WaitOne(3000), "failure worker not ready"); Thread.Sleep(50);
            failure.RequestStop();
            Check(failure.Wait(4000), "cleanup failed to complete");
            Check((failure.ShutdownError == null) == exited, "genuine close error hidden or peer-exit error reported");
            Check(launcher.Child.HasExited, "failed close orphaned child");
        }
    }

    private sealed class TrackingLauncher : ICollectorLauncher
    {
        private readonly SystemCollectorLauncher system = new SystemCollectorLauncher();
        public int Kills;
        public readonly ManualResetEvent Writing = new ManualResetEvent(false);
        public ICollectorChild Start(ProcessStartInfo info) { return new Tracked(system.Start(info), this); }
        private sealed class Tracked : ICollectorChild
        {
            private readonly ICollectorChild child;
            private readonly TrackingLauncher owner;
            public Tracked(ICollectorChild value, TrackingLauncher tracker) { child = value; owner = tracker; }
            public TextReader Output { get { return child.Output; } }
            public TextReader Error { get { return child.Error; } }
            public TextWriter Input { get { return new Writer(child.Input, owner.Writing); } }
            public bool HasExited { get { return child.HasExited; } }
            public int ExitCode { get { return child.ExitCode; } }
            public bool WaitForExit(int milliseconds) { return child.WaitForExit(milliseconds); }
            public void Kill() { Interlocked.Increment(ref owner.Kills); child.Kill(); }
            public void Dispose() { child.Dispose(); }
        }
        private sealed class Writer : TextWriter
        {
            private readonly TextWriter writer;
            private readonly ManualResetEvent writing;
            public Writer(TextWriter value, ManualResetEvent signal) { writer = value; writing = signal; }
            public override Encoding Encoding { get { return writer.Encoding; } }
            public override void Write(string value) { writing.Set(); writer.Write(value); }
            public override void Flush() { writer.Flush(); }
            public override void Close() { writer.Close(); }
        }
    }
    private static ProcessStartInfo Self(string argument)
    {
        return NativeGadgetEnvironment.Command(Assembly.GetExecutingAssembly().Location, argument);
    }
    private static void LaunchContracts()
    {
        Check(NativeGadgetEnvironment.IsAbsolutePath(@"C:\synthetic\python.exe"), "absolute interpreter rejected");
        Check(NativeGadgetEnvironment.IsAbsolutePath(@"\\synthetic\share\python.exe"), "UNC interpreter rejected");
        foreach (string path in new[] { "python.exe", @"C:python.exe", @"\python.exe", "", "\0" })
            Check(!NativeGadgetEnvironment.IsAbsolutePath(path), "nonabsolute interpreter accepted: " + path);
        string directory = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "launch-" + Guid.NewGuid());
        string originalPath = Environment.GetEnvironmentVariable("PATH");
        Directory.CreateDirectory(directory);
        try
        {
            foreach (string name in new[] { "outer_progress.py", "activity.py", "session_probe.py" })
                File.WriteAllText(Path.Combine(directory, name), "# synthetic \u03bb\n");
            foreach (string name in new[] { "python.exe", "py.exe", "wsl.exe" })
                File.WriteAllText(Path.Combine(directory, name), "synthetic nonexecuted launcher fixture");
            Environment.SetEnvironmentVariable("PATH", directory);
            foreach (bool selected in new[] { true, false })
            {
                var launcher = new FakeLauncher();
                var system = new NativeGadgetEnvironment(directory, selected ? Path.Combine(directory, "python.exe") : null, launcher);
                var worker = system.Windows(snapshot => { });
                worker.Start(); Check(launcher.Entered.WaitOne(3000), "native collector command not created");
                Check(launcher.Command.Arguments.Contains("--watch") && launcher.Command.Arguments.Contains("session_probe.py"), "native watch ABI");
                Check(launcher.Command.Arguments.StartsWith(selected ? "-u " : "-3 -u "), "selected/default interpreter ABI");
                Check(!launcher.Command.Arguments.Contains("session_gadget"), "Python parent relaunched");
                Check(launcher.Command.CreateNoWindow && !launcher.Command.UseShellExecute, "visible shell launched");
                worker.RequestStop(); Check(worker.Wait(3000), "native fixture did not stop");
            }
            var wslLauncher = new FakeLauncher();
            var wslEnvironment = new NativeGadgetEnvironment(directory, null, wslLauncher);
            var wsl = wslEnvironment.Wsl("Synthetic Distro", snapshot => { });
            wsl.Start(); Check(wslLauncher.Child.Writing.WaitOne(3000), "WSL bootstrap not sent");
            Eventually(delegate { lock (wslLauncher.Child.Written) return wslLauncher.Child.Written.ToString().Contains("\\u03bb"); }, "non-ASCII source bootstrap not preserved");
            Check(wslLauncher.Command.Arguments.StartsWith("--distribution \"Synthetic Distro\" --exec python3 -u -c "), "WSL launch ABI");
            Check(wslLauncher.Command.Arguments.Contains("session_probe"), "WSL watch bootstrap missing");
            wsl.RequestStop(); Check(wsl.Wait(3000), "WSL fixture did not stop");
            var ordinaryLauncher = new FakeLauncher();
            var ordinary = new NativeGadgetEnvironment(directory, null, ordinaryLauncher).Wsl("Ubuntu", snapshot => { });
            ordinary.Start();
            Check(ordinaryLauncher.Child.Writing.WaitOne(3000), "ordinary WSL bootstrap not sent");
            Check(ordinaryLauncher.Command.Arguments.StartsWith("--distribution Ubuntu --exec python3 -u -c "),
                "unnecessary distro quotes cause WSL_E_DISTRO_NOT_FOUND in the real WSL option parser");
            ordinary.RequestStop(); Check(ordinary.Wait(3000), "ordinary WSL fixture did not stop");
            Environment.SetEnvironmentVariable("PATH", "");
            Check(wslEnvironment.RunningDistros(new ManualResetEvent(false)).Length == 0, "missing WSL should be empty");
            var failure = new List<CollectorSnapshot>();
            var missing = new NativeGadgetEnvironment(directory, null, new FakeLauncher()).Windows(snapshot => failure.Add(snapshot));
            missing.Start(); Check(missing.Wait(3000), "missing Python worker did not finish");
            Check(failure.Last().Errors.Single().Contains("--python"), "missing Python error not actionable");
        }
        finally
        {
            Environment.SetEnvironmentVariable("PATH", originalPath);
            Directory.Delete(directory, true);
        }
    }
    private static void RealPipes()
    {
        var received = new List<CollectorSnapshot>();
        var launcher = new TrackingLauncher();
        var graceful = new CollectorProcess(() => Self("--child-recover"), null, launcher,
            snapshot => { lock (received) received.Add(snapshot); });
        graceful.Start();
        Eventually(delegate { lock (received) return received.Count >= 2; }, "malformed and valid output not received");
        lock (received)
        {
            Check(received[0].Errors.Length == 1, "malformed response not reported");
            Check(received.Last().Sessions.Length == 1, "malformed response prevented recovery");
        }
        var siblingReady = new ManualResetEvent(false);
        var siblingLauncher = new TrackingLauncher();
        var sibling = new CollectorProcess(() => Self("--child-recover"), null, siblingLauncher,
            snapshot => { if (snapshot.Sessions.Length == 1) siblingReady.Set(); });
        sibling.Start();
        try
        {
            Check(siblingReady.WaitOne(3000), "parallel collector did not start");
            graceful.RequestStop();
            Check(graceful.Wait(4000) && launcher.Kills == 0, "real collector EOF did not stop gracefully");
            Check(sibling.IsAlive, "stopping one collector stopped its sibling");
        }
        finally
        {
            graceful.RequestStop();
            sibling.RequestStop();
            Check(graceful.Wait(4000) && sibling.Wait(4000) && siblingLauncher.Kills == 0, "parallel collectors leaked inherited pipe handles");
        }
        var blockedLauncher = new TrackingLauncher();
        var blocked = new CollectorProcess(() => Self("--child-block"), () => new string('x', 2 * 1024 * 1024),
            blockedLauncher, snapshot => { });
        blocked.Start();
        Check(blockedLauncher.Writing.WaitOne(3000), "real bootstrap not writing");
        Thread.Sleep(100);
        var elapsed = Stopwatch.StartNew();
        blocked.RequestStop();
        Check(blocked.Wait(4000) && elapsed.ElapsedMilliseconds < 4000 && blockedLauncher.Kills == 1, "real blocked bootstrap leaked child");
        var errors = new List<CollectorSnapshot>();
        var failed = new CollectorProcess(() => Self("--child-error"), null, new TrackingLauncher(),
            snapshot => { lock (errors) errors.Add(snapshot); });
        failed.Start();
        Check(failed.Wait(5000), "failed child not reaped");
        lock (errors)
        {
            string error = errors.Last().Errors.Single();
            Check(error.Contains("exit 23") && error.Contains("synthetic-end"), "genuine process failure or diagnostics lost: " + error);
            Check(error.Length < 4500, "stderr not capped");
        }
    }

    private static ProcessStartInfo PythonBootstrap()
    {
        string installations = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Python");
        var directories = new List<string>();
        if (Directory.Exists(installations)) directories.AddRange(Directory.GetDirectories(installations));
        directories.AddRange((Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator));
        foreach (string directory in directories.Where(value => !String.IsNullOrWhiteSpace(value)))
            foreach (string name in new[] { "python.exe", "py.exe" })
            {
                string executable = Path.Combine(directory.Trim('"'), name);
                if (File.Exists(executable))
                    return NativeGadgetEnvironment.Command(executable,
                        (name == "py.exe" ? "-3 " : "") + "-u -c " + NativeGadgetEnvironment.Quote(NativeGadgetEnvironment.Bootstrap));
            }
        throw new Exception("Native Python is required for the production bootstrap pipe regression.");
    }
    private static void BootstrapEncoding()
    {
        var sources = new Dictionary<string, string>
        {
            { "outer_progress", "marker = '\\u03bb'\n" },
            { "activity", "" },
            { "session_probe", "import sys, outer_progress\n" +
                "def watch():\n" +
                "    assert outer_progress.marker == '\\u03bb'\n" +
                "    print(" + new JavaScriptSerializer().Serialize(Valid) + ", flush=True)\n" +
                "    sys.stdin.read()\n" }
        };
        string payload = new JavaScriptSerializer().Serialize(sources);
        Check(payload.All(character => character < 128), "bootstrap regression must exercise an ASCII payload");
        var originalEncoding = Console.InputEncoding;
        try
        {
            foreach (int codepage in new[] { 65001, 437 })
            {
                Console.InputEncoding = Encoding.GetEncoding(codepage);
                var received = new List<CollectorSnapshot>();
                var launcher = new TrackingLauncher();
                var worker = new CollectorProcess(PythonBootstrap, () => payload, launcher,
                    snapshot => { lock (received) received.Add(snapshot); });
                worker.Start();
                try
                {
                    Eventually(delegate { lock (received) return received.Any(snapshot => snapshot.Sessions.Length == 1); },
                        "production bootstrap rejected native stdin at codepage " + codepage);
                }
                catch (Exception error)
                {
                    lock (received) throw new Exception(error.Message + ": " +
                        String.Join("; ", received.SelectMany(snapshot => snapshot.Errors)), error);
                }
                finally
                {
                    worker.RequestStop();
                    Check(worker.Wait(4000), "bootstrap child did not stop");
                }
                lock (received) Check(received.Count == 1 && received[0].Errors.Length == 0, "bootstrap or normal cancellation emitted errors");
                Check(launcher.Kills == 0 && worker.ShutdownError == null, "bootstrap EOF was not graceful");
                Check(Console.InputEncoding.CodePage == codepage, "collector changed the process input encoding");
            }
        }
        finally { Console.InputEncoding = originalEncoding; }
        Check(Console.InputEncoding.CodePage == originalEncoding.CodePage, "test failed to restore input encoding");
    }
    private static void TerminalErrors()
    {
        foreach (bool recover in new[] { false, true })
        {
            var state = new SourceAggregator();
            var worker = new CollectorProcess(() => Self(recover ? "--child-startup-recover" : "--child-startup-error"),
                null, new SystemCollectorLauncher(), snapshot => state.Update("Windows", snapshot, 0));
            worker.Start();
            Check(worker.Wait(5000), "startup failure child not reaped");
            var result = state.Display(0);
            Check(result.errors.Any(error => error.Contains("exit 1")), "startup exit status lost");
            Check(result.errors.Count(error => error.Contains("specific startup cause")) == (recover ? 0 : 1),
                "startup cause lost, duplicated, or retained after recovery");
            Check(result.errors.Length == (recover ? 1 : 2), "terminal diagnostics accumulated previous snapshots");
            Check(result.rows.Length == (recover ? 1 : 0) && result.rows.All(row => row.status == "Unknown"),
                "terminal failure retained a false success");
        }
        var snapshots = new List<CollectorSnapshot>();
        var cancelled = new CollectorProcess(() => Self("--child-startup-cancel"), null, new SystemCollectorLauncher(),
            snapshot => { lock (snapshots) snapshots.Add(snapshot); });
        cancelled.Start();
        try
        {
            Eventually(delegate { lock (snapshots) return snapshots.Count == 1; }, "startup error not received before cancellation");
        }
        finally
        {
            cancelled.RequestStop();
            Check(cancelled.Wait(4000), "cancelled startup child not reaped");
        }
        lock (snapshots) Check(snapshots.Count == 1 && snapshots[0].Errors.Single() == "specific startup cause",
            "normal cancellation added terminal diagnostics");
    }

    private static int WslProbe(string distro)
    {
        var environment = new NativeGadgetEnvironment(AppDomain.CurrentDomain.BaseDirectory, null);
        Check(environment.RunningDistros(new ManualResetEvent(false)).Contains(distro),
            "Live probe refuses to wake a stopped distribution");
        var received = new List<CollectorSnapshot>();
        var healthy = new ManualResetEvent(false);
        var collector = environment.Wsl(distro, snapshot =>
        {
            lock (received) received.Add(snapshot);
            if (snapshot.Errors.Length == 0) healthy.Set();
        });
        collector.Start();
        try
        {
            Check(healthy.WaitOne(45000), "Live WSL collector did not publish a healthy versioned snapshot");
            Thread.Sleep(2000);
            lock (received)
            {
                Check(received.Last().Errors.Length == 0 && collector.IsAlive, "Live WSL collector failed after startup");
                Console.WriteLine("WSL probe: healthy=true snapshots=" + received.Count +
                    " sessions=" + received.Last().Sessions.Length);
            }
        }
        finally
        {
            collector.RequestStop();
            Check(collector.Wait(5000), "Live WSL collector did not stop");
            lock (received)
                foreach (string error in received.SelectMany(snapshot => snapshot.Errors).Distinct())
                    Console.WriteLine("WSL probe error: " + error);
            Check(collector.ShutdownError == null, "Live WSL shutdown failed: " + collector.ShutdownError);
            Console.WriteLine("WSL probe: shutdown=clean");
        }
        return 0;
    }

    private sealed class FakeCollector : ICollector
    {
        private readonly Action<CollectorSnapshot> publish;
        public volatile bool Alive;
        public int Starts;
        public int Stops;
        public ManualResetEvent StartEntered;
        public ManualResetEvent StartRelease;
        public FakeCollector(Action<CollectorSnapshot> callback) { publish = callback; }
        public bool IsAlive { get { return Alive; } }
        public string ShutdownError { get { return null; } }
        public void Start(WaitHandle cancellation = null)
        {
            if (StartEntered != null) StartEntered.Set();
            if (StartRelease != null) StartRelease.WaitOne();
            if (cancellation != null && cancellation.WaitOne(0)) return;
            Starts++; Alive = true; publish(Sample());
        }
        public void RequestStop() { Interlocked.Increment(ref Stops); Alive = false; }
        public bool Wait(int milliseconds) { return !Alive; }
        public void Send(CollectorSnapshot snapshot) { publish(snapshot); }
    }
    private sealed class FakeEnvironment : IGadgetEnvironment
    {
        public GadgetConfiguration Config = new GadgetConfiguration(null);
        public bool ConfigFails;
        public bool DiscoveryFails;
        public double Clock;
        public string[] Running = new[] { "Ubuntu", "Debian" };
        public int Discoveries;
        public readonly Dictionary<string, FakeCollector> Collectors = new Dictionary<string, FakeCollector>();
        public readonly ManualResetEvent Discovering = new ManualResetEvent(false);
        public ManualResetEvent DiscoveryRelease;
        public ManualResetEvent CollectorStartEntered;
        public ManualResetEvent CollectorStartRelease;
        public double Now { get { return Clock; } }
        public GadgetConfiguration LoadConfiguration()
        {
            if (ConfigFails) throw new ArgumentException("synthetic invalid config");
            return Config;
        }
        public string[] RunningDistros(WaitHandle cancellation)
        {
            Interlocked.Increment(ref Discoveries);
            Discovering.Set();
            if (DiscoveryRelease != null) DiscoveryRelease.WaitOne();
            if (DiscoveryFails) throw new IOException("synthetic discovery failure");
            return Running;
        }
        private ICollector Create(string name, Action<CollectorSnapshot> callback)
        {
            var collector = new FakeCollector(callback);
            collector.StartEntered = CollectorStartEntered;
            collector.StartRelease = CollectorStartRelease;
            lock (Collectors) Collectors[name] = collector;
            return collector;
        }
        public ICollector Windows(Action<CollectorSnapshot> callback) { return Create("Windows", callback); }
        public ICollector Wsl(string distro, Action<CollectorSnapshot> callback) { return Create("WSL:" + distro, callback); }
        public FakeCollector Get(string name) { lock (Collectors) { FakeCollector result; return Collectors.TryGetValue(name, out result) ? result : null; } }
    }
    private static void Monitor()
    {
        var system = new FakeEnvironment { Config = new GadgetConfiguration(new[] { "ubuntu", "Stopped" }) };
        Snapshot latest = null;
        var gate = new object();
        var host = new GadgetHost(system, snapshot => { lock (gate) latest = snapshot; });
        host.Start();
        try
        {
            Eventually(() => system.Get("WSL:Ubuntu") != null, "allowed collector not started");
            Check(system.Get("WSL:Debian") == null && system.Get("WSL:Stopped") == null, "allowlist woke unwanted distro");
            Eventually(delegate { lock (gate) return latest != null && latest.rows.Length == 2; }, "source aggregation not published");
            system.DiscoveryFails = true; system.Clock = 11; system.Get("Windows").Send(Sample());
            Eventually(delegate { lock (gate) return latest.errors.Any(error => error.Contains("synthetic discovery failure")); }, "discovery failure hidden");
            lock (gate) Check(latest.rows.Length == 2, "discovery failure removed healthy sources");
            var removed = system.Get("WSL:Ubuntu");
            system.DiscoveryFails = false; system.Running = new string[0]; system.Clock = 22; system.Get("Windows").Send(Sample());
            Eventually(() => removed.Stops > 0, "removed source not stopped");
            removed.Send(Sample());
            Eventually(delegate { lock (gate) return latest.rows.Length == 1; }, "removed source resurrected from late callback");
            lock (gate) Check(latest.rows[0].source == "Windows", "healthy source removed");
        }
        finally { host.RequestStop(); Check(host.Wait(4000), "host did not stop"); }
        Check(system.Get("Windows").Stops > 0, "host leaked Windows collector");
        foreach (bool invalid in new[] { false, true })
        {
            var disabled = new FakeEnvironment { Config = new GadgetConfiguration(new string[0]), ConfigFails = invalid };
            Snapshot result = null;
            var disabledHost = new GadgetHost(disabled, snapshot => result = snapshot);
            disabledHost.Start();
            Eventually(() => result != null, "disabled monitor did not publish");
            disabledHost.RequestStop(); Check(disabledHost.Wait(4000), "disabled monitor did not stop");
            Check(disabled.Discoveries == 0 && disabled.Get("Windows").Starts == 1, "disabled/invalid config stopped Windows or probed WSL");
            Check(result.errors.Any(error => error.Contains("gadget.json")) == invalid, "configuration failure hidden");
        }
    }
    private static void MonitorCancellation()
    {
        var before = new FakeEnvironment();
        var stopped = new GadgetHost(before, snapshot => { });
        stopped.RequestStop(); stopped.Start();
        Check(stopped.Wait(3000) && before.Get("Windows") == null && before.Discoveries == 0, "stopped host launched");
        var during = new FakeEnvironment { DiscoveryRelease = new ManualResetEvent(false) };
        var host = new GadgetHost(during, snapshot => { });
        host.Start();
        Check(during.Discovering.WaitOne(3000), "discovery not entered");
        host.RequestStop(); during.DiscoveryRelease.Set();
        Check(host.Wait(3000) && during.Get("WSL:Ubuntu") == null && !during.Get("Windows").Alive, "late launch after discovery cancellation");
        var delayed = new FakeEnvironment { CollectorStartEntered = new ManualResetEvent(false), CollectorStartRelease = new ManualResetEvent(false) };
        var delayedHost = new GadgetHost(delayed, snapshot => { });
        delayedHost.Start();
        Check(delayed.CollectorStartEntered.WaitOne(3000), "delayed worker start not entered");
        delayedHost.RequestStop();
        delayed.CollectorStartRelease.Set();
        Check(delayedHost.Wait(3000) && delayed.Get("Windows").Starts == 0, "host cancellation did not reach queued worker launch");
    }

    private static void UiLifecycle()
    {
        for (int iteration = 0; iteration < 3; iteration++)
        {
            using (var process = Process.Start(Self("--ui-host")))
            {
                if (!process.WaitForExit(15000)) { process.Kill(); throw new Exception("Normal UI host closure timed out"); }
                string output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
                Check(process.ExitCode == 0, "Normal UI host closure failed: " + output);
            }
        }
        using (var process = Process.Start(Self("--ui-stdin")))
        {
            process.StandardInput.WriteLine("{\"rows\":[],\"errors\":[],\"discovering\":false}");
            process.StandardInput.Close();
            if (!process.WaitForExit(15000)) { process.Kill(); throw new Exception("UI stdin EOF did not close window"); }
            Check(process.ExitCode == 0, "UI stdin compatibility failed: " + process.StandardError.ReadToEnd());
        }
    }
    private sealed class UiEnvironment : IGadgetEnvironment
    {
        public ICollector Worker;
        public readonly TrackingLauncher Launcher = new TrackingLauncher();
        public readonly ManualResetEvent Received = new ManualResetEvent(false);
        private readonly Stopwatch clock = Stopwatch.StartNew();
        public double Now { get { return clock.Elapsed.TotalSeconds; } }
        public GadgetConfiguration LoadConfiguration() { return new GadgetConfiguration(new string[0]); }
        public string[] RunningDistros(WaitHandle cancellation) { throw new Exception("Disabled WSL was probed"); }
        public ICollector Wsl(string distro, Action<CollectorSnapshot> callback) { throw new Exception("Disabled WSL was launched"); }
        public ICollector Windows(Action<CollectorSnapshot> callback)
        {
            Worker = new CollectorProcess(() => Self("--child-recover"), null, Launcher, snapshot =>
            {
                callback(snapshot);
                if (snapshot.Sessions.Length > 0) Received.Set();
            });
            return Worker;
        }
    }
    private static int RunUi(bool stdin)
    {
        var environment = new UiEnvironment();
        Exception failure = null;
        string settings = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "ui-test-" + Guid.NewGuid(), "gadget-ui.json");
        int result = GadgetApplication.Run(stdin ? new[] { "--collector-stdin" } : new string[0], environment, settings, window =>
        {
            if (stdin) return;
            var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(250) };
            var timeout = Stopwatch.StartNew();
            timer.Tick += delegate
            {
                if (!environment.Received.WaitOne(0) && timeout.ElapsedMilliseconds < 10000) return;
                timer.Stop();
                try
                {
                    Check(environment.Received.WaitOne(0) && environment.Worker.IsAlive, "default UI did not start native collector");
                    var snapshot = new Snapshot { rows = Sample().Sessions, errors = new string[0] };
                    snapshot.rows[0].source = "Windows";
                    window.Apply(snapshot);
                    window.Rows[0].MarkUnknown();
                    Check(snapshot.rows[0].status == "Done", "UI mutated incoming host snapshot");
                }
                catch (Exception error) { failure = error; }
                finally { window.Window.Close(); }
            };
            timer.Start();
        });
        if (failure != null) throw failure;
        if (!stdin) Check(!environment.Worker.IsAlive && environment.Launcher.Kills == 0, "UI close left collector alive or skipped graceful EOF");
        else Check(environment.Worker == null, "stdin mode launched collector");
        return result;
    }

    [STAThread]
    public static int Main(string[] args)
    {
        try
        {
            if (args.Length > 0)
            {
                switch (args[0])
                {
                    case "--child-recover": Console.WriteLine("{bad"); Console.WriteLine(Valid); Console.Out.Flush(); Console.In.ReadToEnd(); return 0;
                    case "--child-block": Thread.Sleep(60000); return 0;
                    case "--child-error": Console.Error.Write(new string('x', 12000) + "synthetic-end"); return 23;
                    case "--child-startup-error":
                    case "--child-startup-recover":
                        for (int iteration = 0; iteration < 100; iteration++) Console.WriteLine(StartupError);
                        if (args[0] == "--child-startup-recover") Console.WriteLine(Valid);
                        Console.Out.Flush();
                        return 1;
                    case "--child-startup-cancel": Console.WriteLine(StartupError); Console.Out.Flush(); Console.In.ReadToEnd(); return 0;
                    case "--wsl-probe": return WslProbe(args[1]);
                    case "--ui-host": return RunUi(false);
                    case "--ui-stdin": return RunUi(true);
                }
            }
            Test("configuration null/empty/allowlist/invalid/missing", Configuration);
            Test("strict versioned protocol, enum and clones", Protocol);
            Test("source failure/freshness/recovery/removal", Aggregation);
            Test("cancellation before/during launch and blocked bootstrap", Cancellation);
            Test("graceful EOF and genuine versus expected I/O errors", GracefulAndErrors);
            Test("real synthetic child pipes, bounded bootstrap and diagnostics", RealPipes);
            Test("production bootstrap accepts optional BOM across console codepages", BootstrapEncoding);
            Test("terminal errors preserve startup cause and clear it after recovery", TerminalErrors);
            Test("Windows/WSL launch ABI, ASCII bootstrap and missing runtimes", LaunchContracts);
            Test("monitor allowlists/discovery failures/source removal", Monitor);
            Test("monitor cancellation prevents late launch", MonitorCancellation);
            Test("repeated native UI close and collector-stdin EOF", UiLifecycle);
            Check(NativeGadgetEnvironment.DecodeDistros(Encoding.Unicode.GetBytes("Ubuntu\r\nDebian\r\n")).Length == 2, "UTF16 discovery");
            Check(NativeGadgetEnvironment.DecodeDistros(Encoding.UTF8.GetBytes("Ubuntu\n")).Single() == "Ubuntu", "UTF8 discovery");
            Console.WriteLine("GadgetHostTests: " + passed + " groups passed.");
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
