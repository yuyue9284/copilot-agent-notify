using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Web.Script.Serialization;

namespace CopilotSessions
{
    public enum SessionStatus { Loading, Unknown, NeedsInput, InProgress, Done }

    public static class SessionStatuses
    {
        public static SessionStatus Parse(string value)
        {
            switch (value)
            {
                case "Loading": return SessionStatus.Loading;
                case "Unknown": return SessionStatus.Unknown;
                case "Needs input": return SessionStatus.NeedsInput;
                case "In progress": return SessionStatus.InProgress;
                case "Done": return SessionStatus.Done;
                default: throw new ArgumentException("Invalid session status.");
            }
        }

        public static string Format(SessionStatus value)
        {
            switch (value)
            {
                case SessionStatus.Loading: return "Loading";
                case SessionStatus.Unknown: return "Unknown";
                case SessionStatus.NeedsInput: return "Needs input";
                case SessionStatus.InProgress: return "In progress";
                case SessionStatus.Done: return "Done";
                default: throw new ArgumentException("Invalid session status.");
            }
        }
    }

    public sealed class SessionData
    {
        private SessionStatus state = SessionStatus.Unknown;
        public string id { get; set; }
        public string title { get; set; }
        public string source { get; set; }
        public string cwd { get; set; }
        public string status { get { return SessionStatuses.Format(state); } set { state = SessionStatuses.Parse(value); } }
        public string activity_revision { get; set; }
        public string latest_activity { get; set; }
        public int pid { get; set; }
        [ScriptIgnore]
        public SessionStatus State { get { return state; } set { SessionStatuses.Format(value); state = value; } }
        public SessionData Clone() { return (SessionData)MemberwiseClone(); }
    }

    public sealed class Snapshot
    {
        public SessionData[] rows { get; set; }
        public string[] errors { get; set; }
        public bool discovering { get; set; }
        public Snapshot Clone()
        {
            return new Snapshot { rows = rows.Select(row => row.Clone()).ToArray(),
                errors = (string[])errors.Clone(), discovering = discovering };
        }
    }

    public sealed class CollectorSnapshot
    {
        private readonly SessionData[] sessions;
        private readonly string[] errors;
        public CollectorSnapshot(IEnumerable<SessionData> rows, IEnumerable<string> messages)
        {
            sessions = rows.Select(row => row.Clone()).ToArray();
            errors = messages.ToArray();
        }
        public SessionData[] Sessions { get { return sessions.Select(row => row.Clone()).ToArray(); } }
        public string[] Errors { get { return (string[])errors.Clone(); } }
        public static CollectorSnapshot Failure(string error)
        {
            return new CollectorSnapshot(new SessionData[0], new[] { error });
        }
    }

    public static class SnapshotProtocol
    {
        public const int MaxLineLength = 8 * 1024 * 1024;
        internal static Dictionary<string, object> Object(string json)
        {
            if (json == null || json.Length > MaxLineLength) throw new ArgumentException("Invalid JSON message length.");
            object value = new JavaScriptSerializer { MaxJsonLength = MaxLineLength, RecursionLimit = 32 }.DeserializeObject(json);
            var result = value as Dictionary<string, object>;
            if (result == null) throw new ArgumentException("Expected a JSON object.");
            return result;
        }
        private static object Required(Dictionary<string, object> data, string key)
        {
            object value;
            if (!data.TryGetValue(key, out value)) throw new ArgumentException("Missing field: " + key);
            return value;
        }
        private static string Text(Dictionary<string, object> data, string key, bool nullable)
        {
            object value = Required(data, key);
            if (nullable && value == null) return null;
            if (!(value is string)) throw new ArgumentException("Expected a string: " + key);
            return (string)value;
        }
        private static object[] Array(Dictionary<string, object> data, string key)
        {
            var values = Required(data, key) as object[];
            if (values == null) throw new ArgumentException("Expected an array: " + key);
            return values;
        }
        private static string[] Errors(Dictionary<string, object> data)
        {
            return Array(data, "errors").Select(value =>
            {
                if (!(value is string)) throw new ArgumentException("Expected string errors.");
                return (string)value;
            }).ToArray();
        }
        private static SessionData[] Rows(Dictionary<string, object> data, string key, string source, bool legacy)
        {
            var identities = new HashSet<string>(StringComparer.Ordinal);
            return Array(data, key).Select(value =>
            {
                var row = value as Dictionary<string, object>;
                if (row == null) throw new ArgumentException("Expected a session object.");
                object pid = Required(row, "pid");
                if (!(pid is int) || (int)pid <= 0) throw new ArgumentException("Expected a positive integer pid.");
                string revision = legacy && !row.ContainsKey("activity_revision") ? null : Text(row, "activity_revision", true);
                var session = new SessionData { id = Text(row, "id", false), pid = (int)pid,
                    title = Text(row, "title", false), cwd = Text(row, "cwd", false),
                    status = Text(row, "status", false), activity_revision = revision,
                    latest_activity = row.ContainsKey("latest_activity") ? Text(row, "latest_activity", false) : "",
                    source = source ?? Text(row, "source", false) };
                if (session.latest_activity.Length > 240)
                    throw new ArgumentException("Latest activity exceeds 240 characters.");
                if (String.IsNullOrEmpty(session.id) || String.IsNullOrEmpty(session.source))
                    throw new ArgumentException("Session id and source must not be empty.");
                if (!identities.Add(session.source + "/" + session.id)) throw new ArgumentException("Duplicate session identity.");
                return session;
            }).ToArray();
        }
        public static CollectorSnapshot ParseCollector(string json)
        {
            var data = Object(json);
            object version = Required(data, "protocol_version");
            if (!(version is int) || (int)version != 1) throw new ArgumentException("Unsupported collector protocol_version; expected integer 1.");
            return new CollectorSnapshot(Rows(data, "sessions", "collector", false), Errors(data));
        }
        public static Snapshot ParseUi(string json)
        {
            var data = Object(json);
            object discovering = Required(data, "discovering");
            if (!(discovering is bool)) throw new ArgumentException("Expected boolean discovering.");
            return new Snapshot { rows = Rows(data, "rows", null, true), errors = Errors(data), discovering = (bool)discovering };
        }
    }

    public sealed class GadgetConfiguration
    {
        private readonly string[] distros;
        public GadgetConfiguration(string[] allowed)
        {
            if (allowed != null && allowed.Any(name => String.IsNullOrWhiteSpace(name) || name != name.Trim()))
                throw new ArgumentException("wsl_distros must be null or a list of nonempty distribution names.");
            distros = allowed == null ? null : (string[])allowed.Clone();
        }
        public bool WslDisabled { get { return distros != null && distros.Length == 0; } }
        public string[] SelectRunning(IEnumerable<string> running)
        {
            return running.Where(name => distros == null || distros.Contains(name, StringComparer.OrdinalIgnoreCase))
                .Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        }
        public static GadgetConfiguration Parse(string json)
        {
            var data = SnapshotProtocol.Object(json);
            if (data.Keys.Any(key => key != "wsl_distros")) throw new ArgumentException("Expected an object with only wsl_distros.");
            object value;
            if (!data.TryGetValue("wsl_distros", out value) || value == null) return new GadgetConfiguration(null);
            var values = value as object[];
            if (values == null || values.Any(item => !(item is string)))
                throw new ArgumentException("wsl_distros must be null or a list of nonempty distribution names.");
            return new GadgetConfiguration(values.Cast<string>().ToArray());
        }
        public static GadgetConfiguration Load(string path)
        {
            try { return Parse(File.ReadAllText(path)); }
            catch (FileNotFoundException) { return new GadgetConfiguration(null); }
            catch (DirectoryNotFoundException) { return new GadgetConfiguration(null); }
        }
    }

    public sealed class SourceAggregator
    {
        private sealed class Source
        {
            public CollectorSnapshot Snapshot;
            public double Stamp;
            public bool Connecting;
        }
        private readonly Dictionary<string, Source> sources = new Dictionary<string, Source>(StringComparer.OrdinalIgnoreCase);
        public void Connecting(string name, double now)
        {
            if (!sources.ContainsKey(name)) sources[name] = new Source {
                Snapshot = CollectorSnapshot.Failure("Connecting..."), Stamp = now, Connecting = true };
        }
        public void Update(string name, CollectorSnapshot snapshot, double now)
        {
            if (snapshot == null) { sources.Remove(name); return; }
            Source old;
            if (snapshot.Errors.Length != 0 && snapshot.Sessions.Length == 0 && sources.TryGetValue(name, out old))
            {
                var rows = old.Snapshot.Sessions;
                foreach (var row in rows) row.State = SessionStatus.Unknown;
                snapshot = new CollectorSnapshot(rows, snapshot.Errors);
            }
            sources[name] = new Source { Snapshot = snapshot, Stamp = now };
        }
        public Snapshot Display(double now)
        {
            var rows = new List<SessionData>();
            var errors = new List<string>();
            foreach (var item in sources)
            {
                bool stale = now - item.Value.Stamp > 15 && item.Key != "WSL discovery"
                    && item.Key != "Configuration" && item.Key != "Collector shutdown";
                if (stale) errors.Add(item.Key + ": collector not responding");
                errors.AddRange(item.Value.Snapshot.Errors.Select(error => item.Key + ": " + error));
                foreach (var row in item.Value.Snapshot.Sessions)
                {
                    row.source = item.Key;
                    if (stale) row.State = SessionStatus.Unknown;
                    rows.Add(row);
                }
            }
            return new Snapshot { rows = rows.OrderBy(row => Rank(row.State)).ThenBy(row => row.source, StringComparer.Ordinal)
                .ThenBy(row => row.title, StringComparer.Ordinal).ThenBy(row => row.id, StringComparer.Ordinal).ToArray(),
                errors = errors.ToArray(), discovering = sources.Count == 0 || sources.Values.All(source => source.Connecting) };
        }
        private static int Rank(SessionStatus state)
        {
            switch (state)
            {
                case SessionStatus.NeedsInput: return 0;
                case SessionStatus.InProgress: return 1;
                case SessionStatus.Loading: return 2;
                case SessionStatus.Unknown: return 3;
                default: return 4;
            }
        }
    }
}
