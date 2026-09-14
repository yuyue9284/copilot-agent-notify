using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Security;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;

namespace CopilotSessions
{
    internal interface IBackdropNative
    {
        int ReadPreferences(out bool transparency, out bool remote);
        int IsCompositionEnabled(out bool enabled);
        int SetAttribute(IntPtr hwnd, int attribute, int value);
        int ExtendFrame(IntPtr hwnd, bool enabled);
        int SetOpacity(IntPtr hwnd, byte alpha);
        int EnsureTopmost(IntPtr hwnd, bool topmost);
    }

    internal enum WindowAppearance { Opaque, Acrylic, Translucent }
    internal enum AppearancePreference { Auto, Acrylic, Translucent, Solid }

    internal sealed class WindowBackdrop : IDisposable
    {
        private readonly Window window;
        private readonly IBackdropNative native;
        private HwndSource source;
        private volatile bool disposed;
        private bool refreshPending, backdropSet, frameExtended, keepLayered, synchronizingTopmost;
        internal const byte FallbackAlpha = 230;
        internal AppearancePreference Preference { get; private set; }
        internal int OpacityPercent { get; private set; }
        internal byte OpacityAlpha { get { return (byte)Math.Round(OpacityPercent * 255.0 / 100, MidpointRounding.AwayFromZero); } }
        internal WindowAppearance Mode { get; private set; }
        internal bool IsEnabled { get { return Mode == WindowAppearance.Acrylic; } }
        internal event EventHandler Changed;

        internal WindowBackdrop(Window window, IBackdropNative native = null)
        {
            this.window = window;
            this.native = native ?? new DwmNative();
            OpacityPercent = 90;
            window.SourceInitialized += SourceInitialized;
            window.Closed += Closed;
        }

        internal void Configure(AppearancePreference preference, int opacityPercent)
        {
            if (!Enum.IsDefined(typeof(AppearancePreference), preference))
                throw new ArgumentOutOfRangeException("preference");
            if (opacityPercent < 50 || opacityPercent > 100)
                throw new ArgumentOutOfRangeException("opacityPercent");
            Preference = preference;
            OpacityPercent = opacityPercent;
            Refresh(SystemParameters.HighContrast);
        }

        private void SourceInitialized(object sender, EventArgs args)
        {
            source = HwndSource.FromHwnd(new WindowInteropHelper(window).Handle);
            source.AddHook(WindowMessage);
            SystemEvents.SessionSwitch += SessionSwitch;
            Refresh(SystemParameters.HighContrast);
        }

        private void SessionSwitch(object sender, SessionSwitchEventArgs args)
        {
            if (!disposed && !window.Dispatcher.HasShutdownStarted)
                window.Dispatcher.BeginInvoke(new Action(delegate { if (!disposed) QueueRefresh(); }));
        }

        private void QueueRefresh()
        {
            if (refreshPending) return;
            refreshPending = true;
            window.Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(delegate
            {
                refreshPending = false;
                if (!disposed) Refresh(SystemParameters.HighContrast);
            }));
        }

        private IntPtr WindowMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            if (keepLayered && message == 0x007C && wParam.ToInt64() == -20)
            {
                // WPF otherwise strips WS_EX_LAYERED from standard windows. Never
                // alter WS_EX_TOPMOST here: only SetWindowPos may manage its z-order.
                Marshal.WriteInt32(lParam, 4, Marshal.ReadInt32(lParam, 4) | 0x80000);
                handled = true;
            }
            // Defer until WPF has updated its cached accessibility/theme properties.
            if (message == 0x001A || message == 0x031A || message == 0x031E || message == 0x0320)
                QueueRefresh();
            return IntPtr.Zero;
        }

        private bool SynchronizeTopmost()
        {
            if (synchronizingTopmost || source == null || source.IsDisposed) return true;
            // Called only after a complete appearance transition/live disposal, never
            // from WindowMessage. All reentrant style/alpha calls have returned, so
            // SetWindowPos can safely commit the latest Pin intent to the actual z-order.
            synchronizingTopmost = true;
            try { return Succeeded("restore topmost", native.EnsureTopmost(source.Handle, window.Topmost)); }
            finally { synchronizingTopmost = false; }
        }

        internal void Refresh(bool highContrast)
        {
            if (disposed || source == null) return;
            WindowAppearance desired = WindowAppearance.Opaque;
            bool restored = ClearEffects();
            bool allowEffects = false;
            try
            {
                bool transparency, remote;
                if (restored && Succeeded("read appearance preferences", native.ReadPreferences(out transparency, out remote)))
                {
                    allowEffects = !highContrast && transparency && Preference != AppearancePreference.Solid;
                    Succeeded("dark title bar", native.SetAttribute(source.Handle, 20, highContrast ? 0 : 1), true);
                    if (allowEffects)
                    {
                        // Deliberate remote-session policy, not a claim that all RDP
                        // environments lack Acrylic. Prefer a predictable visible effect.
                        desired = Preference == AppearancePreference.Translucent ||
                                  (Preference == AppearancePreference.Auto && remote)
                            ? WindowAppearance.Translucent : TryAcrylic();
                    }
                }
            }
            catch (DllNotFoundException) { desired = allowEffects ? WindowAppearance.Translucent : WindowAppearance.Opaque; }
            catch (EntryPointNotFoundException) { desired = allowEffects ? WindowAppearance.Translucent : WindowAppearance.Opaque; }
            if (desired == WindowAppearance.Translucent && OpacityPercent == 100) desired = WindowAppearance.Opaque;
            if (desired != WindowAppearance.Acrylic && !ClearEffects()) desired = WindowAppearance.Opaque;
            ApplySurfaces(desired == WindowAppearance.Acrylic);
            if (desired == WindowAppearance.Translucent)
            {
                keepLayered = true;
                if (!Succeeded("set uniform alpha", native.SetOpacity(source.Handle, OpacityAlpha)))
                {
                    ClearEffects();
                    desired = WindowAppearance.Opaque;
                }
            }
            if (!SynchronizeTopmost())
            {
                ClearEffects();
                ApplySurfaces(false);
                desired = WindowAppearance.Opaque;
            }
            Mode = desired;
            if (Changed != null) Changed(this, EventArgs.Empty);
        }

        private WindowAppearance TryAcrylic()
        {
            bool composition;
            int result = native.IsCompositionEnabled(out composition);
            if (!Succeeded("DwmIsCompositionEnabled", result, true))
                return ExpectedUnsupported(result) ? WindowAppearance.Translucent : WindowAppearance.Opaque;
            if (!composition) return WindowAppearance.Translucent;
            // DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_TRANSIENTWINDOW (Windows 11 22621+).
            result = native.SetAttribute(source.Handle, 38, 3);
            if (!Succeeded("Desktop Acrylic", result, true))
                return ExpectedUnsupported(result) ? WindowAppearance.Translucent : WindowAppearance.Opaque;
            backdropSet = true;
            if (!Succeeded("extend glass frame", native.ExtendFrame(source.Handle, true)))
                return WindowAppearance.Opaque;
            frameExtended = true;
            return WindowAppearance.Acrylic;
        }

        private bool ClearEffects()
        {
            bool success = true;
            if (keepLayered)
            {
                keepLayered = false;
                success &= Succeeded("restore uniform alpha", native.SetOpacity(source.Handle, 255));
            }
            if (backdropSet)
            {
                int result = native.SetAttribute(source.Handle, 38, 1);
                success &= Succeeded("remove backdrop", result, true) || ExpectedUnsupported(result);
                backdropSet = false;
            }
            if (frameExtended)
            {
                int result = native.ExtendFrame(source.Handle, false);
                success &= Succeeded("restore frame", result, true) || ExpectedUnsupported(result);
                frameExtended = false;
            }
            return success;
        }

        private static bool ExpectedUnsupported(int result)
        {
            return result == unchecked((int)0x80070057) || result == unchecked((int)0x80004001) ||
                   result == unchecked((int)0x80263001);
        }

        private static bool Succeeded(string operation, int result, bool allowUnsupported = false)
        {
            if (result >= 0) return true;
            // Unsupported attributes and disabled composition are expected cosmetic fallbacks.
            if (!allowUnsupported || !ExpectedUnsupported(result))
                Console.Error.WriteLine("Window backdrop: {0} failed (HRESULT 0x{1:X8}).",
                                        operation, result);
            return false;
        }

        private void ApplySurfaces(bool enabled)
        {
            Color solid = Color.FromRgb(16, 21, 29);
            window.Background = enabled ? Brushes.Transparent : new SolidColorBrush(solid);
            source.CompositionTarget.BackgroundColor = enabled ? Colors.Transparent : solid;
            SetSurface("WindowSurface", enabled ? "#8010151D" : "#10151D");
            SetSurface("PanelSurface", enabled ? "#30161D28" : "#161D28");
            SetSurface("HeaderSurface", enabled ? "#80181F2B" : "#181F2B");
            SetSurface("RowSurface", enabled ? "#28161D28" : "#161D28");
            SetSurface("HoverSurface", enabled ? "#901C2838" : "#1C2838");
            SetSurface("SelectedSurface", enabled ? "#D022364A" : "#22364A");
            SetSurface("SurfaceBorder", enabled ? "#387E95B2" : "#2A3545");
            SetSurface("RowBorder", enabled ? "#207E95B2" : "#222D3C");
            SetSurface("BusySurface", enabled ? "#A017283B" : "#17283B");
            SetSurface("AttentionSurface", enabled ? "#A030291E" : "#30291E");
            SetSurface("DoneSurface", enabled ? "#A01A302D" : "#1A302D");
        }

        private void SetSurface(string key, string color)
        {
            var brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));
            brush.Freeze();
            window.Resources[key] = brush;
        }

        private void Closed(object sender, EventArgs args) { Dispose(true); }

        public void Dispose() { Dispose(false); }

        private void Dispose(bool closing)
        {
            if (disposed) return;
            disposed = true;
            SystemEvents.SessionSwitch -= SessionSwitch;
            window.SourceInitialized -= SourceInitialized;
            window.Closed -= Closed;
            if (source != null && !source.IsDisposed)
            {
                // Closed can run after HWND destruction but before HwndSource disposal.
                // The OS owns cleanup then; native positioning is only valid for a live disposal.
                if (!closing)
                {
                    ClearEffects();
                    SynchronizeTopmost();
                    ApplySurfaces(false);
                }
                source.RemoveHook(WindowMessage);
            }
            Mode = WindowAppearance.Opaque;
        }

        private sealed class DwmNative : IBackdropNative
        {
            private bool ownsLayered;
            [StructLayout(LayoutKind.Sequential)]
            private struct Margins { public int Left, Right, Top, Bottom; }

            [DllImport("dwmapi.dll")]
            private static extern int DwmIsCompositionEnabled([MarshalAs(UnmanagedType.Bool)] out bool enabled);
            [DllImport("dwmapi.dll")]
            private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);
            [DllImport("dwmapi.dll")]
            private static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd, ref Margins margins);
            [DllImport("user32.dll")]
            private static extern int GetSystemMetrics(int index);
            [DllImport("user32.dll")]
            private static extern IntPtr GetWindow(IntPtr hwnd, uint command);
            [DllImport("user32.dll", EntryPoint = "GetWindowLongW", SetLastError = true)]
            private static extern int GetWindowLong(IntPtr hwnd, int index);
            [DllImport("user32.dll", EntryPoint = "SetWindowLongW", SetLastError = true)]
            private static extern int SetWindowLong(IntPtr hwnd, int index, int value);
            [DllImport("user32.dll", SetLastError = true)]
            private static extern bool SetLayeredWindowAttributes(IntPtr hwnd, uint color, byte alpha, uint flags);
            [DllImport("user32.dll", SetLastError = true)]
            private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
            [DllImport("kernel32.dll")]
            private static extern void SetLastError(uint error);

            public int ReadPreferences(out bool transparency, out bool remote)
            {
                transparency = false;
                remote = GetSystemMetrics(0x1000) != 0;
                try
                {
                    using (RegistryKey key = Registry.CurrentUser.OpenSubKey(
                        @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
                    {
                        object value = key == null ? 1 : key.GetValue("EnableTransparency", 1);
                        transparency = value is int && (int)value != 0;
                    }
                    return 0;
                }
                catch (IOException error) { return Marshal.GetHRForException(error); }
                catch (UnauthorizedAccessException error) { return Marshal.GetHRForException(error); }
                catch (SecurityException error) { return Marshal.GetHRForException(error); }
            }

            private static int Win32Failure()
            {
                int error = Marshal.GetLastWin32Error();
                return error == 0 ? unchecked((int)0x80004005) : unchecked((int)0x80070000) | (error & 0xFFFF);
            }

            public int EnsureTopmost(IntPtr hwnd, bool topmost)
            {
                // A matching style bit does not prove the actual z-order is correct
                // after a reentrant extended-style update. Commit the current intent.
                IntPtr band = new IntPtr(topmost ? -1 : -2);
                IntPtr after = IntPtr.Zero;
                if (topmost)
                {
                    // Stay below an existing topmost predecessor (notably an open
                    // ContextMenu). HWND_TOPMOST would raise the owner above its popup
                    // on every live opacity adjustment.
                    IntPtr previous = GetWindow(hwnd, 3); // GW_HWNDPREV
                    if (previous != IntPtr.Zero && (GetWindowLong(previous, -20) & 8) != 0)
                        after = previous;
                }
                // Preserve position, size, activation, and owner z-order.
                // First establish the band: inserting an unpinned window after the
                // lowest topmost predecessor alone does not necessarily promote it.
                if (!SetWindowPos(hwnd, band, 0, 0, 0, 0, 0x213)) return Win32Failure();
                if (after != IntPtr.Zero && (GetWindowLong(after, -20) & 8) != 0)
                {
                    if (!SetWindowPos(hwnd, after, 0, 0, 0, 0, 0x213) || (GetWindowLong(hwnd, -20) & 8) == 0)
                    {
                        // The predecessor can disappear or change band concurrently.
                        // Recover the requested band without depending on a foreign HWND.
                        if (!SetWindowPos(hwnd, band, 0, 0, 0, 0, 0x213)) return Win32Failure();
                    }
                }
                return 0;
            }

            public int SetOpacity(IntPtr hwnd, byte alpha)
            {
                SetLastError(0);
                int style = GetWindowLong(hwnd, -20);
                if (style == 0 && Marshal.GetLastWin32Error() != 0) return Win32Failure();
                if (alpha < 255 && (style & 0x80000) == 0)
                {
                    SetLastError(0);
                    if (SetWindowLong(hwnd, -20, style | 0x80000) == 0 && Marshal.GetLastWin32Error() != 0)
                        return Win32Failure();
                    ownsLayered = true;
                }
                int result = 0;
                if (alpha < 255 || ownsLayered)
                    if (!SetLayeredWindowAttributes(hwnd, 0, alpha, 2)) result = Win32Failure();
                if (alpha == 255 && ownsLayered)
                {
                    // Read the current style, not a startup snapshot: pinning and other
                    // standard-window changes must survive opacity restoration.
                    SetLastError(0);
                    style = GetWindowLong(hwnd, -20);
                    if (style == 0 && Marshal.GetLastWin32Error() != 0) return Win32Failure();
                    SetLastError(0);
                    if (SetWindowLong(hwnd, -20, style & ~0x80000) == 0 && Marshal.GetLastWin32Error() != 0)
                        return Win32Failure();
                    ownsLayered = false;
                }
                return result;
            }

            public int IsCompositionEnabled(out bool enabled) { return DwmIsCompositionEnabled(out enabled); }
            public int SetAttribute(IntPtr hwnd, int attribute, int value)
            {
                return DwmSetWindowAttribute(hwnd, attribute, ref value, sizeof(int));
            }
            public int ExtendFrame(IntPtr hwnd, bool enabled)
            {
                // -1 extends the native glass sheet across the client without replacing the standard frame.
                int margin = enabled ? -1 : 0;
                var margins = new Margins { Left = margin, Right = margin, Top = margin, Bottom = margin };
                return DwmExtendFrameIntoClientArea(hwnd, ref margins);
            }
        }
    }
}
