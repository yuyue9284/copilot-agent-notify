using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Input;
using System.Windows.Shell;
using System.Windows.Threading;

namespace CopilotSessions
{
    public sealed class SessionRow : INotifyPropertyChanged
    {
        private SessionData data;
        private bool working;
        public bool Unread { get; private set; }
        public SessionRow(SessionData value, bool resurfaced = false)
        {
            data = value.Clone();
            working = value.status == "In progress" || value.status == "Needs input";
            Unread = resurfaced && value.status == "Done";
        }
        public string Key { get { return Source + "/" + Id; } }
        public string Id { get { return data.id; } }
        public string ShortId { get { return Id.Substring(0, Math.Min(8, Id.Length)); } }
        public string Title { get { return data.title; } }
        public string Source { get { return data.source; } }
        public string Cwd { get { return data.cwd; } }
        public string Status { get { return data.status; } }
        public string ActivityRevision { get { return data.activity_revision; } }
        public bool CanForget { get { return ActivityRevision != null && Status != "Loading" && Status != "Unknown"; } }
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
            data = value.Clone();
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
        internal const string DefaultInterfaceFontFamily = "Segoe UI";
        internal const int DefaultInterfaceFontSize = 13;
        private static readonly string[] InstalledInterfaceFonts = Fonts.SystemFontFamilies
            .Select(family => family.Source).Concat(new[] { DefaultInterfaceFontFamily })
            .Where(name => !String.IsNullOrWhiteSpace(name))
            .Distinct(StringComparer.CurrentCultureIgnoreCase)
            .OrderBy(name => name, StringComparer.CurrentCultureIgnoreCase).ToArray();
        public readonly Window Window;
        public readonly DataGrid Grid;
        public readonly ObservableCollection<SessionRow> Rows = new ObservableCollection<SessionRow>();
        private readonly ICollectionView view;
        private DateTime lastUpdate = DateTime.UtcNow;
        private volatile bool closed;
        private bool applying;
        private readonly string preferencesPath;
        private readonly string forgottenPath;
        private Dictionary<string, string> forgotten = new Dictionary<string, string>();
        private Snapshot latestSnapshot;
        private string forgottenError;
        private double comfortableWidth = 900, comfortableHeight = 530;
        private string settingsError;
        private bool updatingPreferences;
        private IInputElement appearanceReturnFocus;
        public bool IsCompact { get; private set; }
        internal AppearancePreference Appearance { get; private set; }
        internal int OpacityPercent { get; private set; }
        internal string InterfaceFontFamily { get; private set; }
        internal int InterfaceFontSize { get; private set; }
        public int UnreadCount { get { return Rows.Count(row => row.Unread); } }

        internal readonly WindowBackdrop Backdrop;

        public SessionWindow(string settingsPath = null)
        {
            preferencesPath = settingsPath ?? Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "CopilotAgentNotify", "gadget-ui.json");
            forgottenPath = Path.Combine(Path.GetDirectoryName(preferencesPath), "gadget-forgotten.json");
            LoadForgotten();
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
            ((Button)Window.FindName("Forget")).Click += delegate { ForgetSelected(); };
            ToggleButton pin = (ToggleButton)Window.FindName("Pin");
            pin.Checked += delegate { Window.Topmost = true; };
            pin.Unchecked += delegate { Window.Topmost = false; };
            ToggleButton compact = (ToggleButton)Window.FindName("Compact");
            Appearance = AppearancePreference.Auto;
            OpacityPercent = 90;
            InterfaceFontFamily = DefaultInterfaceFontFamily;
            InterfaceFontSize = DefaultInterfaceFontSize;
            bool initialCompact = LoadPreferences();
            Backdrop = new WindowBackdrop(Window);
            Backdrop.Configure(Appearance, OpacityPercent);
            InitializeAppearance();
            ApplyInterfaceFont();
            SetCompact(initialCompact, false);
            compact.Checked += delegate { SetCompact(true); };
            compact.Unchecked += delegate { SetCompact(false); };
            Window.Closed += delegate { closed = true; };
        }

        private bool LoadPreferences()
        {
            try
            {
                if (!File.Exists(preferencesPath)) return false;
                var values = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(preferencesPath));
                if (values == null || !values.ContainsKey("compact") || !(values["compact"] is bool))
                    throw new ArgumentException("Expected a boolean compact preference.");
                AppearancePreference appearance = AppearancePreference.Auto;
                int opacity = 90;
                string fontFamily = DefaultInterfaceFontFamily;
                int fontSize = DefaultInterfaceFontSize;
                if (values.ContainsKey("appearance"))
                {
                    string mode = values["appearance"] as string;
                    int index = Array.IndexOf(new[] { "auto", "acrylic", "translucent", "solid" }, mode);
                    if (index < 0) throw new ArgumentException("Expected appearance: auto, acrylic, translucent, or solid.");
                    appearance = (AppearancePreference)index;
                }
                if (values.ContainsKey("opacity"))
                {
                    if (!(values["opacity"] is int) || (int)values["opacity"] < 50 || (int)values["opacity"] > 100)
                        throw new ArgumentException("Expected an integer opacity percentage from 50 to 100.");
                    opacity = (int)values["opacity"];
                }
                if (values.ContainsKey("font_family"))
                {
                    string requested = values["font_family"] as string;
                    fontFamily = InstalledInterfaceFonts.FirstOrDefault(
                        name => String.Equals(name, requested, StringComparison.CurrentCultureIgnoreCase));
                    if (fontFamily == null)
                        throw new ArgumentException("Expected font_family to name an installed font.");
                }
                if (values.ContainsKey("font_size"))
                {
                    if (!(values["font_size"] is int) || (int)values["font_size"] < 10 || (int)values["font_size"] > 18)
                        throw new ArgumentException("Expected an integer font_size from 10 to 18.");
                    fontSize = (int)values["font_size"];
                }
                Appearance = appearance;
                OpacityPercent = opacity;
                InterfaceFontFamily = fontFamily;
                InterfaceFontSize = fontSize;
                return (bool)values["compact"];
            }
            catch (IOException error) { settingsError = "Cannot load preferences: " + error.Message; }
            catch (UnauthorizedAccessException error) { settingsError = "Cannot load preferences: " + error.Message; }
            catch (ArgumentException error) { settingsError = "Invalid preferences: " + error.Message; }
            catch (InvalidOperationException error) { settingsError = "Invalid preferences: " + error.Message; }
            return false;
        }

        private void InitializeAppearance()
        {
            var button = (Button)Window.FindName("AppearanceButton");
            var menu = button.ContextMenu;
            var slider = (Slider)Window.FindName("AppearanceOpacity");
            var fontFamily = (ComboBox)Window.FindName("InterfaceFontFamily");
            var fontSize = (Slider)Window.FindName("InterfaceFontSize");
            fontFamily.ItemsSource = InstalledInterfaceFonts;
            button.PreviewMouseDown += delegate { appearanceReturnFocus = Keyboard.FocusedElement; };
            button.Click += delegate
            {
                if (appearanceReturnFocus == null) appearanceReturnFocus = Keyboard.FocusedElement;
                menu.PlacementTarget = button;
                menu.IsOpen = true;
            };
            menu.Opened += delegate
            {
                foreach (MenuItem item in menu.Items.OfType<MenuItem>())
                    if (item.IsChecked) { item.Focus(); break; }
            };
            menu.Closed += delegate
            {
                IInputElement target = appearanceReturnFocus ?? button;
                appearanceReturnFocus = null;
                Window.Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(delegate
                {
                    if (!closed) Keyboard.Focus(target);
                }));
            };
            menu.PreviewKeyDown += delegate(object sender, KeyEventArgs args)
            {
                if (args.Key == Key.Tab)
                {
                    var controls = new List<Control> {
                        menu.Items.OfType<MenuItem>().First(item => item.IsChecked)
                    };
                    if (slider.IsEnabled) controls.Add(slider);
                    controls.Add(fontFamily);
                    controls.Add(fontSize);
                    int current = controls.FindIndex(control => control.IsKeyboardFocusWithin);
                    int direction = (Keyboard.Modifiers & ModifierKeys.Shift) != 0 ? -1 : 1;
                    int next = current < 0 ? 0 : (current + direction + controls.Count) % controls.Count;
                    controls[next].Focus();
                    args.Handled = true;
                }
                else if (args.Key == Key.Escape) { menu.IsOpen = false; args.Handled = true; }
            };
            foreach (MenuItem item in menu.Items.OfType<MenuItem>().Where(item => item.Tag != null))
                item.Click += delegate(object sender, RoutedEventArgs args)
                {
                    var choice = (MenuItem)sender;
                    ChangeAppearance((AppearancePreference)Enum.Parse(typeof(AppearancePreference), (string)choice.Tag),
                                     OpacityPercent);
                    args.Handled = true;
                };
            slider.ValueChanged += delegate
            {
                if (updatingPreferences) return;
                int percent = (int)Math.Round(slider.Value, MidpointRounding.AwayFromZero);
                if (percent != OpacityPercent) ChangeAppearance(Appearance, percent);
                else SynchronizePreferenceControls();
            };
            fontFamily.SelectionChanged += delegate
            {
                if (updatingPreferences) return;
                string selected = fontFamily.SelectedItem as string;
                if (selected != null && !String.Equals(selected, InterfaceFontFamily, StringComparison.Ordinal))
                    ChangeInterfaceFont(selected, InterfaceFontSize);
                else SynchronizePreferenceControls();
            };
            fontSize.ValueChanged += delegate
            {
                if (updatingPreferences) return;
                int size = (int)Math.Round(fontSize.Value, MidpointRounding.AwayFromZero);
                if (size != InterfaceFontSize) ChangeInterfaceFont(InterfaceFontFamily, size);
                else SynchronizePreferenceControls();
            };
        }

        private void ChangeAppearance(AppearancePreference appearance, int opacity)
        {
            if (updatingPreferences) return;
            if (SavePreferences(IsCompact, appearance, opacity, InterfaceFontFamily, InterfaceFontSize))
            {
                Appearance = appearance;
                OpacityPercent = opacity;
                Backdrop.Configure(appearance, opacity);
            }
            SynchronizePreferenceControls();
        }

        private void ChangeInterfaceFont(string family, int size)
        {
            if (updatingPreferences) return;
            if (SavePreferences(IsCompact, Appearance, OpacityPercent, family, size))
            {
                InterfaceFontFamily = family;
                InterfaceFontSize = size;
                ApplyInterfaceFont();
            }
            SynchronizePreferenceControls();
        }

        private void ApplyInterfaceFont()
        {
            var family = new FontFamily(InterfaceFontFamily);
            Window.FontFamily = family;
            Window.FontSize = InterfaceFontSize;
            var menu = ((Button)Window.FindName("AppearanceButton")).ContextMenu;
            menu.FontFamily = family;
            menu.FontSize = InterfaceFontSize;
            Window.Resources["SmallFontSize"] = (double)Math.Max(9, InterfaceFontSize - 2);
            Window.Resources["SummaryFontSize"] = (double)(InterfaceFontSize + 11);
            Window.Resources["EmptyTitleFontSize"] = (double)(InterfaceFontSize + 2);
            Window.Resources["EmptyIconFontSize"] = (double)(InterfaceFontSize + 17);
            Window.Resources["HeadingFontSize"] = (double)(InterfaceFontSize + (IsCompact ? 5 : 12));
            UpdateMinimumSize();
        }

        private void UpdateMinimumSize()
        {
            int growth = Math.Max(0, InterfaceFontSize - DefaultInterfaceFontSize);
            Window.MinWidth = (IsCompact ? 420 : 640) + growth * (IsCompact ? 18 : 24);
            Window.MinHeight = (IsCompact ? 230 : 380) + growth * 8;
        }

        private void SynchronizePreferenceControls()
        {
            updatingPreferences = true;
            try
            {
                ((ToggleButton)Window.FindName("Compact")).IsChecked = IsCompact;
                var menu = ((Button)Window.FindName("AppearanceButton")).ContextMenu;
                foreach (MenuItem item in menu.Items.OfType<MenuItem>().Where(item => item.Tag != null))
                    item.IsChecked = String.Equals((string)item.Tag, Appearance.ToString(), StringComparison.Ordinal);
                var slider = (Slider)Window.FindName("AppearanceOpacity");
                slider.Value = OpacityPercent;
                slider.IsEnabled = Appearance == AppearancePreference.Auto || Appearance == AppearancePreference.Translucent;
                ((TextBlock)Window.FindName("OpacityLabel")).Text = "Translucent opacity: " + OpacityPercent + "%";
                ((ComboBox)Window.FindName("InterfaceFontFamily")).SelectedItem = InterfaceFontFamily;
                var fontSize = (Slider)Window.FindName("InterfaceFontSize");
                fontSize.Value = InterfaceFontSize;
                ((TextBlock)Window.FindName("FontSizeLabel")).Text = "Font size: " + InterfaceFontSize;
            }
            finally { updatingPreferences = false; }
        }

        private void LoadForgotten()
        {
            try
            {
                if (!File.Exists(forgottenPath)) return;
                var entries = new JavaScriptSerializer().Deserialize<Dictionary<string, string>>(
                    File.ReadAllText(forgottenPath));
                if (entries == null || entries.Any(entry => String.IsNullOrEmpty(entry.Key) || entry.Value == null))
                    throw new ArgumentException("Expected session identities mapped to activity revisions.");
                forgotten = entries;
            }
            catch (IOException error) { forgottenError = "Cannot load forgotten sessions: " + error.Message; }
            catch (UnauthorizedAccessException error) { forgottenError = "Cannot load forgotten sessions: " + error.Message; }
            catch (ArgumentException error) { forgottenError = "Invalid forgotten sessions: " + error.Message; }
            catch (InvalidOperationException error) { forgottenError = "Invalid forgotten sessions: " + error.Message; }
        }

        private bool SaveForgotten(Dictionary<string, string> entries)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(forgottenPath));
                string scratch = forgottenPath + "." + Guid.NewGuid().ToString("N");
                try
                {
                    File.WriteAllText(scratch, new JavaScriptSerializer().Serialize(entries));
                    if (File.Exists(forgottenPath)) File.Replace(scratch, forgottenPath, null);
                    else File.Move(scratch, forgottenPath);
                }
                finally { if (File.Exists(scratch)) File.Delete(scratch); }
                forgottenError = null;
                return true;
            }
            catch (IOException error) { forgottenError = "Cannot save forgotten sessions: " + error.Message; }
            catch (UnauthorizedAccessException error) { forgottenError = "Cannot save forgotten sessions: " + error.Message; }
            return false;
        }

        public void ForgetSelected()
        {
            var row = Grid.SelectedItem as SessionRow;
            if (row == null || !row.CanForget || latestSnapshot == null) return;
            var updated = new Dictionary<string, string>(forgotten);
            updated[row.Key] = row.ActivityRevision;
            if (!SaveForgotten(updated))
            {
                SetErrors(latestSnapshot.errors);
                return;
            }
            forgotten = updated;
            row.MarkRead();
            Apply(latestSnapshot);
        }

        private static bool NewActivity(string current, string dismissed)
        {
            if (String.IsNullOrEmpty(current) || current == dismissed) return false;
            if (String.IsNullOrEmpty(dismissed)) return true;
            if (current.Length > 20 && dismissed.Length > 20 && current[20] == '/' && dismissed[20] == '/')
                return String.CompareOrdinal(current.Substring(0, 20), dismissed.Substring(0, 20)) >= 0;
            return true;
        }

        public void SetCompact(bool compact, bool save = true)
        {
            if (updatingPreferences) return;
            if (save && !SavePreferences(compact, Appearance, OpacityPercent,
                                         InterfaceFontFamily, InterfaceFontSize))
            {
                SynchronizePreferenceControls();
                return;
            }
            if (compact && !IsCompact)
            {
                comfortableWidth = Window.Width;
                comfortableHeight = Window.Height;
            }
            IsCompact = compact;
            Window.Resources["HeadingFontSize"] = (double)(InterfaceFontSize + (compact ? 5 : 12));
            Window.Resources["DetailVisibility"] = compact ? Visibility.Collapsed : Visibility.Visible;
            Window.Resources["SessionRowHeight"] = compact ? 38.0 : 70.0;
            Window.Resources["CellPadding"] = new Thickness(compact ? 9 : 18, 0, compact ? 9 : 18, 0);
            Window.Resources["HeaderPadding"] = new Thickness(compact ? 9 : 18, compact ? 8 : 13,
                                                             compact ? 9 : 18, compact ? 8 : 13);
            Element("RootLayout").Margin = compact ? new Thickness(12) : new Thickness(24, 20, 24, 16);
            Element("HeaderLayout").Margin = new Thickness(0, 0, 0, compact ? 12 : 22);
            Element("BrandIcon").Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
            ((ToggleButton)Window.FindName("Pin")).Content = compact ? "Pin" : "Always on top";
            Grid.Columns[1].Width = compact ? 112 : 155;
            Grid.Columns[2].Width = compact ? 124 : 156;
            Grid.Columns[0].MinWidth = compact ? 100 : 170;
            Grid.Columns[3].Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
            UpdateMinimumSize();
            Window.Width = compact ? 480 : comfortableWidth;
            Window.Height = compact ? 300 : comfortableHeight;
            SynchronizePreferenceControls();
        }

        private bool SavePreferences(bool compact, AppearancePreference appearance, int opacity,
                                     string fontFamily, int fontSize)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(preferencesPath));
                string scratch = preferencesPath + "." + Guid.NewGuid().ToString("N");
                try
                {
                    File.WriteAllText(scratch, new JavaScriptSerializer().Serialize(
                        new { compact = compact, appearance = appearance.ToString().ToLowerInvariant(),
                              opacity = opacity, font_family = fontFamily, font_size = fontSize }));
                    if (File.Exists(preferencesPath)) File.Replace(scratch, preferencesPath, null);
                    else File.Move(scratch, preferencesPath);
                }
                finally { if (File.Exists(scratch)) File.Delete(scratch); }
                settingsError = null;
            }
            catch (IOException error) { settingsError = "Cannot save preferences: " + error.Message; }
            catch (UnauthorizedAccessException error) { settingsError = "Cannot save preferences: " + error.Message; }
            SetErrors(latestSnapshot == null ? new string[0] : latestSnapshot.errors);
            return settingsError == null;
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
            snapshot = snapshot.Clone();
            lastUpdate = DateTime.UtcNow;
            latestSnapshot = snapshot;
            var incoming = new Dictionary<string, SessionData>();
            var resurfaced = new List<string>();
            foreach (SessionData row in snapshot.rows)
            {
                string key = row.source + "/" + row.id;
                string revision;
                if (forgotten.TryGetValue(key, out revision))
                {
                    // Loading replays old history. Only a complete, healthy
                    // snapshot with new conversation activity can undo Forget.
                    if (row.status == "Loading" || row.status == "Unknown"
                        || !NewActivity(row.activity_revision, revision))
                        continue;
                    resurfaced.Add(key);
                }
                incoming.Add(key, row);
            }
            if (resurfaced.Count > 0)
            {
                var updated = new Dictionary<string, string>(forgotten);
                foreach (string key in resurfaced) updated.Remove(key);
                if (SaveForgotten(updated)) forgotten = updated;
            }
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
                else Rows.Add(new SessionRow(item.Value, resurfaced.Contains(item.Key)));
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
                 snapshot.errors.Length > 0 ? "Session discovery unavailable" :
                 forgotten.Count > 0 ? "All quiet. Forgotten sessions are hidden." : "All quiet. No open sessions.");
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
            ((Button)Window.FindName("Forget")).IsEnabled = row != null && row.CanForget;
            Element("Forget").ToolTip = row != null && !row.CanForget
                ? "Wait for a complete session activity snapshot before forgetting."
                : "Hide this session and clear its badge until new conversation activity. Copilot keeps running.";
        }
        private void SetErrors(string[] errors)
        {
            if (settingsError != null) errors = errors.Concat(new[] { settingsError }).ToArray();
            if (forgottenError != null) errors = errors.Concat(new[] { forgottenError }).ToArray();
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
                    while (!closed && (line = CollectorProcess.ReadLine(Console.In)) != null)
                    {
                        Snapshot snapshot;
                        try { snapshot = SnapshotProtocol.ParseUi(line); }
                        catch (ArgumentException error) { Report(error.Message); continue; }
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
        public static int Main(string[] args)
        {
            return GadgetApplication.Run(args);
        }
    }
}
