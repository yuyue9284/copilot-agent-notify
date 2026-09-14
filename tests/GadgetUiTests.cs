using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Threading;
using CopilotSessions;

public static class GadgetUiTests
{
    private static void Check(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private static void Pump()
    {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle,
            new Action(delegate { frame.Continue = false; }));
        Dispatcher.PushFrame(frame);
    }

    private static IEnumerable<T> Descendants<T>(DependencyObject root) where T : DependencyObject
    {
        for (int index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
        {
            DependencyObject child = VisualTreeHelper.GetChild(root, index);
            if (child is T) yield return (T)child;
            foreach (T descendant in Descendants<T>(child)) yield return descendant;
        }
    }

    private static void ClickHeader(SessionWindow app, DataGridColumn column)
    {
        var presenter = Descendants<DataGridColumnHeadersPresenter>(app.Grid).Single();
        var presenterPeer = UIElementAutomationPeer.CreatePeerForElement(presenter);
        var peer = presenterPeer.GetChildren().Single(item => item.GetName() == column.Header.ToString());
        var invoke = peer.GetPattern(PatternInterface.Invoke) as IInvokeProvider;
        Check(invoke != null, "Column header has no Invoke pattern: " + column.Header + ", sortable=" + column.CanUserSort);
        invoke.Invoke();
        Pump();
    }

    private static Snapshot Sample()
    {
        return new Snapshot { errors = new string[0], rows = new[] {
            new SessionData { id = "9d241ac8-session", title = "Polish the session monitor",
                source = "WSL:Ubuntu", cwd = "/home/dev/projects/session-monitor", status = "In progress", pid = 1042 },
            new SessionData { id = "b42718de-session", title = "Review API changes",
                source = "Windows", cwd = @"C:\work\api-service", status = "Needs input", pid = 2184 },
            new SessionData { id = "3ad580fc-session", title = "Update project documentation",
                source = "WSL:Debian", cwd = "/home/dev/projects/docs", status = "Done", pid = 3908 }
        }};
    }

    private static void Screenshot(SessionWindow app, string path)
    {
        Pump();
        var bitmap = new RenderTargetBitmap((int)app.Window.ActualWidth, (int)app.Window.ActualHeight,
                                             96, 96, PixelFormats.Pbgra32);
        bitmap.Render(app.Window);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));
        using (Stream output = File.Create(path)) encoder.Save(output);
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);
    [DllImport("dwmapi.dll")]
    private static extern int DwmIsCompositionEnabled([MarshalAs(UnmanagedType.Bool)] out bool enabled);
    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW")]
    private static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetLayeredWindowAttributes(IntPtr hwnd, out uint color, out byte alpha, out uint flags);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll")]
    private static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll")]
    private static extern IntPtr GetFocus();
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint process);
    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentProcessId();
    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")]
    private static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostMessage(IntPtr hwnd, int message, IntPtr wparam, IntPtr lparam);

    private sealed class BackdropNative : IBackdropNative
    {
        internal int AttributeResult, FrameResult, OpacityResult, PreferenceResult, Calls, BackdropValue;
        internal bool Composition = true, Transparency = true, Remote, Extended;
        internal byte Alpha = 255;
        public int EnsureTopmost(IntPtr hwnd, bool topmost) { Calls++; return 0; }
        public int ReadPreferences(out bool transparency, out bool remote)
        {
            Calls++; transparency = Transparency; remote = Remote; return PreferenceResult;
        }
        public int SetOpacity(IntPtr hwnd, byte alpha)
        {
            Calls++;
            if (alpha != 255 && OpacityResult != 0) return OpacityResult;
            Alpha = alpha;
            return 0;
        }
        public int IsCompositionEnabled(out bool enabled) { Calls++; enabled = Composition; return 0; }
        public int SetAttribute(IntPtr hwnd, int attribute, int value)
        {
            Calls++;
            if (attribute == 38) { BackdropValue = value; return AttributeResult; }
            return 0;
        }
        public int ExtendFrame(IntPtr hwnd, bool enabled)
        {
            Calls++;
            Extended = enabled;
            return FrameResult;
        }
    }

    private static void CheckOpaqueContent(SessionWindow app)
    {
        // WPF content remains opaque; Translucent mode applies uniform native alpha
        // after composition, deliberately including text and the standard title bar.
        Check(app.Window.Opacity == 1 && !app.Window.AllowsTransparency, "Do not use per-pixel WPF transparency");
        Check(app.Window.WindowStyle == WindowStyle.SingleBorderWindow &&
              app.Window.ResizeMode == ResizeMode.CanResize, "Keep the standard resizable frame");
        foreach (UIElement element in Descendants<Control>(app.Window).Cast<UIElement>()
            .Concat(Descendants<TextBlock>(app.Window)))
        {
            Check(element.Opacity == 1, "Glass must not lower control/text opacity: " + element.GetType().Name +
                  "/" + (element is FrameworkElement ? ((FrameworkElement)element).Name : "") + "=" + element.Opacity);
            for (DependencyObject parent = VisualTreeHelper.GetParent(element); parent != null;
                 parent = VisualTreeHelper.GetParent(parent))
                if (parent is UIElement)
                    Check(((UIElement)parent).Opacity == 1, "A container fades its content");
        }
        foreach (Brush brush in Descendants<TextBlock>(app.Window).Select(text => text.Foreground)
            .Concat(Descendants<Control>(app.Window).Select(control => control.Foreground)))
        {
            var solid = brush as SolidColorBrush;
            Check(solid != null && solid.Color.A == 255 && solid.Opacity == 1, "Text foreground must be opaque");
        }
        foreach (SessionRow row in app.Rows)
            Check(((Color)ColorConverter.ConvertFromString(row.StatusForeground)).A == 255,
                  "Status color must remain opaque");
    }

    private static void CheckSolid(Window window)
    {
        Check(((SolidColorBrush)window.Background).Color == Color.FromRgb(16, 21, 29),
              "Fallback must restore the existing dark window");
        foreach (string key in new[] { "WindowSurface", "PanelSurface", "RowSurface", "HeaderSurface",
                                       "HoverSurface", "SelectedSurface", "BusySurface", "AttentionSurface", "DoneSurface" })
            Check(((SolidColorBrush)window.Resources[key]).Color.A == 255, "Fallback surface is translucent: " + key);
        var source = HwndSource.FromHwnd(new WindowInteropHelper(window).Handle);
        Check(source.CompositionTarget.BackgroundColor.A == 255, "Fallback compositor is transparent");
    }

    private static void CheckNativeAppearance(SessionWindow app)
    {
        IntPtr hwnd = new WindowInteropHelper(app.Window).Handle;
        int style = GetWindowLong(hwnd, -20);
        Check((style & 0x20) == 0, "Appearance must not introduce click-through");
        if (app.Backdrop.Mode == WindowAppearance.Translucent)
        {
            uint color, flags;
            byte alpha;
            Check((style & 0x80000) != 0, "Uniform-alpha fallback lost its native layered style");
            Check(GetLayeredWindowAttributes(hwnd, out color, out alpha, out flags) &&
                  alpha == app.Backdrop.OpacityAlpha && flags == 2, "Incorrect native fallback alpha");
        }
        else Check((style & 0x80000) == 0, "Opaque/Acrylic modes must restore normal composition");
    }

    private static void CheckPopupAppearance(SessionWindow app)
    {
        byte expected = app.Backdrop.Mode == WindowAppearance.Translucent
            ? app.Backdrop.OpacityAlpha
            : app.Backdrop.Mode == WindowAppearance.Acrylic ? WindowBackdrop.FallbackAlpha : (byte)255;
        var menu = ((Button)app.Window.FindName("AppearanceButton")).ContextMenu;
        var surface = (SolidColorBrush)menu.Resources["PopupSurface"];
        Check(surface.Color.A == expected, "Settings popup does not follow the effective appearance opacity");
        Check(((SolidColorBrush)menu.Background).Color == surface.Color,
              "Settings popup background is not using the shared popup surface");
        Check(((SolidColorBrush)menu.Resources["PopupBorder"]).Color.A >= surface.Color.A,
              "Popup border is less visible than its surface");
    }

    private static void CheckActualTopmost(SessionWindow app, Window witness, bool pinned)
    {
        IntPtr gadget = new WindowInteropHelper(app.Window).Handle;
        IntPtr normal = new WindowInteropHelper(witness).Handle;
        Check(SetWindowPos(normal, IntPtr.Zero, 0, 0, 0, 0, 0x213),
              "Could not raise the independent normal witness: " + Marshal.GetLastWin32Error());
        Check((GetWindowLong(normal, -20) & 8) == 0, "Witness must remain a normal, non-topmost window");
        IntPtr above = pinned ? gadget : normal;
        IntPtr below = pinned ? normal : gadget;
        for (int count = 0; count < 1000 && below != IntPtr.Zero; count++)
        {
            below = GetWindow(below, 3); // GW_HWNDPREV: actual z-order, not a style-bit proxy.
            if (below == above) return;
        }
        throw new InvalidOperationException("Actual z-order did not match pin=" + pinned +
            "; gadget style=0x" + GetWindowLong(gadget, -20).ToString("X8"));
    }

    private static void TestBackdrop(SessionWindow app)
    {
        CheckOpaqueContent(app);
        CheckNativeAppearance(app);
        WindowAppearance original = app.Backdrop.Mode;
        bool enabled = app.Backdrop.IsEnabled;
        int value;
        int result = DwmGetWindowAttribute(new WindowInteropHelper(app.Window).Handle, 38, out value, sizeof(int));
        bool composition;
        object preference = Microsoft.Win32.Registry.GetValue(
            @"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency", 1);
        bool effects = !SystemParameters.HighContrast && (preference == null || (preference is int && (int)preference != 0));
        bool remote = GetSystemMetrics(0x1000) != 0;
        if (effects && remote)
            Check(original == WindowAppearance.Translucent, "Remote-session policy must select the visible fallback");
        else if (effects && result == 0 && DwmIsCompositionEnabled(out composition) == 0 && composition)
            Check(enabled, "Supported native Desktop Acrylic must be enabled");
        else if (!effects) Check(original == WindowAppearance.Opaque, "User preference must disable all transparency");
        Console.WriteLine("Native backdrop: HRESULT=0x{0:X8}, type={1}, mode={2}, highContrast={3}, remote={4}",
                          result, value, original, SystemParameters.HighContrast, remote);
        if (enabled)
        {
            Check(result == 0 && value == 3, "Desktop Acrylic attribute is not enabled");
            Check(((SolidColorBrush)app.Window.Background).Color.A == 0, "WPF window blocks the backdrop");
            Check(HwndSource.FromHwnd(new WindowInteropHelper(app.Window).Handle)
                  .CompositionTarget.BackgroundColor.A == 0, "WPF compositor blocks the backdrop");
            Check(((SolidColorBrush)app.Window.Resources["PanelSurface"]).Color.A < 255,
                  "Opaque panel blocks the backdrop");
        }
        else CheckSolid(app.Window);
        app.Backdrop.Refresh(true);
        Check(app.Backdrop.Mode == WindowAppearance.Opaque, "High contrast must disable all effects");
        CheckSolid(app.Window);
        CheckOpaqueContent(app);
        CheckNativeAppearance(app);
        // Exercise the same deferred settings/composition path used by Windows, without changing any system settings.
        foreach (int message in new[] { 0x001A, 0x031A, 0x031E })
            SendMessage(new WindowInteropHelper(app.Window).Handle, message, IntPtr.Zero, IntPtr.Zero);
        Pump();
        Check(app.Backdrop.Mode == original, "Settings notification did not restore the appropriate appearance");
        CheckNativeAppearance(app);
        var pin = (ToggleButton)app.Window.FindName("Pin");
        var witness = new Window { Title = "Synthetic z-order witness", Width = 120, Height = 70,
            Left = app.Window.Left + 30, Top = app.Window.Top + 70, WindowStartupLocation = WindowStartupLocation.Manual,
            ShowActivated = false, ShowInTaskbar = false, Content = new TextBlock { Text = "Z-order test" } };
        witness.Show();
        Pump();
        try
        {
        IntPtr gadgetHandle = new WindowInteropHelper(app.Window).Handle;
        IntPtr proposedStyle = Marshal.AllocHGlobal(8);
        try
        {
            int currentStyle = GetWindowLong(gadgetHandle, -20);
            int requestedStyle = currentStyle ^ 8;
            Marshal.WriteInt32(proposedStyle, 0, currentStyle);
            Marshal.WriteInt32(proposedStyle, 4, requestedStyle);
            // Preflight only: do not apply this word to the actual window.
            SendMessage(gadgetHandle, 0x007C, new IntPtr(-20), proposedStyle);
            int returnedStyle = Marshal.ReadInt32(proposedStyle, 4);
            Check(((returnedStyle ^ requestedStyle) & ~0x80000) == 0, String.Format(
                "Style hook corrupted a Win32 topmost request: requested=0x{0:X8}, returned=0x{1:X8}",
                requestedStyle, returnedStyle));
        }
        finally { Marshal.FreeHGlobal(proposedStyle); }
        // A genuine native z-order operation must not be vetoed by rewriting its
        // STYLESTRUCT from a managed property that may not yet reflect the operation.
        bool nativePin = SetWindowPos(gadgetHandle, new IntPtr(-1), 0, 0, 0, 0, 0x213);
        int nativePinError = Marshal.GetLastWin32Error();
        Check(nativePin, "Genuine SetWindowPos(HWND_TOPMOST) was rejected: " + nativePinError);
        CheckActualTopmost(app, witness, true);
        Check(SetWindowPos(gadgetHandle, new IntPtr(-2), 0, 0, 0, 0, 0x213), "Genuine native unpin was rejected");
        CheckActualTopmost(app, witness, false);
        for (int cycle = 0; cycle < 30; cycle++)
        {
            if (cycle % 3 == 0)
                SendMessage(new WindowInteropHelper(app.Window).Handle, 0x001A, IntPtr.Zero, IntPtr.Zero);
            pin.IsChecked = true;
            int beforeReset = GetWindowLong(new WindowInteropHelper(app.Window).Handle, -20);
            Check(app.Window.Topmost && (beforeReset & 8) != 0, String.Format(
                "Pin did not immediately update native topmost: cycle={0}, managed={1}, pin={2}, style=0x{3:X8}",
                cycle, app.Window.Topmost, pin.IsChecked, beforeReset));
            CheckActualTopmost(app, witness, true);
            app.Backdrop.Refresh(true);
            int afterReset = GetWindowLong(new WindowInteropHelper(app.Window).Handle, -20);
            Check((afterReset & 8) != 0, String.Format(
                "Opacity reset lost topmost: before=0x{0:X8}, after=0x{1:X8}, managed={2}, pin={3}",
                beforeReset, afterReset, app.Window.Topmost, pin.IsChecked));
            CheckActualTopmost(app, witness, true);
            CheckNativeAppearance(app);
            app.Backdrop.Refresh(SystemParameters.HighContrast);
            Check((GetWindowLong(new WindowInteropHelper(app.Window).Handle, -20) & 8) != 0, "Opacity enable lost topmost");
            CheckActualTopmost(app, witness, true);
            CheckNativeAppearance(app);
            pin.IsChecked = false;
            Check((GetWindowLong(new WindowInteropHelper(app.Window).Handle, -20) & 8) == 0, "Unpin did not immediately clear native topmost");
            CheckActualTopmost(app, witness, false);
            Pump();
            CheckNativeAppearance(app);
            Check((GetWindowLong(new WindowInteropHelper(app.Window).Handle, -20) & 8) == 0, "Appearance prevents unpinning");
        }
        if (original == WindowAppearance.Translucent)
        {
            // Exercise a real Win32 reentrancy boundary, not a timer-dependent race:
            // Pin changes after SetWindowLong has prepared its older style value.
            bool changePin = true;
            bool pinValue = true;
            var source = HwndSource.FromHwnd(new WindowInteropHelper(app.Window).Handle);
            HwndSourceHook reentrantPin = delegate(IntPtr hwnd, int message, IntPtr wp, IntPtr lp, ref bool handled)
            {
                if (changePin && message == 0x007C && wp.ToInt64() == -20)
                {
                    changePin = false;
                    pin.IsChecked = pinValue;
                }
                return IntPtr.Zero;
            };
            source.AddHook(reentrantPin);
            try
            {
                app.Backdrop.Refresh(true);
                Check(!changePin && app.Window.Topmost, "Reentrant pin fixture did not run");
                Check((GetWindowLong(source.Handle, -20) & 8) != 0, "In-flight opacity reset overwrote a new pin");
                CheckActualTopmost(app, witness, true);
                pinValue = false;
                changePin = true;
                app.Backdrop.Refresh(SystemParameters.HighContrast);
                Check(!changePin && !app.Window.Topmost, "Reentrant unpin fixture did not run");
                Check((GetWindowLong(source.Handle, -20) & 8) == 0, "In-flight opacity enable resurrected an old pin");
                CheckActualTopmost(app, witness, false);
                CheckNativeAppearance(app);
            }
            finally { source.RemoveHook(reentrantPin); pin.IsChecked = false; }
        }
        foreach (AppearancePreference requested in Enum.GetValues(typeof(AppearancePreference)))
        {
            pin.IsChecked = true;
            app.Backdrop.Configure(requested, 90);
            CheckNativeAppearance(app);
            CheckActualTopmost(app, witness, true);
            pin.IsChecked = false;
            CheckActualTopmost(app, witness, false);
        }
        pin.IsChecked = true;
        foreach (int opacity in new[] { 75, 100, 90 })
        {
            app.Backdrop.Configure(AppearancePreference.Translucent, opacity);
            CheckNativeAppearance(app);
            CheckActualTopmost(app, witness, true);
        }
        pin.IsChecked = false;
        app.Backdrop.Configure(app.Appearance, app.OpacityPercent);
        CheckActualTopmost(app, witness, false);
        }
        finally { witness.Close(); }
        Console.WriteLine("Native topmost: 30 immediate pin/appearance cycles and independent normal-window z-order passed; reentrant={0}.",
                          original == WindowAppearance.Translucent);
        int chrome = GetWindowLong(new WindowInteropHelper(app.Window).Handle, -16);
        Check((chrome & 0x00CF0000) == 0x00CF0000, "Native resize/caption/minimize/maximize/system-menu styles changed");
        foreach (WindowState state in new[] { WindowState.Maximized, WindowState.Normal, WindowState.Minimized, WindowState.Normal })
        {
            app.Window.WindowState = state;
            Pump();
            Check(app.Window.WindowState == state, "Standard window state transition failed: requested=" +
                  state + ", actual=" + app.Window.WindowState);
            CheckNativeAppearance(app);
        }
        Point caption = app.Window.PointToScreen(new Point(80, -12));
        long hitPoint = ((int)caption.X & 0xFFFF) | (((int)caption.Y & 0xFFFF) << 16);
        Check(SendMessage(new WindowInteropHelper(app.Window).Handle, 0x0084, IntPtr.Zero, new IntPtr(hitPoint)).ToInt32() == 2,
              "Standard title bar no longer supplies native dragging");

        var native = new BackdropNative { AttributeResult = unchecked((int)0x80070057) };
        var window = new Window { Width = 100, Height = 100, ShowInTaskbar = false };
        var backdrop = new WindowBackdrop(window, native);
        TextWriter previous = Console.Error;
        var diagnostics = new StringWriter();
        try
        {
            Console.SetError(diagnostics);
            new WindowInteropHelper(window).EnsureHandle();
            Check(backdrop.Mode == WindowAppearance.Translucent && native.Alpha == 230 && !native.Extended,
                  "Unsupported Acrylic must choose plain translucency without extending the frame");
            CheckSolid(window);
            Check(diagnostics.ToString() == "", "Unsupported cosmetic attribute emitted a failure diagnostic");
            native.AttributeResult = 0;
            native.Transparency = false;
            backdrop.Refresh(false);
            Check(backdrop.Mode == WindowAppearance.Opaque && native.Alpha == 255, "Disabled effects must restore opaque alpha");
            CheckSolid(window);
            native.Transparency = true;
            native.Remote = true;
            backdrop.Refresh(false);
            Check(backdrop.Mode == WindowAppearance.Translucent && native.Alpha == 230, "Remote fallback not selected");
            backdrop.Refresh(true);
            Check(backdrop.Mode == WindowAppearance.Opaque && native.Alpha == 255, "High contrast must win over remote policy");
            native.OpacityResult = unchecked((int)0x80070057);
            backdrop.Refresh(false);
            Check(backdrop.Mode == WindowAppearance.Opaque && native.Alpha == 255, "Alpha API failure must stay opaque");
            Check(diagnostics.ToString().Contains("set uniform alpha") && diagnostics.ToString().Contains("0x80070057"),
                  "An invalid alpha call is a real failure, not unsupported Acrylic");
            native.OpacityResult = 0;
            native.PreferenceResult = unchecked((int)0x80070005);
            backdrop.Refresh(false);
            Check(backdrop.Mode == WindowAppearance.Opaque, "Unreadable user preference must fail closed");
            native.PreferenceResult = 0;
            native.Remote = false;
            native.AttributeResult = unchecked((int)0x80004005);
            backdrop.Refresh(false);
            Check(backdrop.Mode == WindowAppearance.Opaque, "Unexpected Acrylic error must not silently select translucency");
            native.AttributeResult = 0;
            native.FrameResult = unchecked((int)0x80004005);
            backdrop.Refresh(false);
            Check(!backdrop.IsEnabled && native.BackdropValue == 1, "Failed extension must roll back the backdrop");
            CheckSolid(window);
            Check(diagnostics.ToString().Contains("0x80004005"), "Unexpected HRESULT needs a scoped diagnostic");
            native.FrameResult = 0;
            backdrop.Refresh(false);
            Check(backdrop.IsEnabled, "Backdrop did not recover after native failure");
            Check(native.Alpha == 255, "Acrylic must restore fully opaque native alpha");
            native.Composition = false;
            backdrop.Refresh(false);
            Check(backdrop.Mode == WindowAppearance.Translucent && !native.Extended, "Composition loss must use unblurred fallback");
            CheckSolid(window);
            native.Composition = true;
            backdrop.Refresh(false);
            Check(backdrop.IsEnabled, "Composition recovery failed");
            foreach (AppearancePreference requested in Enum.GetValues(typeof(AppearancePreference)))
            {
                native.Transparency = false;
                backdrop.Configure(requested, 75);
                Check(backdrop.Mode == WindowAppearance.Opaque && backdrop.Preference == requested && native.Alpha == 255,
                      "Windows transparency preference must override every requested appearance");
                native.Transparency = true;
                backdrop.Configure(requested, 75);
                backdrop.Refresh(true);
                Check(backdrop.Mode == WindowAppearance.Opaque && backdrop.Preference == requested && native.Alpha == 255,
                      "High contrast must override every requested appearance");
            }
            backdrop.Configure(AppearancePreference.Auto, 90);
            native.Remote = true;
            backdrop.Refresh(false);
            SendMessage(new WindowInteropHelper(window).Handle, 0x031E, IntPtr.Zero, IntPtr.Zero);
            backdrop.Dispose();
            Check(native.Alpha == 255 && backdrop.Mode == WindowAppearance.Opaque, "Dispose must restore normal composition");
            CheckSolid(window);
            SendMessage(new WindowInteropHelper(window).Handle, 0x031E, IntPtr.Zero, IntPtr.Zero);
            window.Close();
            int calls = native.Calls;
            Pump();
            backdrop.Refresh(false);
            Check(native.Calls == calls, "Closed window retained backdrop callbacks");
        }
        finally
        {
            Console.SetError(previous);
            window.Close();
            backdrop.Dispose();
        }
        var disposableWindow = new Window { Width = 100, Height = 100, ShowInTaskbar = false };
        var disposableBackdrop = new WindowBackdrop(disposableWindow);
        try
        {
            IntPtr handle = new WindowInteropHelper(disposableWindow).EnsureHandle();
            disposableBackdrop.Dispose();
            Check((GetWindowLong(handle, -20) & 0x80000) == 0, "Disposing a live window left native alpha enabled");
            CheckSolid(disposableWindow);
        }
        finally { disposableBackdrop.Dispose(); disposableWindow.Close(); }
        var closingNative = new BackdropNative();
        var closingWindow = new Window { Width = 100, Height = 100, ShowInTaskbar = false };
        var closingBackdrop = new WindowBackdrop(closingWindow, closingNative);
        try
        {
            new WindowInteropHelper(closingWindow).EnsureHandle();
            int calls = closingNative.Calls;
            closingWindow.Close();
            Pump();
            Check(closingNative.Calls == calls, "Closed attempted native cleanup/positioning on a dying HWND");
        }
        finally { closingWindow.Close(); closingBackdrop.Dispose(); }
    }

    private sealed class SyntheticBackdrop : FrameworkElement
    {
        internal bool Alternate;
        protected override void OnRender(DrawingContext drawing)
        {
            // Only generated colors/patterns, never another application or desktop content.
            for (int y = 0; y < ActualHeight; y += 12)
                for (int x = 0; x < ActualWidth; x += 12)
                {
                    bool blue = (x >= ActualWidth / 2) != Alternate;
                    bool light = (x / 12 + y / 12) % 2 == 0;
                    Color color = blue
                        ? (light ? Color.FromRgb(55, 182, 238) : Color.FromRgb(20, 81, 141))
                        : (light ? Color.FromRgb(248, 179, 94) : Color.FromRgb(159, 54, 67));
                    drawing.DrawRectangle(new SolidColorBrush(color), null, new Rect(x, y, 12, 12));
                }
        }
    }

    private static void Invoke(Control control)
    {
        var peer = UIElementAutomationPeer.CreatePeerForElement(control);
        var invoke = peer.GetPattern(PatternInterface.Invoke) as IInvokeProvider;
        Check(invoke != null, "Missing Invoke pattern: " + control.Name);
        invoke.Invoke();
        Pump();
    }

    private static void ChooseAppearance(SessionWindow app, string name)
    {
        var button = (Button)app.Window.FindName("AppearanceButton");
        if (!button.ContextMenu.IsOpen) Invoke(button);
        var choice = (MenuItem)app.Window.FindName("Appearance" + name);
        Invoke(choice);
        Pump();
        Check(choice.IsChecked, "Appearance choice was not checked: " + name);
        Check(button.ContextMenu.Items.OfType<MenuItem>().Count(item => item.IsChecked) == 1,
              "Appearance choices must be mutually exclusive");
    }

    private static void SetOpacity(SessionWindow app, int percent)
    {
        var button = (Button)app.Window.FindName("AppearanceButton");
        if (!button.ContextMenu.IsOpen) Invoke(button);
        var slider = (Slider)app.Window.FindName("AppearanceOpacity");
        var range = (IRangeValueProvider)UIElementAutomationPeer.CreatePeerForElement(slider).GetPattern(PatternInterface.RangeValue);
        range.SetValue(percent);
        Pump();
        Check(slider.Value == percent && app.OpacityPercent == percent, "Opacity slider did not apply its integer value");
    }

    private static void SetInterfaceFont(SessionWindow app, string family, int size)
    {
        var button = (Button)app.Window.FindName("AppearanceButton");
        if (!button.ContextMenu.IsOpen) Invoke(button);
        var families = (ComboBox)app.Window.FindName("InterfaceFontFamily");
        var sizes = (Slider)app.Window.FindName("InterfaceFontSize");
        families.SelectedItem = family;
        Pump();
        var range = (IRangeValueProvider)UIElementAutomationPeer.CreatePeerForElement(sizes)
            .GetPattern(PatternInterface.RangeValue);
        range.SetValue(size);
        Pump();
        Check(app.InterfaceFontFamily == family && app.InterfaceFontSize == size,
              "Font controls did not apply the selected family and size");
    }

    private static void Press(UIElement target, Key key)
    {
        var menu = target as ContextMenu;
        if (menu != null && !menu.IsOpen && key == Key.Escape) return;
        var args = new KeyEventArgs(Keyboard.PrimaryDevice, PresentationSource.FromVisual(target),
            Environment.TickCount, key) { RoutedEvent = Keyboard.PreviewKeyDownEvent };
        target.RaiseEvent(args);
        if (!args.Handled)
        {
            args.RoutedEvent = Keyboard.KeyDownEvent;
            target.RaiseEvent(args);
        }
        Pump();
    }

    private static string AppearanceFocusState(SessionWindow app)
    {
        var menu = ((Button)app.Window.FindName("AppearanceButton")).ContextMenu;
        var slider = (Slider)app.Window.FindName("AppearanceOpacity");
        var fontFamily = (ComboBox)app.Window.FindName("InterfaceFontFamily");
        var fontSize = (Slider)app.Window.FindName("InterfaceFontSize");
        var focus = Keyboard.FocusedElement as FrameworkElement;
        var hover = Mouse.DirectlyOver as FrameworkElement;
        var popup = PresentationSource.FromVisual(menu) as HwndSource;
        IntPtr foreground = GetForegroundWindow();
        uint process;
        GetWindowThreadProcessId(foreground, out process);
        return String.Format("focus={0}/{1}; hover={2}/{3}; active={4}; menuOpen={5}; popupSource={6}; popupForeground={7}; sliderFocus={8}; fontFamilyFocus={9}; fontSizeFocus={10}; rootForeground={11}; nativeFocusRoot={12}; nativeFocusPopup={13}; foregroundOtherProcess={14}; foregroundZero={15}",
            focus == null ? "null" : focus.GetType().Name, focus == null ? "" : focus.Name,
            hover == null ? "null" : hover.GetType().Name, hover == null ? "" : hover.Name,
            app.Window.IsActive, menu.IsOpen, popup != null,
            popup != null && GetForegroundWindow() == popup.Handle, slider.IsKeyboardFocusWithin,
            fontFamily.IsKeyboardFocusWithin, fontSize.IsKeyboardFocusWithin,
            GetForegroundWindow() == new WindowInteropHelper(app.Window).Handle,
            GetFocus() == new WindowInteropHelper(app.Window).Handle, popup != null && GetFocus() == popup.Handle,
            foreground != IntPtr.Zero && process != GetCurrentProcessId(), foreground == IntPtr.Zero);
    }

    private static void RequireKeyboardFixture(SessionWindow app)
    {
        IntPtr desktop = OpenInputDesktop(0, false, 1); // DESKTOP_READOBJECTS; no desktop/session changes.
        int error = Marshal.GetLastWin32Error();
        Check(desktop != IntPtr.Zero, "Native keyboard tests require an active, unlocked input desktop. " +
              "OpenInputDesktop failed (" + error + "); reconnect/unlock the test session. No keyboard assertions were skipped.");
        CloseDesktop(desktop);
        IntPtr handle = new WindowInteropHelper(app.Window).Handle;
        Check(GetForegroundWindow() == handle || app.Window.Activate(),
              "Could not activate the owned keyboard-test window; do not infer a menu failure. " + AppearanceFocusState(app));
        Pump();
        Check(GetForegroundWindow() == handle, "Keyboard fixture is not foreground. " + AppearanceFocusState(app));
    }

    private static void TestAppearance(SessionWindow app, string preferences)
    {
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
        app.Apply(Sample());
        app.Grid.SelectedItem = app.Rows.Single(row => row.Status == "In progress");
        Snapshot done = Sample();
        done.rows[0].status = "Done";
        app.Apply(done);
        SessionRow selected = (SessionRow)app.Grid.SelectedItem;
        int unread = app.UnreadCount;
        Check(unread == 1, "Appearance fixture needs a selected unread completion");
        string forgottenPath = Path.Combine(Path.GetDirectoryName(preferences), "gadget-forgotten.json");
        const string forgotten = "{\"Windows/appearance-sentinel\":\"00000000000000000001/sentinel\"}";
        File.WriteAllText(forgottenPath, forgotten);
        var button = (Button)app.Window.FindName("AppearanceButton");
        var slider = (Slider)app.Window.FindName("AppearanceOpacity");
        var fontFamily = (ComboBox)app.Window.FindName("InterfaceFontFamily");
        var fontSize = (Slider)app.Window.FindName("InterfaceFontSize");
        string alternateFont = fontFamily.Items.Cast<string>().First(
            name => !String.Equals(name, SessionWindow.DefaultInterfaceFontFamily,
                                   StringComparison.CurrentCultureIgnoreCase));
        Check(UIElementAutomationPeer.CreatePeerForElement(button).GetName() == "Appearance",
              "Appearance button has no discoverable automation name");
        Check(UIElementAutomationPeer.CreatePeerForElement(fontFamily).GetName() == "Interface font family" &&
              UIElementAutomationPeer.CreatePeerForElement(fontSize).GetName() == "Interface font size",
              "Font controls have no discoverable automation names");
        Check(fontFamily.Items.Count > 1 && fontFamily.SelectedItem.ToString() == SessionWindow.DefaultInterfaceFontFamily,
              "Installed-font dropdown did not initialize to the default");
        Check(fontSize.Minimum == 10 && fontSize.Maximum == 18 && fontSize.IsSnapToTickEnabled &&
              fontSize.TickFrequency == 1 && app.InterfaceFontSize == SessionWindow.DefaultInterfaceFontSize,
              "Font-size slider range/default is incorrect");
        fontFamily.ApplyTemplate();
        var fontPopup = (Popup)fontFamily.Template.FindName("PART_Popup", fontFamily);
        Check(fontPopup != null && fontPopup.AllowsTransparency,
              "Font picker does not use the custom transparent popup");
        var focusTrace = new List<string>();
        KeyboardFocusChangedEventHandler traceFocus = delegate(object sender, KeyboardFocusChangedEventArgs args)
        {
            var oldFocus = args.OldFocus as FrameworkElement;
            var newFocus = args.NewFocus as FrameworkElement;
            focusTrace.Add(String.Format("Focus {0}: {1}/{2} -> {3}/{4}: {5}", args.RoutedEvent.Name,
                oldFocus == null ? "null" : oldFocus.GetType().Name, oldFocus == null ? "" : oldFocus.Name,
                newFocus == null ? "null" : newFocus.GetType().Name, newFocus == null ? "" : newFocus.Name,
                AppearanceFocusState(app)));
        };
        RoutedEventHandler traceOpened = delegate { focusTrace.Add("Opened: " + AppearanceFocusState(app)); };
        RoutedEventHandler traceClosed = delegate { focusTrace.Add("Closed: " + AppearanceFocusState(app)); };
        app.Window.AddHandler(Keyboard.GotKeyboardFocusEvent, traceFocus, true);
        button.ContextMenu.AddHandler(Keyboard.GotKeyboardFocusEvent, traceFocus, true);
        app.Window.AddHandler(Keyboard.LostKeyboardFocusEvent, traceFocus, true);
        button.ContextMenu.AddHandler(Keyboard.LostKeyboardFocusEvent, traceFocus, true);
        button.ContextMenu.Opened += traceOpened;
        button.ContextMenu.Closed += traceClosed;
        try
        {
            focusTrace.Add("Before grid focus: " + AppearanceFocusState(app));
            RequireKeyboardFixture(app);
            bool gridFocused = app.Grid.Focus();
            IInputElement focus = Keyboard.FocusedElement;
            focusTrace.Add("Grid.Focus=" + gridFocused + ": " + AppearanceFocusState(app));
            Check(app.Grid.IsKeyboardFocusWithin && focus != null,
                  "Keyboard fixture did not establish focus within the grid");
            Invoke(button);
            focusTrace.Add("After open/pump: " + AppearanceFocusState(app));
            Check(button.ContextMenu.IsOpen, "Appearance button did not open the menu");
            Press(button.ContextMenu, Key.Tab);
            focusTrace.Add("After Tab/pump: " + AppearanceFocusState(app));
            Check(slider.IsKeyboardFocusWithin, "Tab does not reach the opacity slider");
            Press(slider, Key.Left);
            Check(app.OpacityPercent == 89, "Keyboard Left did not adjust opacity");
            Press(slider, Key.Right);
            Check(app.OpacityPercent == 90, "Keyboard Right did not restore opacity");
            Press(button.ContextMenu, Key.Tab);
            Check(fontFamily.IsKeyboardFocusWithin, "Second Tab does not reach the font-family dropdown");
            Press(button.ContextMenu, Key.Tab);
            Check(fontSize.IsKeyboardFocusWithin, "Third Tab does not reach the font-size slider");
            Press(fontSize, Key.Left);
            Check(app.InterfaceFontSize == 12, "Keyboard Left did not adjust font size");
            Press(fontSize, Key.Right);
            Check(app.InterfaceFontSize == 13, "Keyboard Right did not restore font size");
            Press(button.ContextMenu, Key.Escape);
            Check(!button.ContextMenu.IsOpen, "Escape did not close Appearance");
            Check(Keyboard.FocusedElement == focus, "Appearance menu did not restore prior focus");
            Console.WriteLine("Keyboard fixture: interactive foreground verified; Open/Tab/arrows/Escape and focus restoration passed.");
        }
        catch
        {
            Console.Error.WriteLine(String.Join(Environment.NewLine, focusTrace));
            throw;
        }
        finally
        {
            app.Window.RemoveHandler(Keyboard.GotKeyboardFocusEvent, traceFocus);
            button.ContextMenu.RemoveHandler(Keyboard.GotKeyboardFocusEvent, traceFocus);
            app.Window.RemoveHandler(Keyboard.LostKeyboardFocusEvent, traceFocus);
            button.ContextMenu.RemoveHandler(Keyboard.LostKeyboardFocusEvent, traceFocus);
            button.ContextMenu.Opened -= traceOpened;
            button.ContextMenu.Closed -= traceClosed;
        }

        bool effects = !SystemParameters.HighContrast && app.Backdrop.Mode != WindowAppearance.Opaque;
        Invoke(button);
        ((MenuItem)app.Window.FindName("AppearanceAuto")).Focus();
        foreach (string next in new[] { "Acrylic", "Translucent", "Solid" })
        {
            Press((UIElement)Keyboard.FocusedElement, Key.Down);
            Check(Keyboard.FocusedElement == app.Window.FindName("Appearance" + next), "Menu arrow navigation failed");
        }
        Press((UIElement)Keyboard.FocusedElement, Key.Enter);
        Check(app.Appearance == AppearancePreference.Solid, "Keyboard Enter did not choose Solid");
        ChooseAppearance(app, "Translucent");
        var pin = (ToggleButton)app.Window.FindName("Pin");
        pin.IsChecked = true;
        foreach (int percent in new[] { 50, 75, 90, 100, 75 })
        {
            SetOpacity(app, percent);
            Check(app.Appearance == AppearancePreference.Translucent, "Slider changed the requested appearance");
            Check(app.Backdrop.OpacityAlpha == (byte)Math.Round(percent * 255.0 / 100, MidpointRounding.AwayFromZero),
                  "Opacity conversion did not round deliberately");
            Check(app.Backdrop.Mode == (effects && percent < 100 ? WindowAppearance.Translucent : WindowAppearance.Opaque),
                  "Unexpected effective mode for configured opacity");
            CheckPopupAppearance(app);
            CheckNativeAppearance(app);
            Check((GetWindowLong(new WindowInteropHelper(app.Window).Handle, -20) & 8) != 0,
                  "Changing opacity lost pin state");
        }
        Check(app.Backdrop.OpacityAlpha == 191, "75% alpha must be 191");
        Check(slider.Minimum == 50 && slider.Maximum == 100 && slider.IsSnapToTickEnabled && slider.TickFrequency == 1,
              "Opacity slider range/snapping changed");
        SetInterfaceFont(app, alternateFont, 16);
        Check(app.Window.FontFamily.Source == alternateFont && app.Window.FontSize == 16 &&
              button.ContextMenu.FontFamily.Source == alternateFont && button.ContextMenu.FontSize == 16,
              "Selected font did not apply to the window and appearance popup");
        Check((double)app.Window.Resources["SmallFontSize"] == 14 &&
              (double)app.Window.Resources["SummaryFontSize"] == 27,
              "Font size did not scale secondary and summary text");
        fontFamily.IsDropDownOpen = true;
        Pump();
        var dropDownSurface = fontPopup.Child as Border;
        Check(dropDownSurface != null &&
              ((SolidColorBrush)dropDownSurface.Background).Color ==
              ((SolidColorBrush)button.ContextMenu.Resources["PopupSurface"]).Color,
              "Font dropdown is not using the shared translucent popup surface");
        Check(Object.ReferenceEquals(fontFamily.ItemContainerStyle,
                                     app.Window.Resources["FontComboBoxItemStyle"]),
              "Font dropdown fell back to the stock item chrome");
        fontFamily.IsDropDownOpen = false;
        Press(button.ContextMenu, Key.Escape);
        ((ToggleButton)app.Window.FindName("Compact")).IsChecked = true;
        Pump();
        var restored = new SessionWindow(preferences);
        try
        {
            Check(restored.IsCompact && restored.Appearance == AppearancePreference.Translucent &&
                  restored.OpacityPercent == 75 && restored.InterfaceFontFamily == alternateFont &&
                  restored.InterfaceFontSize == 16,
                  "Appearance/opacity/font/compact preferences did not survive reload together");
        }
        finally { restored.Window.Close(); }
        foreach (string mode in new[] { "Acrylic", "Solid", "Auto", "Translucent" })
        {
            ChooseAppearance(app, mode);
            Check(app.Appearance.ToString() == mode && app.Backdrop.Preference == app.Appearance,
                  "Requested preference was confused with the effective mode");
            CheckPopupAppearance(app);
            Check(slider.IsEnabled == (mode == "Auto" || mode == "Translucent"), "Incorrect slider enablement");
            if (mode == "Solid") Check(app.Backdrop.Mode == WindowAppearance.Opaque, "Solid choice is not opaque");
            if (mode == "Acrylic" && effects)
            {
                int nativeMode;
                if (DwmGetWindowAttribute(new WindowInteropHelper(app.Window).Handle, 38, out nativeMode, 4) == 0)
                    Check(app.Backdrop.Mode == WindowAppearance.Acrylic && nativeMode == 3,
                          "Explicit Acrylic did not request the supported native material, including remotely");
            }
            CheckNativeAppearance(app);
            Check(app.IsCompact, "Changing appearance reset the compact layout");
            Check(app.InterfaceFontFamily == alternateFont && app.InterfaceFontSize == 16,
                  "Changing appearance reset the interface font");
            Check((GetWindowLong(new WindowInteropHelper(app.Window).Handle, -20) & 8) != 0,
                  "Changing appearance lost pin state");
        }
        SetOpacity(app, 90);
        ChooseAppearance(app, "Auto");
        Press(button.ContextMenu, Key.Escape);
        ((ToggleButton)app.Window.FindName("Compact")).IsChecked = false;
        pin.IsChecked = false;
        Check(Object.ReferenceEquals(selected, app.Grid.SelectedItem) && app.UnreadCount == unread,
              "Opening/changing appearance consumed unread state or changed selection");
        Check(File.ReadAllText(forgottenPath) == forgotten, "Appearance/layout saving changed Forget preferences");
        File.Delete(forgottenPath);
        foreach (bool compact in new[] { false, true })
        {
            app.SetCompact(compact, false);
            app.Window.Width = app.Window.MinWidth;
            Pump();
            var heading = (FrameworkElement)app.Window.FindName("Heading");
            var compactButton = (FrameworkElement)app.Window.FindName("Compact");
            Point headingRight = heading.TranslatePoint(new Point(heading.ActualWidth, 0), app.Window);
            Point controlsLeft = compactButton.TranslatePoint(new Point(), app.Window);
            Point gearRight = button.TranslatePoint(new Point(button.ActualWidth, 0), app.Window);
            Check(button.IsVisible && headingRight.X <= controlsLeft.X + 1 && gearRight.X <= app.Window.ActualWidth,
                  "Appearance header controls overlap or overflow at minimum width");
        }
        app.SetCompact(false, false);
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
        app.Apply(Sample());
        Console.WriteLine("Appearance UI: menu, focus, opacity, installed fonts, size scaling, persistence, pin and minimum widths passed.");
    }

    private static void TestAppearancePreferences(string directory)
    {
        string root = Path.Combine(directory, "appearance-preferences");
        Directory.CreateDirectory(root);
        string path = Path.Combine(root, "gadget-ui.json");
        try
        {
            File.WriteAllText(path, "{\"compact\":true}");
            var legacy = new SessionWindow(path);
            try
            {
                Check(legacy.IsCompact && legacy.Appearance == AppearancePreference.Auto && legacy.OpacityPercent == 90 &&
                      legacy.InterfaceFontFamily == SessionWindow.DefaultInterfaceFontFamily &&
                      legacy.InterfaceFontSize == SessionWindow.DefaultInterfaceFontSize,
                      "Legacy compact-only preference did not default to Auto/90 and the standard font");
            }
            finally { legacy.Window.Close(); }
            string[] invalid = {
                "null", "{}", "{\"compact\":\"true\"}",
                "{\"compact\":true,\"appearance\":\"unknown\"}", "{\"compact\":true,\"appearance\":1}",
                "{\"compact\":true,\"appearance\":null}", "{\"compact\":true,\"appearance\":true}",
                "{\"compact\":true,\"opacity\":49}", "{\"compact\":true,\"opacity\":101}",
                "{\"compact\":true,\"opacity\":75.5}", "{\"compact\":true,\"opacity\":\"90\"}",
                "{\"compact\":true,\"opacity\":null}", "{\"compact\":true,\"opacity\":true}",
                "{\"compact\":true,\"opacity\":NaN}", "{\"compact\":true,\"opacity\":Infinity}",
                "{\"compact\":true,\"opacity\":1e999}",
                "{\"compact\":true,\"font_family\":null}", "{\"compact\":true,\"font_family\":1}",
                "{\"compact\":true,\"font_family\":\"Definitely Missing Font 9284\"}",
                "{\"compact\":true,\"font_size\":9}", "{\"compact\":true,\"font_size\":19}",
                "{\"compact\":true,\"font_size\":13.5}", "{\"compact\":true,\"font_size\":\"13\"}",
                "{\"compact\":true,\"font_size\":null}", "{\"compact\":true,\"font_size\":true}"
            };
            foreach (string json in invalid)
            {
                File.WriteAllText(path, json);
                var bad = new SessionWindow(path);
                try
                {
                    bad.Apply(Sample());
                    Check(!bad.IsCompact && bad.Appearance == AppearancePreference.Auto && bad.OpacityPercent == 90 &&
                          bad.InterfaceFontFamily == SessionWindow.DefaultInterfaceFontFamily &&
                          bad.InterfaceFontSize == SessionWindow.DefaultInterfaceFontSize,
                          "Invalid preference was partially accepted: " + json);
                    Check(((FrameworkElement)bad.Window.FindName("ErrorPanel")).Visibility == Visibility.Visible,
                          "Invalid preference was not surfaced: " + json);
                }
                finally { bad.Window.Close(); }
            }
            File.Delete(path);
            Directory.CreateDirectory(path);
            var failure = new SessionWindow(path);
            try
            {
                failure.Apply(Sample());
                failure.Window.Show();
                Pump();
                var button = (Button)failure.Window.FindName("AppearanceButton");
                Invoke(button);
                Invoke((MenuItem)failure.Window.FindName("AppearanceSolid"));
                Check(failure.Appearance == AppearancePreference.Auto &&
                      ((MenuItem)failure.Window.FindName("AppearanceAuto")).IsChecked,
                      "Failed save left a falsely persisted appearance");
                Invoke(button);
                ((Slider)failure.Window.FindName("AppearanceOpacity")).Value = 75;
                Check(failure.OpacityPercent == 90 && ((Slider)failure.Window.FindName("AppearanceOpacity")).Value == 90,
                      "Failed opacity save did not revert the slider");
                var fontFamily = (ComboBox)failure.Window.FindName("InterfaceFontFamily");
                string alternateFont = fontFamily.Items.Cast<string>().First(
                    name => !String.Equals(name, SessionWindow.DefaultInterfaceFontFamily,
                                           StringComparison.CurrentCultureIgnoreCase));
                fontFamily.SelectedItem = alternateFont;
                Check(failure.InterfaceFontFamily == SessionWindow.DefaultInterfaceFontFamily &&
                      (string)fontFamily.SelectedItem == SessionWindow.DefaultInterfaceFontFamily,
                      "Failed font-family save did not revert the dropdown");
                ((Slider)failure.Window.FindName("InterfaceFontSize")).Value = 16;
                Check(failure.InterfaceFontSize == SessionWindow.DefaultInterfaceFontSize &&
                      ((Slider)failure.Window.FindName("InterfaceFontSize")).Value ==
                      SessionWindow.DefaultInterfaceFontSize,
                      "Failed font-size save did not revert the slider");
                Press(button.ContextMenu, Key.Escape);
                ((ToggleButton)failure.Window.FindName("Compact")).IsChecked = true;
                Check(!failure.IsCompact && ((ToggleButton)failure.Window.FindName("Compact")).IsChecked == false,
                      "Failed compact save did not revert coherently");
                Check(((TextBlock)failure.Window.FindName("ErrorText")).Text.Contains("Cannot save"),
                      "Persistence failure was not reported in the existing error panel");
                Check(Directory.GetFiles(root).Length == 0, "Failed preference save leaked an atomic scratch file");
                Directory.Delete(path);
                ChooseAppearance(failure, "Solid");
                Check(((FrameworkElement)failure.Window.FindName("ErrorPanel")).Visibility == Visibility.Collapsed,
                      "Successful retry did not clear the preference error");
            }
            finally { failure.Window.Close(); }
        }
        finally { Directory.Delete(root, true); }
    }

    private static void CheckOpenAppearanceMenu(SessionWindow app)
    {
        var menu = ((Button)app.Window.FindName("AppearanceButton")).ContextMenu;
        Check(menu.IsOpen, "Opacity adjustment closed Appearance");
        var popup = PresentationSource.FromVisual(menu) as HwndSource;
        Check(popup != null, "Open Appearance menu lost its native source");
        IntPtr scan = new WindowInteropHelper(app.Window).Handle;
        bool above = false;
        for (int count = 0; count < 1000 && scan != IntPtr.Zero; count++)
        {
            scan = GetWindow(scan, 3);
            if (scan == popup.Handle) { above = true; break; }
        }
        Check(above, "Opacity adjustment raised the gadget above its open menu");
        var auto = (MenuItem)app.Window.FindName("AppearanceAuto");
        Check(auto.IsVisible && auto.IsEnabled, "Auto is unavailable after opacity adjustment");
    }

    private static void TestAppearanceMenuAdjustment(SessionWindow app)
    {
        var button = (Button)app.Window.FindName("AppearanceButton");
        var menu = button.ContextMenu;
        var slider = (Slider)app.Window.FindName("AppearanceOpacity");
        var pin = (ToggleButton)app.Window.FindName("Pin");
        int closes = 0;
        RoutedEventHandler trackClosed = delegate { closes++; };
        menu.Closed += trackClosed;
        try
        {
            foreach (bool pinned in new[] { false, true })
            {
                pin.IsChecked = pinned;
                ChooseAppearance(app, "Translucent");
                SetOpacity(app, 75);
                CheckOpenAppearanceMenu(app);
                slider.Focus();
                Pump();
                int initialCloses = closes;
                var popup = (HwndSource)PresentationSource.FromVisual(slider);
                for (int expected = 76; expected <= 90; expected++)
                {
                    // Target only this synthetic popup; never inject global keyboard input.
                    Check(PostMessage(popup.Handle, 0x0100, new IntPtr(0x27), new IntPtr(0x014D0001)),
                          "Could not post native Right key-down");
                    Check(PostMessage(popup.Handle, 0x0101, new IntPtr(0x27), new IntPtr(unchecked((int)0xC14D0001))),
                          "Could not post native Right key-up");
                    Pump();
                    Check(slider.Value == expected && app.OpacityPercent == expected, "Native slider keyboard adjustment failed");
                    Check(closes == initialCloses, "Keyboard opacity adjustment emitted a Closed event");
                    CheckOpenAppearanceMenu(app);
                    CheckNativeAppearance(app);
                }
                Invoke(button);
                CheckOpenAppearanceMenu(app);
                ChooseAppearance(app, "Auto");
                Check(app.Appearance == AppearancePreference.Auto && app.OpacityPercent == 90,
                      "Auto could not be selected after native slider adjustments");
            }
        }
        finally
        {
            menu.Closed -= trackClosed;
            menu.IsOpen = false;
            pin.IsChecked = false;
        }
        Console.WriteLine("Appearance popup: 30 native Right keys, pinned/unpinned, visible z-order and subsequent Auto selection passed.");
    }

    [DllImport("user32.dll")]
    private static extern IntPtr GetDC(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")]
    private static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);
    [DllImport("gdi32.dll")]
    private static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")]
    private static extern IntPtr CreateCompatibleBitmap(IntPtr dc, int width, int height);
    [DllImport("gdi32.dll")]
    private static extern IntPtr SelectObject(IntPtr dc, IntPtr obj);
    [DllImport("gdi32.dll")]
    private static extern bool BitBlt(IntPtr dest, int x, int y, int width, int height,
                                       IntPtr src, int srcX, int srcY, int operation);
    [DllImport("gdi32.dll")]
    private static extern bool DeleteObject(IntPtr obj);
    [DllImport("gdi32.dll")]
    private static extern bool DeleteDC(IntPtr dc);
    [DllImport("dwmapi.dll")]
    private static extern int DwmFlush();

    private static void Settle()
    {
        var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(700) };
        var frame = new DispatcherFrame();
        timer.Tick += delegate { timer.Stop(); frame.Continue = false; };
        timer.Start();
        Dispatcher.PushFrame(frame);
        DwmFlush();
    }

    private static BitmapSource CaptureScreen(Window background, string path)
    {
        Settle();
        Point origin = background.PointToScreen(new Point(0, 0));
        Point end = background.PointToScreen(new Point(background.ActualWidth, background.ActualHeight));
        int width = (int)(end.X - origin.X), height = (int)(end.Y - origin.Y);
        IntPtr screen = GetDC(IntPtr.Zero);
        IntPtr memory = IntPtr.Zero, bitmap = IntPtr.Zero, previous = IntPtr.Zero;
        try
        {
            Check(screen != IntPtr.Zero, "Cannot acquire screen capture DC");
            memory = CreateCompatibleDC(screen);
            bitmap = CreateCompatibleBitmap(screen, width, height);
            Check(memory != IntPtr.Zero && bitmap != IntPtr.Zero, "Cannot allocate native capture");
            previous = SelectObject(memory, bitmap);
            Check(BitBlt(memory, 0, 0, width, height, screen, (int)origin.X, (int)origin.Y, 0x00CC0020),
                  "Native screen capture failed");
            BitmapSource image = Imaging.CreateBitmapSourceFromHBitmap(bitmap, IntPtr.Zero, Int32Rect.Empty,
                                                                       BitmapSizeOptions.FromEmptyOptions());
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(image));
            using (Stream output = File.Create(path)) encoder.Save(output);
            return image;
        }
        finally
        {
            if (previous != IntPtr.Zero) SelectObject(memory, previous);
            if (bitmap != IntPtr.Zero) DeleteObject(bitmap);
            if (memory != IntPtr.Zero) DeleteDC(memory);
            if (screen != IntPtr.Zero) ReleaseDC(IntPtr.Zero, screen);
        }
    }

    private static double PixelDifference(BitmapSource first, BitmapSource second, Int32Rect region)
    {
        var a = new FormatConvertedBitmap(first, PixelFormats.Bgra32, null, 0);
        var b = new FormatConvertedBitmap(second, PixelFormats.Bgra32, null, 0);
        int stride = region.Width * 4;
        byte[] left = new byte[stride * region.Height], right = new byte[left.Length];
        a.CopyPixels(region, left, stride, 0);
        b.CopyPixels(region, right, stride, 0);
        double difference = 0;
        for (int i = 0; i < left.Length; i++)
            if (i % 4 != 3) difference += Math.Abs(left[i] - right[i]);
        return difference / (region.Width * region.Height * 3);
    }

    private static void CaptureFrost(SessionWindow app)
    {
        string path = Environment.GetEnvironmentVariable("COPILOT_GADGET_FROST_SCREENSHOT");
        if (String.IsNullOrEmpty(path)) return;
        string stem = Path.Combine(Path.GetDirectoryName(path), Path.GetFileNameWithoutExtension(path));
        var pattern = new SyntheticBackdrop();
        Rect work = SystemParameters.WorkArea;
        var background = new Window { WindowStyle = WindowStyle.None, ResizeMode = ResizeMode.NoResize,
            WindowStartupLocation = WindowStartupLocation.Manual, Left = work.Left, Top = work.Top,
            Width = Math.Min(1080, work.Width), Height = Math.Min(720, work.Height),
            ShowInTaskbar = false, Topmost = true, Content = pattern };
        double left = app.Window.Left, top = app.Window.Top, width = app.Window.Width, height = app.Window.Height;
        bool pinned = app.Window.Topmost;
        try
        {
            background.Show();
            app.Window.Width = Math.Min(900, background.Width - 140);
            app.Window.Height = Math.Min(530, background.Height - 160);
            app.Window.Left = background.Left + 70;
            app.Window.Top = background.Top + 80;
            app.Window.Topmost = true;
            bool activated = app.Window.Activate();
            app.Sort(app.Grid.Columns[2], ListSortDirection.Ascending);
            app.Grid.SelectedItem = app.Rows.Single(row => row.Status == "Needs input");
            Pump();
            BitmapSource first = CaptureScreen(background, path);
            bool foreground = GetForegroundWindow() == new WindowInteropHelper(app.Window).Handle;
            Console.WriteLine("Native visual capture: active={0}, foreground={1}, remoteSession={2}, mode={3}",
                              activated && app.Window.IsActive,
                              foreground, GetSystemMetrics(0x1000) != 0, app.Backdrop.Mode);
            pattern.Alternate = true;
            pattern.InvalidateVisual();
            BitmapSource second = CaptureScreen(background, stem + "-alternate.png");
            Point origin = background.PointToScreen(new Point());
            Point gutter = app.Window.PointToScreen(new Point(8, 100));
            double scale = PresentationSource.FromVisual(background).CompositionTarget.TransformToDevice.M11;
            var region = new Int32Rect((int)(gutter.X - origin.X), (int)(gutter.Y - origin.Y),
                                       Math.Max(1, (int)(4 * scale)), (int)(100 * scale));
            double change = PixelDifference(first, second, region);
            Console.WriteLine("Native visual capture: background-change mean RGB delta={0:F3} ({1}).", change,
                              change > 1 ? "visible background response" : "solid material; blur not demonstrated");
            WindowAppearance capturedMode = app.Backdrop.Mode;
            if (capturedMode == WindowAppearance.Translucent)
                Check(change > 3, "Plain-translucency fallback must visibly respond to the synthetic background");
            app.Backdrop.Refresh(true);
            BitmapSource solid = CaptureScreen(background, stem + "-opaque.png");
            pattern.Alternate = false;
            pattern.InvalidateVisual();
            BitmapSource solidAlternate = CaptureScreen(background, stem + "-opaque-alternate.png");
            double solidChange = PixelDifference(solid, solidAlternate, region);
            Check(solidChange < 0.1,
                  "Opaque fallback leaks the synthetic background");
            File.WriteAllText(stem + "-measurements.txt", String.Format(
                "Mode={0}\r\nNativeAlpha={1}\r\nRemoteSession={2}\r\nForeground={3}\r\nMeanRGBDelta={4:F6}\r\nOpaqueRGBDelta={5:F6}\r\n",
                capturedMode, capturedMode == WindowAppearance.Translucent ? app.Backdrop.OpacityAlpha : 255,
                GetSystemMetrics(0x1000) != 0, foreground, change, solidChange));
            app.Backdrop.Refresh(SystemParameters.HighContrast);
            Invoke((Button)app.Window.FindName("AppearanceButton"));
            CaptureScreen(background, stem + "-menu.png");
            var fontFamily = (ComboBox)app.Window.FindName("InterfaceFontFamily");
            fontFamily.IsDropDownOpen = true;
            Pump();
            CaptureScreen(background, stem + "-font-menu.png");
            fontFamily.IsDropDownOpen = false;
            Press(((Button)app.Window.FindName("AppearanceButton")).ContextMenu, Key.Escape);
            app.SetCompact(true, false);
            Pump();
            CheckNativeAppearance(app);
            CaptureScreen(background, stem + "-compact.png");
            Invoke((Button)app.Window.FindName("AppearanceButton"));
            CaptureScreen(background, stem + "-compact-menu.png");
            Press(((Button)app.Window.FindName("AppearanceButton")).ContextMenu, Key.Escape);
            app.SetCompact(false, false);
            app.Window.Hide();
            CaptureScreen(background, stem + "-background.png");
        }
        finally
        {
            app.Backdrop.Refresh(SystemParameters.HighContrast);
            app.SetCompact(false, false);
            app.Window.Left = left;
            app.Window.Top = top;
            app.Window.Width = width;
            app.Window.Height = height;
            app.Window.Topmost = pinned;
            background.Close();
            app.Window.Show();
        }
    }

    private static void TestUnread(SessionWindow app)
    {
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
        app.Apply(Sample());
        Check(app.UnreadCount == 0, "Startup Done sessions must not be unread");
        app.Grid.SelectedItem = app.Rows.Single(row => row.Status == "In progress");
        Snapshot finished = Sample();
        finished.rows[0].status = "Done";
        finished.rows[1].status = "Done";
        app.Apply(finished);
        Check(app.UnreadCount == 2, "Both observed completions must be unread, including selected row");
        Check(app.Window.TaskbarItemInfo.Overlay != null, "Missing taskbar unread overlay");
        Check(app.Window.TaskbarItemInfo.Description.Contains("2 unread"), "Incorrect taskbar badge description");
        Check(app.Rows.Count(row => row.StatusLabel == "Done \u00B7 New") == 2, "Missing unread row indicators");
        app.Apply(finished);
        app.Sort(app.Grid.Columns[0], ListSortDirection.Descending);
        Check(app.UnreadCount == 2, "Refresh or sorting consumed/doubled unread completions");
        app.Grid.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice,
            PresentationSource.FromVisual(app.Grid), Environment.TickCount, Key.Enter)
            { RoutedEvent = Keyboard.PreviewKeyDownEvent });
        Check(app.UnreadCount == 1, "Enter on selected completed session did not mark read");
        app.Grid.SelectedItem = app.Rows.Single(row => row.Unread);
        Check(app.UnreadCount == 0, "Selecting completed session did not mark read");
        Check(app.Window.TaskbarItemInfo.Overlay == null, "Zero count must remove taskbar overlay");

        app.Apply(Sample());
        var unavailable = Sample();
        unavailable.rows[0].status = "Unknown";
        app.Apply(unavailable);
        app.Apply(finished);
        Check(app.UnreadCount == 2, "Temporary unavailable status lost observed work");
        var button = (Button)app.Window.FindName("MarkAllRead");
        ((IInvokeProvider)UIElementAutomationPeer.CreatePeerForElement(button).GetPattern(PatternInterface.Invoke)).Invoke();
        Pump();
        Check(app.UnreadCount == 0, "Mark all read did not clear completions");
        app.Apply(finished);
        Check(app.UnreadCount == 0, "Repeated Done recreated a read badge");
        app.Apply(Sample());
        app.Apply(finished);
        Check(app.UnreadCount == 2, "Second completion should become unread again");
        app.Apply(Sample());
        Check(app.UnreadCount == 0, "Resumed work should clear outdated done badges");
        var replacement = Sample();
        replacement.rows[0].pid++;
        replacement.rows[0].status = "Done";
        app.Apply(replacement);
        Check(app.UnreadCount == 0, "New process must not inherit prior completion state");
        app.Apply(finished);
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
        Check(app.UnreadCount == 0 && app.Window.TaskbarItemInfo.Overlay == null,
              "Removed sessions must not leave unread badges");
        Check(SessionWindow.CreateBadge(100) != null, "99+ badge failed");
    }

    private static void TestSharedProcess(SessionWindow app)
    {
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
        Snapshot sessions = Sample();
        foreach (SessionData row in sessions.rows) row.pid = 4242;
        app.Apply(sessions);
        Check(app.Rows.Count == 3, "Sessions sharing a PID were merged");
        SessionRow waiting = app.Rows.Single(row => row.Status == "Needs input");
        app.Grid.SelectedItem = waiting;
        sessions = Sample();
        foreach (SessionData row in sessions.rows) row.pid = 4242;
        sessions.rows[0].status = "Done";
        app.Apply(sessions);
        Check(app.UnreadCount == 1, "Completion should count only its session, not its process");
        Check(waiting.Status == "Needs input" && !waiting.Unread, "Sibling status was overwritten");
        Check(Object.ReferenceEquals(app.Grid.SelectedItem, waiting), "Sibling selection was lost");
        sessions = Sample();
        foreach (SessionData row in sessions.rows) row.pid = 4242;
        sessions.rows[0].status = "Done";
        sessions.rows[1].status = "Done";
        app.Apply(sessions);
        Check(app.UnreadCount == 2, "Same-PID completions must be counted independently");
        app.MarkSelectedRead();
        Check(app.UnreadCount == 1, "Reading one session cleared its sibling badge");
        app.Apply(new Snapshot { rows = new[] { sessions.rows[0], sessions.rows[2] }, errors = new string[0] });
        Check(app.Rows.Count == 2 && app.UnreadCount == 1, "Closing a sibling changed other session state");
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
    }

    private static Snapshot ForgetSample(string revision, string status = "Done")
    {
        Snapshot sample = Sample();
        sample.rows[0].activity_revision = revision;
        sample.rows[0].status = status;
        sample.rows[1].pid = sample.rows[0].pid;
        return sample;
    }

    private static void TestForget(SessionWindow app, string preferences)
    {
        const string first = "00000000000000000100/first";
        const string newer = "00000000000000000101/second";
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
        app.Apply(ForgetSample(first, "In progress"));
        app.Grid.SelectedItem = app.Rows.Single(row => row.Id.StartsWith("9d24"));
        app.Apply(ForgetSample(first));
        Check(app.UnreadCount == 1, "Forget fixture requires an unread completion");
        var button = (Button)app.Window.FindName("Forget");
        Check(button.IsEnabled, "Forget not enabled for a ready selected session");
        ((IInvokeProvider)UIElementAutomationPeer.CreatePeerForElement(button).GetPattern(PatternInterface.Invoke)).Invoke();
        Pump();
        Check(app.Rows.Count == 2 && app.UnreadCount == 0, "Forget did not hide row and clear badge");
        Check(app.Rows.Any(row => row.Status == "Needs input"), "Forget affected a same-PID sibling");
        app.Apply(ForgetSample(first));
        Check(app.Rows.Count == 2, "Unchanged activity resurfaced forgotten session");
        app.Apply(ForgetSample("00000000000000000099/older", "Loading"));
        Check(app.Rows.Count == 2, "Partial replay resurfaced forgotten session");
        app.Apply(ForgetSample(newer, "Unknown"));
        Check(app.Rows.Count == 2, "Unreliable status resurfaced forgotten session");
        app.Apply(ForgetSample("00000000000000000099/older"));
        Check(app.Rows.Count == 2, "Older rewritten history resurfaced forgotten session");
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
        app.Apply(ForgetSample(first));
        Check(app.Rows.Count == 2, "Reconnecting alone resurfaced forgotten session");
        var restored = new SessionWindow(preferences);
        try
        {
            restored.Apply(ForgetSample(first));
            Check(restored.Rows.Count == 2, "Forget did not survive restart");
            restored.Apply(ForgetSample(newer));
            Check(restored.Rows.Count == 3 && restored.UnreadCount == 1,
                  "New completed activity did not resurface with an unread badge");
        }
        finally { restored.Window.Close(); }
        app.Apply(ForgetSample(newer, "In progress"));
        Check(app.Rows.Count == 3 && app.UnreadCount == 0, "New work did not resurface independently");
        app.Grid.SelectedItem = app.Rows.Single(row => row.Id.StartsWith("9d24"));
        app.ForgetSelected();
        Check(app.Rows.Count == 2, "Could not forget an in-progress session locally");
        app.Apply(ForgetSample("00000000000000000102/input", "Needs input"));
        Check(app.Rows.Count == 3, "New input request did not resurface");
        app.Grid.SelectedItem = app.Rows.Single(row => row.Id.StartsWith("9d24"));
        app.Apply(ForgetSample(null, "Loading"));
        Check(!button.IsEnabled, "Forget allowed incomplete history to establish a bad baseline");
        app.Apply(ForgetSample(""));
        app.ForgetSelected();
        Check(app.Rows.Count == 2, "Metadata-only ghost cannot be forgotten");
        app.Apply(ForgetSample(""));
        Check(app.Rows.Count == 2, "Metadata-only ghost resurfaced without activity");
        app.Apply(ForgetSample(newer, "In progress"));
        Check(app.Rows.Count == 3, "First conversation activity did not resurface ghost");

        string directory = Path.Combine(Path.GetDirectoryName(preferences), "failure-case");
        Directory.CreateDirectory(directory);
        string blocked = Path.Combine(directory, "gadget-forgotten.json");
        Directory.CreateDirectory(blocked);
        var failure = new SessionWindow(Path.Combine(directory, "gadget-ui.json"));
        try
        {
            failure.Apply(ForgetSample(first));
            failure.Grid.SelectedItem = failure.Rows.First(row => row.ActivityRevision != null);
            failure.ForgetSelected();
            Check(failure.Rows.Count == 3, "Failed persistence falsely hid the session");
            Check(((TextBlock)failure.Window.FindName("ErrorText")).Text.Contains("Cannot save forgotten"),
                  "Forget persistence failure not surfaced");
        }
        finally
        {
            failure.Window.Close();
            Directory.Delete(blocked);
            Directory.Delete(directory);
        }
        app.Apply(new Snapshot { rows = new SessionData[0], errors = new string[0] });
    }

    private static void TestScrollbars(SessionWindow app)
    {
        var data = Enumerable.Range(0, 60).Select(index => new SessionData {
            id = "scroll-" + index.ToString("D3"), title = "Session " + index,
            source = "Windows", cwd = @"C:\work\scroll-test", status = "Done", pid = 1234
        }).ToArray();
        app.Apply(new Snapshot { rows = data, errors = new string[0] });
        foreach (bool compact in new[] { false, true })
        {
            app.SetCompact(compact, false);
            app.Window.Width = app.Window.MinWidth;
            app.Window.Height = app.Window.MinHeight;
            Pump();
            var viewer = Descendants<ScrollViewer>(app.Grid).First();
            var vertical = Descendants<ScrollBar>(app.Grid).Single(bar => bar.Orientation == Orientation.Vertical);
            Check(vertical.IsVisible && vertical.ActualWidth == 14,
                  "Missing slim vertical scrollbar: visible=" + vertical.IsVisible +
                  ", width=" + vertical.ActualWidth + ", style=" + (vertical.Style == null ? "none" : "present"));
            var track = (Track)vertical.Template.FindName("PART_Track", vertical);
            Check(track != null && track.IsDirectionReversed, "Vertical track direction incorrect");
            Check(track.Thumb.Template.FindName("ThumbSurface", track.Thumb) != null, "Custom thumb missing");
            Check(track.Thumb.ActualHeight >= 24, "Scrollbar thumb is too small to grab");
            viewer.ScrollToTop();
            Pump();
            ((IInvokeProvider)UIElementAutomationPeer.CreatePeerForElement(track.IncreaseRepeatButton)
                .GetPattern(PatternInterface.Invoke)).Invoke();
            Pump();
            Check(viewer.VerticalOffset > 0, "Track page-down does not scroll");
            double before = viewer.VerticalOffset;
            track.Thumb.RaiseEvent(new DragDeltaEventArgs(0, 20) { RoutedEvent = Thumb.DragDeltaEvent });
            Pump();
            Check(viewer.VerticalOffset > before, "Dragging thumb down does not scroll down");
            viewer.ScrollToTop();
            Pump();
            viewer.RaiseEvent(new MouseWheelEventArgs(Mouse.PrimaryDevice, Environment.TickCount, -120)
                { RoutedEvent = Mouse.MouseWheelEvent });
            Pump();
            Check(viewer.VerticalOffset > 0, "Mouse-wheel scrolling broke");
            app.Grid.Focus();
            app.Grid.SelectedIndex = 0;
            app.Grid.CurrentCell = new DataGridCellInfo(app.Grid.Items[0], app.Grid.Columns[0]);
            app.Grid.ScrollIntoView(app.Grid.Items[0], app.Grid.Columns[0]);
            Pump();
            app.Grid.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice,
                PresentationSource.FromVisual(app.Grid), Environment.TickCount, Key.PageDown)
                { RoutedEvent = Keyboard.KeyDownEvent });
            Pump();
            Check(app.Grid.SelectedIndex > 0, "Keyboard PageDown navigation broke");

            DataGridLength width = app.Grid.Columns[0].Width;
            app.Grid.Columns[0].Width = 900;
            Pump();
            var horizontal = Descendants<ScrollBar>(app.Grid).Single(bar => bar.Orientation == Orientation.Horizontal);
            Check(horizontal.IsVisible && horizontal.ActualHeight == 14, "Missing slim horizontal scrollbar");
            var horizontalTrack = (Track)horizontal.Template.FindName("PART_Track", horizontal);
            Check(!horizontalTrack.IsDirectionReversed, "Horizontal track direction incorrect");
            ((IInvokeProvider)UIElementAutomationPeer.CreatePeerForElement(horizontalTrack.IncreaseRepeatButton)
                .GetPattern(PatternInterface.Invoke)).Invoke();
            Pump();
            Check(viewer.HorizontalOffset > 0, "Horizontal track page-right does not scroll");
            app.Grid.Columns[0].Width = width;
            string screenshot = Environment.GetEnvironmentVariable("COPILOT_GADGET_SCREENSHOT");
            if (!String.IsNullOrEmpty(screenshot))
                Screenshot(app, Path.Combine(Path.GetDirectoryName(screenshot),
                           compact ? "gadget-scrollbar-compact.png" : "gadget-scrollbar-comfortable.png"));
        }
        app.SetCompact(false, false);
        app.Apply(Sample());
        Pump();
    }

    private static void TestColumnResizing(SessionWindow app)
    {
        app.SetCompact(false, false);
        app.Apply(Sample());
        Pump();
        Check(app.Grid.CanUserResizeColumns, "Session table does not allow column resizing");
        var headers = Descendants<DataGridColumnHeader>(app.Grid)
            .Where(candidate => candidate.Column != null).ToArray();
        Check(headers.Length == app.Grid.Columns.Count, "Not all visible columns have resizeable headers");
        var header = headers.Single(item => item.Column == app.Grid.Columns[1]);
        header.ApplyTemplate();
        var left = (Thumb)header.Template.FindName("PART_LeftHeaderGripper", header);
        var right = (Thumb)header.Template.FindName("PART_RightHeaderGripper", header);
        Check(left != null && right != null && left.Cursor == Cursors.SizeWE && right.Cursor == Cursors.SizeWE,
              "Column header resize grippers are missing or do not advertise horizontal resizing");
        double before = app.Grid.Columns[1].ActualWidth;
        right.RaiseEvent(new DragDeltaEventArgs(36, 0) { RoutedEvent = Thumb.DragDeltaEvent });
        Pump();
        Check(app.Grid.Columns[1].ActualWidth >= before + 30,
              "Dragging the header divider did not resize the column");
        app.SetCompact(true, false);
        app.SetCompact(false, false);
        Pump();
        Check(app.Grid.Columns[0].Width.IsStar && app.Grid.Columns[1].Width.Value == 155 &&
              app.Grid.Columns[2].Width.Value == 156 && app.Grid.Columns[3].Width.Value == 106,
              "Switching layouts did not restore the documented default column widths");
    }

    [STAThread]
    public static int Main()
    {
        var application = new Application();
        string directory = Path.Combine(Path.GetTempPath(), "GadgetUiTests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        string preferences = Path.Combine(directory, "gadget-ui.json");
        var app = new SessionWindow(preferences);
        try
        {
            app.Apply(Sample());
            app.Window.Show();
            Pump();
            TestBackdrop(app);
            TestAppearance(app, preferences);
            TestAppearanceMenuAdjustment(app);
            TestAppearancePreferences(directory);
            TestColumnResizing(app);
            Check(app.Window.Icon != null, "Missing application icon");
            foreach (DataGridColumn column in app.Grid.Columns)
            {
                // Remove the initial status sort so every first header click is ascending.
                column.SortDirection = null;
                ClickHeader(app, column);
                Check(column.SortDirection == ListSortDirection.Ascending, "Header did not sort ascending");
                var property = typeof(SessionRow).GetProperty(column.SortMemberPath);
                var ascending = app.Grid.Items.Cast<SessionRow>().Select(row => property.GetValue(row, null)).ToArray();
                Check(ascending.SequenceEqual(ascending.OrderBy(value => value)), "Incorrect ascending order");
                ClickHeader(app, column);
                Check(column.SortDirection == ListSortDirection.Descending, "Header did not reverse sorting");
                var descending = app.Grid.Items.Cast<SessionRow>().Select(row => property.GetValue(row, null)).ToArray();
                Check(descending.SequenceEqual(descending.OrderByDescending(value => value)), "Incorrect descending order");
            }
            DataGridColumn title = app.Grid.Columns[0];
            title.SortDirection = null;
            ClickHeader(app, title);
            SessionRow selected = (SessionRow)app.Grid.Items[1];
            app.Grid.SelectedItem = selected;
            Snapshot update = Sample();
            update.rows[0].title = "A newly updated session";
            app.Apply(update);
            Pump();
            Check(((SessionRow)app.Grid.Items[0]).Title == "A newly updated session", "Updates ignored sort key changes");
            Check(title.SortDirection == ListSortDirection.Ascending, "Refresh lost sort direction");
            Check(Object.ReferenceEquals(app.Grid.SelectedItem, selected), "Refresh lost selection");
            var pin = (ToggleButton)app.Window.FindName("Pin");
            pin.IsChecked = true;
            Check(app.Window.Topmost, "Pin did not make the window topmost");
            pin.IsChecked = false;
            Check(!app.Window.Topmost, "Unpin did not clear topmost");
            Check(((TextBlock)app.Window.FindName("Detail")).Text.Contains(selected.Id), "Missing selection details");
            app.Apply(new Snapshot { rows = new SessionData[0], errors = new[] { "Collector unavailable" } });
            Check(app.Grid.Items.Count == 0, "Closed sessions remain visible");
            Check(((FrameworkElement)app.Window.FindName("ErrorPanel")).Visibility == Visibility.Visible,
                  "Missing collector error");
            Check(((TextBlock)app.Window.FindName("EmptyTitle")).Text.Contains("unavailable"), "False healthy empty state");
            app.Apply(Sample());
            Check(((FrameworkElement)app.Window.FindName("ErrorPanel")).Visibility == Visibility.Collapsed,
                  "Recovered source still shows error");
            app.Sort(app.Grid.Columns[2], ListSortDirection.Ascending);
            Pump();
            TestUnread(app);
            TestSharedProcess(app);
            TestForget(app, preferences);
            TestScrollbars(app);
            app.Apply(Sample());
            CaptureFrost(app);
            string screenshot = Environment.GetEnvironmentVariable("COPILOT_GADGET_SCREENSHOT");
            if (!String.IsNullOrEmpty(screenshot)) Screenshot(app, screenshot);
            app.Window.Width = app.Window.MinWidth;
            app.Window.Height = app.Window.MinHeight;
            Pump();
            Check(app.Grid.ActualHeight > 80, "Table collapsed at minimum window size");
            var compact = (ToggleButton)app.Window.FindName("Compact");
            compact.IsChecked = true;
            Pump();
            Check(app.IsCompact && app.Window.Width == 480 && app.Window.Height == 300, "Compact dimensions incorrect");
            CheckOpaqueContent(app);
            Check(((FrameworkElement)app.Window.FindName("Forget")).IsVisible, "Forget missing from compact layout");
            Check(((FrameworkElement)app.Window.FindName("SummaryCards")).Visibility == Visibility.Collapsed,
                  "Compact layout should hide summary cards");
            Check(app.Grid.Columns[3].Visibility == Visibility.Collapsed, "Compact layout should hide ID column");
            Check(Descendants<DataGridRow>(app.Grid).All(row => row.ActualHeight <= 40), "Compact rows too tall");
            ClickHeader(app, app.Grid.Columns[1]);
            Check(app.Grid.Columns[1].SortDirection != null, "Compact headers not sortable");
            if (!String.IsNullOrEmpty(screenshot))
                Screenshot(app, Path.Combine(Path.GetDirectoryName(screenshot), "gadget-compact.png"));
            app.Window.Width = app.Window.MinWidth;
            app.Window.Height = app.Window.MinHeight;
            Pump();
            Check(app.Grid.ActualHeight >= 80, "Compact layout unusable at minimum size");
            var restored = new SessionWindow(preferences);
            Check(restored.IsCompact, "Compact preference did not survive restart");
            restored.Window.Close();
            compact.IsChecked = false;
            Pump();
            Check(!app.IsCompact && app.Grid.Columns[3].Visibility == Visibility.Visible, "Comfortable restore failed");
            Console.WriteLine("Native UI: sorts, refresh, compact layouts, persistence, icon, unread transitions and taskbar overlay passed.");
            return 0;
        }
        catch (Exception error)
        {
            // Test runner boundary: retain the full failure and return a failing exit code.
            Console.Error.WriteLine(error);
            return 1;
        }
        finally
        {
            app.Window.Close();
            application.Shutdown();
            File.Delete(preferences);
            File.Delete(Path.Combine(directory, "gadget-forgotten.json"));
            Directory.Delete(directory);
        }
    }
}
