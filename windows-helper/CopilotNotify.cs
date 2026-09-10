using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;

internal static class CopilotNotify
{
    private const string DefaultAppId =
        "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App";

    private static int Main(string[] args)
    {
        bool bell = args.Length > 0 && args[0] == "--bell";
        if (bell)
        {
            var remaining = new string[args.Length - 1];
            Array.Copy(args, 1, remaining, 0, remaining.Length);
            args = remaining;
        }
        if (args.Length < 2 || args.Length > 4)
        {
            Console.Error.WriteLine(
                "Usage: copilot-notify.exe [--bell] TITLE MESSAGE [TAG] [APP_ID]");
            return 2;
        }

        try
        {
            var title = args[0];
            var message = args[1];
            var tag = args.Length >= 3 ? args[2] : "copilot";
            var appId = args.Length >= 4 ? args[3] : DefaultAppId;

            var xml = ToastNotificationManager.GetTemplateContent(
                ToastTemplateType.ToastText02);
            var textNodes = xml.GetElementsByTagName("text");
            textNodes[0].AppendChild(xml.CreateTextNode(title));
            textNodes[1].AppendChild(xml.CreateTextNode(message));

            var toast = new ToastNotification(xml)
            {
                Tag = tag.Substring(0, Math.Min(16, tag.Length)),
                Group = "CopilotCLI",
                ExpirationTime = DateTimeOffset.Now.AddHours(1)
            };

            ToastNotificationManager.CreateToastNotifier(appId).Show(toast);
            if (bell)
                RingBell((uint)Process.GetCurrentProcess().Id);
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception);
            return 1;
        }
    }

    private static void RingBell(uint processId)
    {
        var error = Console.Error;
        var processes = new Dictionary<uint, ProcessEntry>();
        using (var snapshot = CreateToolhelp32Snapshot(2, 0))
        {
            var entry = new ProcessEntry();
            entry.Size = (uint)Marshal.SizeOf(typeof(ProcessEntry));
            if (!snapshot.IsInvalid && Process32First(snapshot, ref entry))
            {
                do { processes[entry.Id] = entry; }
                while (Process32Next(snapshot, ref entry));
            }
        }
        var ancestors = new List<uint>();
        uint zellijPaneConsole = 0;
        uint pid = processId;
        var seen = new HashSet<uint>();
        while (processes.ContainsKey(pid) && seen.Add(pid))
        {
            uint parent = processes[pid].ParentId;
            ProcessEntry parentEntry;
            if (zellijPaneConsole == 0 &&
                processes.TryGetValue(parent, out parentEntry) &&
                String.Equals(parentEntry.ExeFile, "zellij.exe",
                    StringComparison.OrdinalIgnoreCase))
            {
                // The nearest child of Zellij is the shell in the originating pane.
                zellijPaneConsole = pid;
            }
            if (processes.TryGetValue(parent, out parentEntry) &&
                String.Equals(parentEntry.ExeFile, "WindowsTerminal.exe",
                    StringComparison.OrdinalIgnoreCase))
            {
                // Ring both levels: native Zellij does not forward the pane bell
                // to Windows Terminal in the observed setup.
                if (zellijPaneConsole != 0 && zellijPaneConsole != pid &&
                    !RingConsole(zellijPaneConsole))
                    error.WriteLine("copilot-notify: Zellij pane console unavailable; inner bell skipped.");
                if (!RingConsole(pid))
                    error.WriteLine("copilot-notify: outer Windows Terminal console unavailable; bell skipped.");
                return;
            }
            pid = parent;
            if (pid == 0)
                break;
            ancestors.Add(pid);
        }

        if (WriteBell())
            return;

        // Without a Windows Terminal ancestor, retain the nearest-console behavior.
        foreach (uint ancestor in ancestors)
        {
            if (!AttachConsole(ancestor))
                continue;
            try
            {
                if (WriteBell())
                    return;
            }
            finally { FreeConsole(); }
        }
        error.WriteLine("copilot-notify: no ancestor console available; bell skipped.");
    }

    private static bool RingConsole(uint pid)
    {
        FreeConsole();
        if (!AttachConsole(pid))
            return false;
        try { return WriteBell(); }
        finally { FreeConsole(); }
    }

    private static bool WriteBell()
    {
        using (var output = CreateFile("CONOUT$", 0x40000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero))
        {
            if (output.IsInvalid)
                return false;
            uint written;
            return WriteConsole(output, "\a", 1, out written, IntPtr.Zero) && written == 1;
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ProcessEntry
    {
        public uint Size, Usage, Id;
        public IntPtr DefaultHeap;
        public uint ModuleId, Threads, ParentId;
        public int Priority;
        public uint Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string ExeFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeFileHandle CreateToolhelp32Snapshot(uint flags, uint pid);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Process32First(SafeFileHandle snapshot, ref ProcessEntry entry);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Process32Next(SafeFileHandle snapshot, ref ProcessEntry entry);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AttachConsole(uint pid);
    [DllImport("kernel32.dll")]
    private static extern bool FreeConsole();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string name, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool WriteConsole(SafeFileHandle output, string text, uint length,
        out uint written, IntPtr reserved);
}
