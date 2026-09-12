using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Input;
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
