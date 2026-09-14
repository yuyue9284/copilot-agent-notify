using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices.ComTypes;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;

internal static class CopilotNotify
{
    private const string DefaultAppId =
        "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App";

    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--install-shortcut")
        {
            if (args.Length != 8)
            {
                Console.Error.WriteLine(
                    "Usage: copilot-notify.exe --install-shortcut SHORTCUT TARGET ARGUMENTS WORKING_DIRECTORY ICON APP_ID DESCRIPTION");
                return 2;
            }
            try
            {
                InstallShortcut(args[1], args[2], args[3], args[4], args[5], args[6], args[7]);
                return 0;
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine(exception);
                return 1;
            }
        }
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

    private static void InstallShortcut(string shortcut, string target, string arguments,
        string workingDirectory, string icon, string appId, string description)
    {
        var link = (IShellLinkW)new ShellLink();
        try
        {
            link.SetPath(target);
            link.SetArguments(arguments);
            link.SetWorkingDirectory(workingDirectory);
            link.SetIconLocation(icon, 0);
            link.SetDescription(description);
            var store = (IPropertyStore)link;
            var key = new PropertyKey(
                new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);
            var value = new PropVariant {
                VariantType = 31,
                Pointer = Marshal.StringToCoTaskMemUni(appId)
            };
            try
            {
                store.SetValue(ref key, ref value);
                store.Commit();
            }
            finally { PropVariantClear(ref value); }
            Directory.CreateDirectory(Path.GetDirectoryName(shortcut));
            ((IPersistFile)link).Save(shortcut, true);
        }
        finally { Marshal.FinalReleaseComObject(link); }
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

    [ComImport]
    [Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport]
    [Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length,
            IntPtr findData, uint flags);
        void GetIDList(out IntPtr itemIdList);
        void SetIDList(IntPtr itemIdList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder description, int length);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string description);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory, int length);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments, int length);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCommand);
        void SetShowCmd(int showCommand);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder icon, int length,
            out int iconIndex);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string icon, int iconIndex);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    [ComImport]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    private struct PropertyKey
    {
        public Guid FormatId;
        public uint PropertyId;
        public PropertyKey(Guid formatId, uint propertyId)
        {
            FormatId = formatId;
            PropertyId = propertyId;
        }
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct PropVariant
    {
        [FieldOffset(0)] public ushort VariantType;
        [FieldOffset(8)] public IntPtr Pointer;
    }

    [DllImport("ole32.dll", PreserveSig = true)]
    private static extern int PropVariantClear(ref PropVariant variant);

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
