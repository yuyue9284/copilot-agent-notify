using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace CopilotSessions
{
    public enum SessionNotificationKind { NeedsInput, Completed }

    public interface ISessionNotifier
    {
        void Notify(SessionData session, SessionNotificationKind kind, Action<string> completed);
    }

    public sealed class NativeSessionNotifier : ISessionNotifier
    {
        private readonly string executable;

        public NativeSessionNotifier(string directory)
        {
            executable = Path.Combine(directory, "copilot-notify.exe");
        }

        public void Notify(SessionData session, SessionNotificationKind kind, Action<string> completed)
        {
            SessionData copy = session.Clone();
            ThreadPool.QueueUserWorkItem(delegate
            {
                string error = null;
                try
                {
                    if (!File.Exists(executable))
                        throw new IOException("Missing packaged notifier: " + executable + ". Rebuild or reinstall Copilot Sessions.");
                    string shortId = copy.id.Substring(0, Math.Min(8, copy.id.Length));
                    string title = "[" + copy.source + "] " + copy.title + " [#" + shortId + "]";
                    string message = kind == SessionNotificationKind.NeedsInput
                        ? "Copilot needs permission or input."
                        : "Agent finished responding.";
                    if (!String.IsNullOrEmpty(copy.cwd)) message += Environment.NewLine + "Go to: " + copy.cwd;
                    var info = new ProcessStartInfo(executable,
                        NativeGadgetEnvironment.Quote(title) + " " +
                        NativeGadgetEnvironment.Quote(message) + " " +
                        NativeGadgetEnvironment.Quote(shortId))
                    {
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        RedirectStandardError = true
                    };
                    using (Process process = Process.Start(info))
                    {
                        if (!process.WaitForExit(10000))
                        {
                            process.Kill();
                            process.WaitForExit(1500);
                            throw new IOException("Notification helper timed out.");
                        }
                        string diagnostics = process.StandardError.ReadToEnd().Trim();
                        if (process.ExitCode != 0)
                            throw new IOException("Notification helper exited with code " + process.ExitCode +
                                (diagnostics.Length == 0 ? "." : ": " + diagnostics));
                    }
                }
                catch (Exception exception)
                {
                    if (!(exception is IOException) && !(exception is InvalidOperationException)
                        && !(exception is System.ComponentModel.Win32Exception)
                        && !(exception is UnauthorizedAccessException))
                        throw;
                    error = "Desktop notification failed: " + exception.Message;
                }
                if (completed != null) completed(error);
            });
        }
    }
}
