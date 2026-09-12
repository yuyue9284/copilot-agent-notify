using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Interop;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Input;
using System.Windows.Shell;
using System.Windows.Threading;

namespace CopilotSessions
{
    public sealed class SessionData
    {
        public string id { get; set; }
        public string title { get; set; }
        public string source { get; set; }
        public string cwd { get; set; }
        public string status { get; set; }
        public int pid { get; set; }
    }

    public sealed class Snapshot
    {
        public SessionData[] rows { get; set; }
        public string[] errors { get; set; }
        public bool discovering { get; set; }
    }

    public sealed class SessionRow : INotifyPropertyChanged
    {
        private SessionData data;
        private bool working;
        public bool Unread { get; private set; }
        public SessionRow(SessionData value)
        {
            data = value;
            working = value.status == "In progress" || value.status == "Needs input";
        }
        public string Key { get { return Source + "/" + Id; } }
        public string Id { get { return data.id; } }
        public string ShortId { get { return Id.Substring(0, Math.Min(8, Id.Length)); } }
        public string Title { get { return data.title; } }
        public string Source { get { return data.source; } }
        public string Cwd { get { return data.cwd; } }
        public string Status { get { return data.status; } }
        public string StatusLabel { get { return Status == "Done" && Unread ? "Done \u00B7 New" : Status; } }
        public int Pid { get { return data.pid; } }
        public int StatusRank
        {
            get
            {
                switch (Status)
                {
                    case "Needs input": return 0;
                    case "In progress": return 1;
                    case "Loading": return 2;
                    case "Unknown": return 3;
                    default: return 4;
                }
            }
        }
        public string StatusForeground
        {
            get { return new[] { "#F3CF87", "#8EC5FF", "#BBC6D7", "#EEA9B2", "#8CDBC0" }[StatusRank]; }
        }
        public string StatusBackground
        {
            get { return new[] { "#3A3021", "#213750", "#2C3545", "#402A34", "#203C35" }[StatusRank]; }
        }
        public event PropertyChangedEventHandler PropertyChanged;
        public void Update(SessionData value)
        {
            if (value.pid != data.pid)
            {
                working = false;
                Unread = false;
            }
            if (value.status == "In progress" || value.status == "Needs input")
            {
                working = true;
                Unread = false;
            }
            else if (value.status == "Done" && working)
            {
                Unread = true;
                working = false;
            }
            data = value;
            Notify();
        }
        private void Notify()
        {
            if (PropertyChanged != null) PropertyChanged(this, new PropertyChangedEventArgs(""));
        }
        public void MarkRead() { Unread = false; Notify(); }
        public void MarkUnknown()
        {
            data.status = "Unknown";
            Update(data);
        }
    }

    public sealed class SessionWindow
    {
        public readonly Window Window;
        public readonly DataGrid Grid;
        public readonly ObservableCollection<SessionRow> Rows = new ObservableCollection<SessionRow>();
        private readonly ICollectionView view;
        private DateTime lastUpdate = DateTime.UtcNow;
        private volatile bool closed;
        private bool applying;
        private readonly string preferencesPath;
        private double comfortableWidth = 900, comfortableHeight = 530;
        private string settingsError;
        public bool IsCompact { get; private set; }
        public int UnreadCount { get { return Rows.Count(row => row.Unread); } }

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

        public SessionWindow(string settingsPath = null)
        {
            preferencesPath = settingsPath ?? Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "CopilotAgentNotify", "gadget-ui.json");
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("SessionWindow.xaml"))
                Window = (Window)XamlReader.Load(stream);
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("sessions.png"))
            {
                var icon = new BitmapImage();
                icon.BeginInit();
                icon.CacheOption = BitmapCacheOption.OnLoad;
                icon.StreamSource = stream;
                icon.EndInit();
                icon.Freeze();
                Window.Icon = icon;
                ((Image)Window.FindName("AppLogo")).Source = icon;
            }
            Window.TaskbarItemInfo = new TaskbarItemInfo();
            Grid = (DataGrid)Window.FindName("Sessions");
            view = CollectionViewSource.GetDefaultView(Rows);
            Grid.ItemsSource = view;
            Sort(Grid.Columns[2], ListSortDirection.Ascending);
            Grid.Sorting += delegate(object sender, DataGridSortingEventArgs e)
            {
                e.Handled = true;
                Sort(e.Column, e.Column.SortDirection == ListSortDirection.Ascending
                    ? ListSortDirection.Descending : ListSortDirection.Ascending);
            };
            Grid.SelectionChanged += delegate
            {
                if (!applying) MarkSelectedRead();
                UpdateDetail();
            };
            Grid.PreviewMouseLeftButtonUp += delegate(object sender, MouseButtonEventArgs e)
            {
                var origin = e.OriginalSource as DependencyObject;
                if (origin != null && ItemsControl.ContainerFromElement(Grid, origin) is DataGridRow)
                    MarkSelectedRead();
            };
            Grid.PreviewKeyDown += delegate(object sender, KeyEventArgs e)
            {
                if (e.Key == Key.Enter || e.Key == Key.Space) MarkSelectedRead();
            };
            ((Button)Window.FindName("MarkAllRead")).Click += delegate { MarkAllRead(); };
            ToggleButton pin = (ToggleButton)Window.FindName("Pin");
            pin.Checked += delegate { Window.Topmost = true; };
            pin.Unchecked += delegate { Window.Topmost = false; };
            ToggleButton compact = (ToggleButton)Window.FindName("Compact");
            compact.IsChecked = LoadCompact();
            SetCompact(compact.IsChecked == true, false);
            compact.Checked += delegate { SetCompact(true); };
            compact.Unchecked += delegate { SetCompact(false); };
            Window.SourceInitialized += delegate
            {
                int dark = 1;
                // Cosmetic only: older Windows versions can reject this attribute.
                DwmSetWindowAttribute(new WindowInteropHelper(Window).Handle, 20, ref dark, sizeof(int));
            };
            Window.Closed += delegate { closed = true; };
        }

        private bool LoadCompact()
        {
            try
            {
                if (!File.Exists(preferencesPath)) return false;
                var values = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(preferencesPath));
                if (values == null || !values.ContainsKey("compact") || !(values["compact"] is bool))
                    throw new ArgumentException("Expected a boolean compact preference.");
                return (bool)values["compact"];
            }
            catch (IOException error) { settingsError = "Cannot load layout: " + error.Message; }
            catch (UnauthorizedAccessException error) { settingsError = "Cannot load layout: " + error.Message; }
            catch (ArgumentException error) { settingsError = "Invalid layout preference: " + error.Message; }
            catch (InvalidOperationException error) { settingsError = "Invalid layout preference: " + error.Message; }
            return false;
        }

        public void SetCompact(bool compact, bool save = true)
        {
            if (compact && !IsCompact)
            {
                comfortableWidth = Window.Width;
                comfortableHeight = Window.Height;
            }
            IsCompact = compact;
            Window.Resources["DetailVisibility"] = compact ? Visibility.Collapsed : Visibility.Visible;
            Window.Resources["SessionRowHeight"] = compact ? 38.0 : 70.0;
            Window.Resources["CellPadding"] = new Thickness(compact ? 9 : 18, 0, compact ? 9 : 18, 0);
            Window.Resources["HeaderPadding"] = new Thickness(compact ? 9 : 18, compact ? 8 : 13,
                                                             compact ? 9 : 18, compact ? 8 : 13);
            Element("RootLayout").Margin = compact ? new Thickness(12) : new Thickness(24, 20, 24, 16);
            Element("HeaderLayout").Margin = new Thickness(0, 0, 0, compact ? 12 : 22);
            Element("BrandIcon").Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
            ((TextBlock)Window.FindName("Heading")).FontSize = compact ? 18 : 25;
            ((ToggleButton)Window.FindName("Pin")).Content = compact ? "Pin" : "Always on top";
            Grid.Columns[1].Width = compact ? 112 : 155;
            Grid.Columns[2].Width = compact ? 124 : 156;
            Grid.Columns[0].MinWidth = compact ? 100 : 170;
            Grid.Columns[3].Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
            Window.MinWidth = compact ? 420 : 640;
            Window.MinHeight = compact ? 230 : 380;
            Window.Width = compact ? 480 : comfortableWidth;
            Window.Height = compact ? 300 : comfortableHeight;
            if (!save) return;
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(preferencesPath));
                string scratch = preferencesPath + "." + Guid.NewGuid().ToString("N");
                try
                {
                    File.WriteAllText(scratch, new JavaScriptSerializer().Serialize(new { compact = compact }));
                    if (File.Exists(preferencesPath)) File.Replace(scratch, preferencesPath, null);
                    else File.Move(scratch, preferencesPath);
                }
                finally { if (File.Exists(scratch)) File.Delete(scratch); }
                settingsError = null;
            }
            catch (IOException error) { settingsError = "Cannot save layout: " + error.Message; }
            catch (UnauthorizedAccessException error) { settingsError = "Cannot save layout: " + error.Message; }
            if (settingsError != null) SetErrors(new string[0]);
        }

        public void MarkSelectedRead()
        {
            var row = Grid.SelectedItem as SessionRow;
            if (row != null) row.MarkRead();
            UpdateBadge();
        }
        public void MarkAllRead()
        {
            foreach (SessionRow row in Rows) row.MarkRead();
            UpdateBadge();
        }
        private void UpdateBadge()
        {
            int count = UnreadCount;
            Window.TaskbarItemInfo.Description = count == 0 ? "Copilot Sessions" : count + " unread completed sessions";
            Window.TaskbarItemInfo.Overlay = count == 0 ? null : CreateBadge(count);
            ((Button)Window.FindName("MarkAllRead")).Content = count == 0 ? "All read" : "Mark all read (" + count + ")";
            ((Button)Window.FindName("MarkAllRead")).IsEnabled = count > 0;
        }
        public static ImageSource CreateBadge(int count)
        {
            var drawing = new DrawingVisual();
            using (DrawingContext context = drawing.RenderOpen())
            {
                context.DrawEllipse(new SolidColorBrush(Color.FromRgb(229, 77, 103)),
                                    new Pen(Brushes.White, 1), new Point(16, 16), 15, 15);
                string label = count > 99 ? "99+" : count.ToString(CultureInfo.InvariantCulture);
                var text = new FormattedText(label, CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
                    new Typeface(new FontFamily("Segoe UI"), FontStyles.Normal, FontWeights.Bold, FontStretches.Normal),
                    label.Length == 1 ? 23 : label.Length == 2 ? 19 : 14, Brushes.White, 1);
                context.DrawText(text, new Point((32 - text.Width) / 2, (32 - text.Height) / 2 - 1));
            }
            var image = new RenderTargetBitmap(32, 32, 96, 96, PixelFormats.Pbgra32);
            image.Render(drawing);
            image.Freeze();
            return image;
        }

        public void Sort(DataGridColumn column, ListSortDirection direction)
        {
            applying = true;
            try
            {
            using (view.DeferRefresh())
            {
                view.SortDescriptions.Clear();
                view.SortDescriptions.Add(new SortDescription(column.SortMemberPath, direction));
                if (column.SortMemberPath != "Title")
                    view.SortDescriptions.Add(new SortDescription("Title", ListSortDirection.Ascending));
                view.SortDescriptions.Add(new SortDescription("Key", ListSortDirection.Ascending));
                foreach (DataGridColumn other in Grid.Columns) other.SortDirection = null;
                column.SortDirection = direction;
            }
            }
            finally { applying = false; }
        }

        public void Apply(Snapshot snapshot)
        {
            if (snapshot == null || snapshot.rows == null || snapshot.errors == null)
                throw new ArgumentException("Invalid collector snapshot.");
            foreach (SessionData row in snapshot.rows)
                if (row == null || String.IsNullOrEmpty(row.id) || String.IsNullOrEmpty(row.source)
                    || row.title == null || row.cwd == null
                    || !new[] { "In progress", "Needs input", "Done", "Loading", "Unknown" }.Contains(row.status))
                    throw new ArgumentException("Invalid collector session.");
            lastUpdate = DateTime.UtcNow;
            var incoming = snapshot.rows.ToDictionary(row => row.source + "/" + row.id);
            var existing = Rows.ToDictionary(row => row.Key);
            SessionRow selected = Grid.SelectedItem as SessionRow;
            applying = true;
            try
            {
            foreach (SessionRow row in Rows.ToArray())
                if (!incoming.ContainsKey(row.Key)) Rows.Remove(row);
            foreach (var item in incoming)
            {
                SessionRow row;
                if (existing.TryGetValue(item.Key, out row)) row.Update(item.Value);
                else Rows.Add(new SessionRow(item.Value));
            }
            // Property updates can change a sort key without changing collection membership.
            view.Refresh();
            if (selected != null && Rows.Contains(selected)) Grid.SelectedItem = selected;
            }
            finally { applying = false; }
            UpdateBadge();
            Text("BusyCount", Rows.Count(row => row.Status == "In progress").ToString());
            Text("AttentionCount", Rows.Count(row => row.Status == "Needs input").ToString());
            Text("DoneCount", Rows.Count(row => row.Status == "Done").ToString());
            Element("EmptyState").Visibility = Rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            Text("EmptyTitle", snapshot.discovering ? "Connecting to your agents" :
                 snapshot.errors.Length > 0 ? "Session discovery unavailable" : "All quiet. No open sessions.");
            SetErrors(snapshot.errors);
            Text("Connection", snapshot.discovering ? "Connecting" :
                 snapshot.errors.Length > 0 ? "Check connection" : "Live \u00B7 " + Rows.Count + " sessions");
            UpdateDetail();
        }

        private FrameworkElement Element(string name) { return (FrameworkElement)Window.FindName(name); }
        private void Text(string name, string value) { ((TextBlock)Window.FindName(name)).Text = value; }
        private void UpdateDetail()
        {
            SessionRow row = Grid.SelectedItem as SessionRow;
            string detail = row == null ? "Click a column to sort \u00B7 click again to reverse" :
                row.Id + "  \u00B7  PID " + row.Pid + "  \u00B7  " + row.Cwd;
            Text("Detail", detail);
            Element("Detail").ToolTip = detail;
        }
        private void SetErrors(string[] errors)
        {
            if (settingsError != null) errors = errors.Concat(new[] { settingsError }).ToArray();
            Text("ErrorText", String.Join("\n", errors.Take(3)));
            Element("ErrorText").ToolTip = String.Join("\n", errors);
            Element("ErrorPanel").Visibility = errors.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
            ((System.Windows.Shapes.Ellipse)Window.FindName("LiveDot")).Fill =
                new SolidColorBrush((Color)ColorConverter.ConvertFromString(errors.Length == 0 ? "#72C9AF" : "#F3CF87"));
        }
        private void Disconnected(string error)
        {
            foreach (SessionRow row in Rows) row.MarkUnknown();
            view.Refresh();
            Text("BusyCount", "0");
            Text("AttentionCount", "0");
            Text("DoneCount", "0");
            Text("Connection", "Disconnected");
            SetErrors(new[] { error });
        }

        public void ReadCollector()
        {
            var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            timer.Tick += delegate
            {
                if (DateTime.UtcNow - lastUpdate > TimeSpan.FromSeconds(15))
                    Disconnected("The session collector is not responding.");
            };
            timer.Start();
            Window.Closed += delegate { timer.Stop(); };
            new Thread(delegate()
            {
                try
                {
                    string line;
                    var serializer = new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024 };
                    while (!closed && (line = Console.ReadLine()) != null)
                    {
                        Snapshot snapshot = serializer.Deserialize<Snapshot>(line);
                        Window.Dispatcher.Invoke(new Action(delegate
                        {
                            if (!closed) Apply(snapshot);
                        }));
                    }
                    if (!closed) Window.Dispatcher.BeginInvoke(new Action(Window.Close));
                }
                catch (IOException error) { Report(error.Message); }
                catch (ArgumentException error) { Report(error.Message); }
                catch (InvalidOperationException error) { Report(error.Message); }
            }) { IsBackground = true, Name = "Session snapshots" }.Start();
        }
        private void Report(string error)
        {
            if (!closed && !Window.Dispatcher.HasShutdownStarted)
                Window.Dispatcher.BeginInvoke(new Action(delegate { Disconnected(error); }));
        }

        [STAThread]
        public static void Main()
        {
            var app = new Application();
            var window = new SessionWindow();
            window.Window.Loaded += delegate { window.ReadCollector(); };
            app.Run(window.Window);
        }
    }
}
