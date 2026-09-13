using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;

namespace CopilotSessions
{
    public interface ICollectorChild : IDisposable
    {
        TextReader Output { get; }
        TextReader Error { get; }
        TextWriter Input { get; }
        bool HasExited { get; }
        int ExitCode { get; }
        bool WaitForExit(int milliseconds);
        void Kill();
    }

    public interface ICollectorLauncher
    {
        ICollectorChild Start(ProcessStartInfo info);
    }

    public sealed class SystemCollectorLauncher : ICollectorLauncher
    {
        private sealed class Child : ICollectorChild
        {
            private readonly Process process;
            public Child(Process value) { process = value; }
            public TextReader Output { get { return process.StandardOutput; } }
            public TextReader Error { get { return process.StandardError; } }
            public TextWriter Input { get { return process.StandardInput; } }
            public bool HasExited { get { return process.HasExited; } }
            public int ExitCode { get { return process.ExitCode; } }
            public bool WaitForExit(int milliseconds) { return process.WaitForExit(milliseconds); }
            public void Kill() { if (!process.HasExited) process.Kill(); }
            public void Dispose() { process.Dispose(); }
        }
        public ICollectorChild Start(ProcessStartInfo info)
        {
            return new Child(Process.Start(info));
        }
    }

    public interface ICollector
    {
        bool IsAlive { get; }
        string ShutdownError { get; }
        void Start(WaitHandle cancellation = null);
        void RequestStop();
        bool Wait(int milliseconds);
    }

    public sealed class CollectorProcess : ICollector
    {
        private readonly Func<ProcessStartInfo> command;
        private readonly Func<string> bootstrap;
        private readonly ICollectorLauncher launcher;
        private readonly Action<CollectorSnapshot> publish;
        private readonly object gate = new object();
        private readonly object cleanupGate = new object();
        private readonly ManualResetEvent stopping = new ManualResetEvent(false);
        private readonly ManualResetEvent complete = new ManualResetEvent(false);
        private readonly StringBuilder diagnostics = new StringBuilder();
        private ICollectorChild child;
        private Thread thread;
        private volatile bool ready;
        private bool cleaned;
        private int started;
        private int stopQueued;
        private WaitHandle ownerCancellation;
        public string ShutdownError { get; private set; }

        public CollectorProcess(Func<ProcessStartInfo> info, Func<string> payload,
            ICollectorLauncher processLauncher, Action<CollectorSnapshot> onSnapshot)
        {
            command = info; bootstrap = payload; launcher = processLauncher; publish = onSnapshot;
        }
        public bool IsAlive { get { return started != 0 && !complete.WaitOne(0); } }
        private bool IsStopping { get { return stopping.WaitOne(0) || (ownerCancellation != null && ownerCancellation.WaitOne(0)); } }
        public void Start(WaitHandle cancellation = null)
        {
            if (Interlocked.Exchange(ref started, 1) != 0) throw new InvalidOperationException("Collector already started.");
            ownerCancellation = cancellation;
            thread = new Thread(Run) { IsBackground = true, Name = "Session collector" };
            thread.Start();
        }
        private void Send(CollectorSnapshot snapshot)
        {
            lock (gate)
                if (!IsStopping) publish(snapshot);
        }
        private void ReadDiagnostics()
        {
            try
            {
                var buffer = new char[512];
                int count;
                while ((count = child.Error.Read(buffer, 0, buffer.Length)) > 0)
                {
                    lock (diagnostics)
                    {
                        diagnostics.Append(buffer, 0, count);
                        if (diagnostics.Length > 4096) diagnostics.Remove(0, diagnostics.Length - 4096);
                    }
                }
            }
            catch (Exception error)
            {
                if (IsExpected(error)) Send(CollectorSnapshot.Failure("Collector diagnostics: " + error.Message));
                else throw;
            }
        }
        internal static bool IsExpected(Exception error)
        {
            return error is IOException || error is ArgumentException || error is InvalidOperationException
                || error is System.ComponentModel.Win32Exception || error is UnauthorizedAccessException
                || error is System.Security.SecurityException || error is NotSupportedException;
        }
        private void Run()
        {
            Thread errorReader = null;
            string[] reportedErrors = new string[0];
            try
            {
                if (IsStopping) return;
                string payload = bootstrap == null ? null : bootstrap();
                ProcessStartInfo info = command();
                // Publication and launch are serialized; cancellation is signalled
                // without taking this lock, including while Process.Start is pending.
                lock (gate)
                {
                    if (IsStopping) return;
                    child = launcher.Start(info);
                }
                if (IsStopping) return;
                errorReader = new Thread(ReadDiagnostics) { IsBackground = true, Name = "Collector diagnostics" };
                errorReader.Start();
                if (payload != null)
                {
                    child.Input.Write(payload);
                    child.Input.Write("\n");
                    child.Input.Flush();
                }
                ready = true;
                string line;
                while (!IsStopping && (line = ReadLine(child.Output)) != null)
                {
                    CollectorSnapshot snapshot;
                    try { snapshot = SnapshotProtocol.ParseCollector(line); }
                    catch (ArgumentException error) { snapshot = CollectorSnapshot.Failure("Invalid collector response: " + error.Message); }
                    // Replace rather than accumulate: a successful snapshot clears
                    // earlier errors, and repeated watch errors cannot grow this state.
                    reportedErrors = snapshot.Errors;
                    Send(snapshot);
                }
                if (!IsStopping)
                {
                    errorReader.Join(200);
                    // Pipe EOF can precede the process handle becoming signalled.
                    child.WaitForExit(200);
                    string details;
                    lock (diagnostics) details = diagnostics.ToString().Trim();
                    SendTerminal(reportedErrors, "Collector stopped" +
                        (child.HasExited ? " (exit " + child.ExitCode + ")" : "") + "." +
                        (details.Length == 0 ? "" : " " + details));
                }
            }
            catch (Exception error)
            {
                if (!IsExpected(error)) throw;
                SendTerminal(reportedErrors, "Collector failed: " + error.Message);
            }
            finally
            {
                Cleanup();
                if (errorReader != null) errorReader.Join(500);
                if (child != null) child.Dispose();
                complete.Set();
            }
        }
        private void SendTerminal(string[] reportedErrors, string message)
        {
            Send(new CollectorSnapshot(new SessionData[0], reportedErrors.Concat(new[] { message }).Distinct()));
        }
        public static string ReadLine(TextReader reader)
        {
            var line = new StringBuilder();
            int value;
            while ((value = reader.Read()) != -1)
            {
                if (value == '\n') return line.ToString().TrimEnd('\r');
                if (line.Length >= SnapshotProtocol.MaxLineLength) throw new ArgumentException("Collector message exceeds the size limit.");
                line.Append((char)value);
            }
            return line.Length == 0 ? null : line.ToString();
        }
        private void Cleanup()
        {
            lock (cleanupGate)
            {
                if (cleaned) return;
                ICollectorChild process;
                lock (gate) process = child;
                if (process == null) return;
                cleaned = true;
                try
                {
                    if (!ready)
                    {
                        // Never close a buffered writer concurrently with a blocked
                        // bootstrap write: kill the reader first to release the pipe.
                        if (!process.HasExited) process.Kill();
                    }
                    else
                    {
                        try { process.Input.Close(); }
                        catch (IOException error)
                        {
                            if (!process.HasExited)
                            {
                                ShutdownError = "Collector stdin close failed: " + error.Message;
                                Send(CollectorSnapshot.Failure(ShutdownError));
                            }
                        }
                    }
                    if (!process.WaitForExit(1500))
                    {
                        process.Kill();
                        if (!process.WaitForExit(1500))
                        {
                            ShutdownError = "Collector did not stop after termination.";
                            Send(CollectorSnapshot.Failure(ShutdownError));
                        }
                    }
                }
                catch (Exception error)
                {
                    if (!IsExpected(error)) throw;
                    ShutdownError = "Collector shutdown failed: " + error.Message;
                    Send(CollectorSnapshot.Failure(ShutdownError));
                    try { if (!process.HasExited) process.Kill(); }
                    catch (Exception killError) { if (!IsExpected(killError)) throw; }
                }
            }
        }
        public void RequestStop()
        {
            stopping.Set();
            if (Interlocked.Exchange(ref stopQueued, 1) == 0)
                ThreadPool.QueueUserWorkItem(delegate { Cleanup(); });
        }
        public bool Wait(int milliseconds) { return started == 0 || complete.WaitOne(milliseconds); }
    }

    public interface IGadgetEnvironment
    {
        double Now { get; }
        GadgetConfiguration LoadConfiguration();
        string[] RunningDistros(WaitHandle cancellation);
        ICollector Windows(Action<CollectorSnapshot> publish);
        ICollector Wsl(string distro, Action<CollectorSnapshot> publish);
    }

    public sealed class NativeGadgetEnvironment : IGadgetEnvironment
    {
        // Framework stdin may prefix its ASCII-compatible JSON with a UTF-8 BOM.
        public const string Bootstrap = "import json, sys, types\n" +
            "sources = json.loads(sys.stdin.readline().lstrip('\\ufeff'))\n" +
            "for name in ('outer_progress', 'activity', 'session_probe'):\n" +
            "    module = types.ModuleType(name)\n" +
            "    module.__file__ = name + '.py'\n" +
            "    sys.modules[name] = module\n" +
            "    exec(compile(sources[name], module.__file__, 'exec'), module.__dict__)\n" +
            "sys.modules['session_probe'].watch()\n";
        private readonly string directory;
        private readonly string python;
        private readonly Stopwatch clock = Stopwatch.StartNew();
        private readonly ICollectorLauncher launcher;
        public NativeGadgetEnvironment(string applicationDirectory, string interpreter)
            : this(applicationDirectory, interpreter, new SystemCollectorLauncher()) { }
        public NativeGadgetEnvironment(string applicationDirectory, string interpreter, ICollectorLauncher processLauncher)
        {
            directory = applicationDirectory; python = interpreter; launcher = processLauncher;
        }
        public double Now { get { return clock.Elapsed.TotalSeconds; } }
        public GadgetConfiguration LoadConfiguration()
        {
            return GadgetConfiguration.Load(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "CopilotAgentNotify", "gadget.json"));
        }
        public static string Quote(string value)
        {
            var quoted = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\') { slashes++; continue; }
                quoted.Append('\\', character == '"' ? slashes * 2 + 1 : slashes);
                quoted.Append(character);
                slashes = 0;
            }
            return quoted.Append('\\', slashes * 2).Append('"').ToString();
        }
        public static ProcessStartInfo Command(string executable, string arguments)
        {
            var info = new ProcessStartInfo(executable, arguments) { UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = new UTF8Encoding(false), StandardErrorEncoding = new UTF8Encoding(false) };
            info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            return info;
        }
        public static bool IsAbsolutePath(string path)
        {
            try
            {
                string root = Path.GetPathRoot(path);
                return root != null && root.Length >= 3 &&
                    (root.StartsWith(@"\\", StringComparison.Ordinal) || (root[1] == ':' && (root[2] == '\\' || root[2] == '/')));
            }
            catch (ArgumentException) { return false; }
        }
        private static string FindExecutable(string name)
        {
            foreach (string item in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
            {
                if (String.IsNullOrWhiteSpace(item)) continue;
                try
                {
                    string candidate = Path.Combine(item.Trim('"'), name);
                    if (File.Exists(candidate)) return Path.GetFullPath(candidate);
                }
                catch (ArgumentException) { }
            }
            return null;
        }
        public ICollector Windows(Action<CollectorSnapshot> publish)
        {
            return new CollectorProcess(delegate
            {
                string executable = python ?? FindExecutable("py.exe") ?? FindExecutable("python.exe");
                if (executable == null || !IsAbsolutePath(executable) || !File.Exists(executable))
                    throw new IOException("Windows Python was not found. Install Python 3 or launch CopilotSessions.exe --python <absolute python.exe path>.");
                string probe = Path.Combine(directory, "session_probe.py");
                if (!File.Exists(probe)) throw new IOException("Missing packaged collector: " + probe + ". Rebuild or reinstall Copilot Sessions.");
                string args = (String.Equals(Path.GetFileName(executable), "py.exe", StringComparison.OrdinalIgnoreCase) ? "-3 " : "") +
                    "-u " + Quote(probe) + " --watch";
                return Command(executable, args);
            }, null, launcher, publish);
        }
        public ICollector Wsl(string distro, Action<CollectorSnapshot> publish)
        {
            return new CollectorProcess(delegate
            {
                string executable = FindExecutable("wsl.exe");
                if (executable == null) throw new IOException("wsl.exe was not found.");
                // WSL's option parser can retain unnecessary quotes as part of
                // the distro name; match Python list2cmdline for ordinary names.
                string distroArgument = String.IsNullOrEmpty(distro) ||
                    distro.Any(character => Char.IsWhiteSpace(character) || character == '"') ? Quote(distro) : distro;
                return Command(executable, "--distribution " + distroArgument + " --exec python3 -u -c " + Quote(Bootstrap));
            }, delegate
            {
                var sources = new Dictionary<string, string>();
                foreach (string name in new[] { "outer_progress", "activity", "session_probe" })
                    sources.Add(name, File.ReadAllText(Path.Combine(directory, name + ".py")));
                string json = new JavaScriptSerializer { MaxJsonLength = SnapshotProtocol.MaxLineLength }.Serialize(sources);
                // ASCII JSON preserves every source character under legacy console codepages.
                var ascii = new StringBuilder();
                foreach (char character in json)
                    if (character > 127) ascii.Append("\\u").Append(((int)character).ToString("x4"));
                    else ascii.Append(character);
                return ascii.ToString();
            }, launcher, publish);
        }
        public static string[] DecodeDistros(byte[] output)
        {
            string text;
            if (output.Length >= 2 && output[0] == 255 && output[1] == 254)
                text = Encoding.Unicode.GetString(output, 2, output.Length - 2);
            else if (output.Contains((byte)0)) text = Encoding.Unicode.GetString(output);
            else text = Encoding.UTF8.GetString(output);
            return text.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(line => line.Trim()).Where(line => line.Length > 0).Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        }
        public string[] RunningDistros(WaitHandle cancellation)
        {
            string executable = FindExecutable("wsl.exe");
            if (executable == null || cancellation.WaitOne(0)) return new string[0];
            using (var process = new Process { StartInfo = Command(executable, "--list --running --quiet") })
            {
                if (cancellation.WaitOne(0)) return new string[0];
                process.Start();
                var bytes = new MemoryStream();
                Exception readError = null;
                var outputReader = new Thread(delegate()
                {
                    try
                    {
                        var buffer = new byte[4096];
                        int count;
                        while ((count = process.StandardOutput.BaseStream.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            if (bytes.Length + count > 1024 * 1024) throw new IOException("WSL discovery response exceeds the size limit.");
                            bytes.Write(buffer, 0, count);
                        }
                    }
                    catch (Exception error) { readError = error; }
                }) { IsBackground = true };
                var errorReader = new Thread(delegate()
                {
                    try { while (process.StandardError.Read() >= 0) { } }
                    catch (IOException) { }
                    catch (ObjectDisposedException) { }
                }) { IsBackground = true };
                outputReader.Start(); errorReader.Start();
                try
                {
                    var timeout = Stopwatch.StartNew();
                    while (!process.WaitForExit(50))
                    {
                        if (cancellation.WaitOne(0)) return new string[0];
                        if (timeout.Elapsed.TotalSeconds >= 8) throw new IOException("Cannot list running WSL distributions: timed out.");
                        if (readError != null) throw new IOException("Cannot read WSL discovery response.", readError);
                    }
                    outputReader.Join(1000);
                    if (readError != null) throw new IOException("Cannot read WSL discovery response.", readError);
                    if (process.ExitCode != 0) throw new IOException("Cannot list running WSL distributions (exit " + process.ExitCode + ").");
                    if (outputReader.IsAlive) throw new IOException("WSL discovery output did not close.");
                    return DecodeDistros(bytes.ToArray());
                }
                finally
                {
                    if (!process.HasExited) { process.Kill(); process.WaitForExit(1500); }
                    outputReader.Join(1500); errorReader.Join(1500);
                }
            }
        }
    }

    public sealed class GadgetHost
    {
        private readonly IGadgetEnvironment environment;
        private readonly Action<Snapshot> publish;
        private readonly SourceAggregator sources = new SourceAggregator();
        private readonly Dictionary<string, ICollector> workers = new Dictionary<string, ICollector>(StringComparer.OrdinalIgnoreCase);
        private readonly List<ICollector> retired = new List<ICollector>();
        private readonly List<string> shutdownHistory = new List<string>();
        private readonly object gate = new object();
        private readonly ManualResetEvent stopping = new ManualResetEvent(false);
        private readonly ManualResetEvent complete = new ManualResetEvent(false);
        private readonly AutoResetEvent changed = new AutoResetEvent(false);
        private Thread thread;
        private int started;
        private int stopQueued;
        private string[] shutdownErrors = new string[0];
        public string[] ShutdownErrors { get { return (string[])shutdownErrors.Clone(); } }
        public GadgetHost(IGadgetEnvironment system, Action<Snapshot> onSnapshot) { environment = system; publish = onSnapshot; }
        public void Start()
        {
            if (Interlocked.Exchange(ref started, 1) != 0) throw new InvalidOperationException("Host already started.");
            thread = new Thread(Run) { IsBackground = true, Name = "Session monitor" };
            thread.Start();
        }
        private void Update(string source, CollectorSnapshot snapshot)
        {
            lock (gate)
            {
                if (stopping.WaitOne(0)) return;
                sources.Update(source, snapshot, environment.Now);
            }
            changed.Set();
        }
        private void EnsureWorker(string source, Func<Action<CollectorSnapshot>, ICollector> create)
        {
            lock (gate)
            {
                if (stopping.WaitOne(0)) return;
                ICollector worker;
                if (workers.TryGetValue(source, out worker) && worker.IsAlive) return;
                sources.Connecting(source, environment.Now);
                try
                {
                    ICollector owner = null;
                    worker = create(snapshot =>
                    {
                        lock (gate)
                        {
                            ICollector current;
                            if (!workers.TryGetValue(source, out current) || !Object.ReferenceEquals(current, owner)) return;
                            Update(source, snapshot);
                        }
                    });
                    owner = worker;
                    workers[source] = worker;
                    if (stopping.WaitOne(0)) worker.RequestStop();
                    else worker.Start(stopping);
                }
                catch (Exception error)
                {
                    if (!CollectorProcess.IsExpected(error)) throw;
                    sources.Update(source, CollectorSnapshot.Failure(error.Message), environment.Now);
                }
            }
        }
        private void Run()
        {
            try
            {
                GadgetConfiguration config;
                try { config = environment.LoadConfiguration(); }
                catch (Exception error)
                {
                    if (!CollectorProcess.IsExpected(error)) throw;
                    config = new GadgetConfiguration(new string[0]);
                    Update("Configuration", CollectorSnapshot.Failure("gadget.json: " + error.Message));
                }
                double nextDiscovery = Double.NegativeInfinity;
                while (!stopping.WaitOne(0))
                {
                    if (environment.Now >= nextDiscovery)
                    {
                        EnsureWorker("Windows", environment.Windows);
                        try
                        {
                            string[] selected = config.WslDisabled ? new string[0] : config.SelectRunning(environment.RunningDistros(stopping));
                            if (stopping.WaitOne(0)) break;
                            lock (gate)
                            {
                                foreach (string source in workers.Keys.Where(key => key.StartsWith("WSL:", StringComparison.Ordinal)
                                    && !selected.Contains(key.Substring(4), StringComparer.OrdinalIgnoreCase)).ToArray())
                                {
                                    workers[source].RequestStop();
                                    retired.Add(workers[source]);
                                    workers.Remove(source);
                                    sources.Update(source, null, environment.Now);
                                }
                            }
                            Update("WSL discovery", new CollectorSnapshot(new SessionData[0], new string[0]));
                            foreach (string distro in selected)
                            {
                                string name = distro;
                                EnsureWorker("WSL:" + name, callback => environment.Wsl(name, callback));
                            }
                        }
                        catch (Exception error)
                        {
                            if (!CollectorProcess.IsExpected(error)) throw;
                            Update("WSL discovery", CollectorSnapshot.Failure(error.Message));
                        }
                        nextDiscovery = environment.Now + 10;
                    }
                    Snapshot snapshot;
                    lock (gate)
                    {
                        foreach (ICollector worker in retired.Where(worker => !worker.IsAlive && worker.ShutdownError != null))
                            shutdownHistory.Add(worker.ShutdownError);
                        retired.RemoveAll(worker => !worker.IsAlive);
                        if (shutdownHistory.Count != 0)
                            sources.Update("Collector shutdown", new CollectorSnapshot(new SessionData[0], shutdownHistory), environment.Now);
                        snapshot = sources.Display(environment.Now);
                    }
                    if (!stopping.WaitOne(0)) publish(snapshot);
                    WaitHandle.WaitAny(new WaitHandle[] { stopping, changed }, 2000);
                }
            }
            finally
            {
                ICollector[] children;
                lock (gate) children = workers.Values.Concat(retired).ToArray();
                foreach (ICollector worker in children) worker.RequestStop();
                var timeout = Stopwatch.StartNew();
                var errors = new List<string>(shutdownHistory);
                foreach (ICollector worker in children)
                {
                    if (!worker.Wait(Math.Max(0, 6000 - (int)timeout.ElapsedMilliseconds)))
                        errors.Add("Timed out stopping a session collector.");
                    if (worker.ShutdownError != null) errors.Add(worker.ShutdownError);
                }
                shutdownErrors = errors.ToArray();
                complete.Set();
            }
        }
        public void RequestStop()
        {
            stopping.Set();
            if (Interlocked.Exchange(ref stopQueued, 1) != 0) return;
            // Window.Closed never waits on launch, bootstrap I/O, discovery, or joins.
            ThreadPool.QueueUserWorkItem(delegate
            {
                ICollector[] children;
                lock (gate) children = workers.Values.Concat(retired).ToArray();
                foreach (ICollector worker in children) worker.RequestStop();
            });
        }
        public bool Wait(int milliseconds) { return started == 0 || complete.WaitOne(milliseconds); }
    }

    public static class GadgetApplication
    {
        public static int Run(string[] args)
        {
            return Run(args, null, null, null);
        }
        public static int Run(string[] args, IGadgetEnvironment environment, string settingsPath, Action<SessionWindow> loaded)
        {
            bool stdin = false;
            string python = null;
            string argumentError = null;
            for (int index = 0; index < args.Length; index++)
            {
                if (args[index] == "--collector-stdin") stdin = true;
                else if (args[index] == "--python" && index + 1 < args.Length && NativeGadgetEnvironment.IsAbsolutePath(args[index + 1])) python = args[++index];
                else { argumentError = "Usage: CopilotSessions.exe [--python <absolute python.exe path>] [--collector-stdin]"; break; }
            }
            var app = new Application();
            var window = new SessionWindow(settingsPath);
            GadgetHost host = null;
            bool closed = false;
            if (!stdin && argumentError == null)
                host = new GadgetHost(environment ?? new NativeGadgetEnvironment(AppDomain.CurrentDomain.BaseDirectory, python), snapshot =>
                {
                    if (closed || window.Window.Dispatcher.HasShutdownStarted) return;
                    try { window.Window.Dispatcher.BeginInvoke(new Action(delegate { if (!closed) window.Apply(snapshot); })); }
                    catch (InvalidOperationException) { if (!closed) throw; }
                });
            window.Window.Loaded += delegate
            {
                if (argumentError != null) window.Apply(new Snapshot { rows = new SessionData[0], errors = new[] { argumentError } });
                else if (stdin) window.ReadCollector();
                else host.Start();
                if (loaded != null) loaded(window);
            };
            window.Window.Closed += delegate { closed = true; if (host != null) host.RequestStop(); };
            bool clean = true;
            try { app.Run(window.Window); }
            finally
            {
                if (host != null)
                {
                    host.RequestStop();
                    clean = host.Wait(10000);
                    foreach (string error in host.ShutdownErrors) { Console.Error.WriteLine(error); clean = false; }
                    if (!clean && host.ShutdownErrors.Length == 0) Console.Error.WriteLine("Session monitor shutdown timed out.");
                }
            }
            if (!clean) return 1;
            return argumentError == null ? 0 : 2;
        }
    }
}
