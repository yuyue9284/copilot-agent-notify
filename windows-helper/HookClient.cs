using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

internal static class HookClient
{
    internal static int Run(string[] args, Func<string[], int> notify)
    {
        try
        {
            if (args.Length != 3 || args[0] != "--hook")
                throw new ArgumentException("Expected --hook activity|notify EVENT.");
            string kind = args[1], hookEvent = args[2];
            if (kind == "activity" && (hookEvent == "sessionStart" ||
                hookEvent == "userPromptSubmitted" || hookEvent == "agentStop" ||
                hookEvent == "sessionEnd"))
                return Activity(hookEvent);
            if (kind == "notify" && (hookEvent == "agentStop" ||
                hookEvent == "subagentStop" || hookEvent == "notification"))
                return Notify(hookEvent, notify);
            throw new ArgumentException("Unsupported hook kind or event.");
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("copilot-notify: " + exception.Message);
            return 1;
        }
    }

    private static string Env(string name)
    {
        return Environment.GetEnvironmentVariable(name) ?? "";
    }

    private static string Home()
    {
        string home = Env("COPILOT_HOME");
        return home.Length > 0 ? home : Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".copilot");
    }

    private static string Input()
    {
        Console.InputEncoding = new UTF8Encoding(false);
        return Console.In.ReadToEnd();
    }

    private static Dictionary<string, object> Object(string json)
    {
        var value = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue }
            .DeserializeObject(json) as Dictionary<string, object>;
        if (value == null)
            throw new FormatException("expected a JSON object");
        return value;
    }

    private static string Text(Dictionary<string, object> value, string key)
    {
        object item;
        return value.TryGetValue(key, out item) ? item as string ?? "" : "";
    }

    private static int Notify(string hookEvent, Func<string[], int> notify)
    {
        string alerts = Env("COPILOT_NOTIFY_HOOK_ALERTS");
        if (alerts.Length == 0)
        {
            alerts = "1";
            string configPath = Path.Combine(Home(), "copilot-agent-notify.json");
            if (File.Exists(configPath))
            {
                try
                {
                    object enabled;
                    if (Object(File.ReadAllText(configPath)).TryGetValue("hook_alerts", out enabled))
                    {
                        if (!(enabled is bool))
                            throw new FormatException("hook_alerts must be true or false");
                        alerts = (bool)enabled ? "1" : "0";
                    }
                }
                catch (Exception exception)
                {
                    throw new FormatException("invalid hook config: " + exception.Message);
                }
            }
        }
        if (alerts == "0")
            return 0;
        if (alerts != "1")
            throw new FormatException("COPILOT_NOTIFY_HOOK_ALERTS must be 0 or 1.");
        var payload = Object(Input());
        if (hookEvent == "subagentStop")
        {
            string subagents = Env("COPILOT_NOTIFY_SUBAGENTS");
            if (subagents.Length == 0 || subagents == "0")
                return 0;
            if (subagents != "1")
                throw new FormatException("COPILOT_NOTIFY_SUBAGENTS must be 0 or 1.");
        }
        string sessionId = Text(payload, "sessionId");
        string transcript = Text(payload, "transcriptPath");
        if (hookEvent == "agentStop" && transcript.Length > 0)
        {
            Dictionary<string, object> header;
            using (var reader = new StreamReader(transcript))
                header = Object(reader.ReadLine());
            object data;
            var session = header.TryGetValue("data", out data)
                ? data as Dictionary<string, object> : null;
            if (Text(header, "type") != "session.start" || session == null ||
                Text(session, "sessionId").Length == 0)
                throw new FormatException("invalid transcript session header.");
            if (sessionId.Length == 0)
                throw new FormatException("agentStop payload is missing sessionId.");
            if (!String.Equals(sessionId, Text(session, "sessionId"), StringComparison.OrdinalIgnoreCase))
                return 0;
        }
        string sessionTitle = "Untitled session";
        string workspace = Path.Combine(Home(), "session-state", sessionId, "workspace.yaml");
        if (File.Exists(workspace))
        {
            foreach (string line in File.ReadLines(workspace))
            {
                if (!line.StartsWith("name: ", StringComparison.Ordinal))
                    continue;
                string value = line.Substring(6);
                if (value.StartsWith("\"") && value.EndsWith("\""))
                    sessionTitle = new JavaScriptSerializer().Deserialize<string>(value);
                else if (value.StartsWith("'") && value.EndsWith("'") && value.Length >= 2)
                    sessionTitle = value.Substring(1, value.Length - 2).Replace("''", "'");
                else
                    sessionTitle = value;
                break;
            }
        }
        sessionTitle = sessionTitle.Replace("\r", " ").Replace("\n", " ");
        if (sessionTitle.Length > 64)
            sessionTitle = sessionTitle.Substring(0, 61) + "...";
        string shortId = sessionId.Substring(0, Math.Min(8, sessionId.Length));
        string agentId = Text(payload, "agentId");
        string shortAgentId = agentId.Substring(0, Math.Min(8, agentId.Length));
        string title = "[Windows] " + sessionTitle;
        if (shortId.Length > 0)
            title += " [#" + shortId + "]";
        string action = "Agent finished responding";
        if (hookEvent == "subagentStop")
        {
            string label = Text(payload, "agentDisplayName");
            if (label.Length == 0) label = Text(payload, "agentName");
            if (label.Length == 0) label = Text(payload, "agentType");
            if (label.Length == 0) label = "Subagent";
            action = "Subagent complete: " + label;
            if (shortAgentId.Length > 0)
                action += " [#" + shortAgentId + "]";
        }
        else
        {
            string notificationType = Text(payload, "notification_type");
            if (notificationType == "permission_prompt" || notificationType == "elicitation_dialog")
            {
                action = Text(payload, "title");
                if (action.Length == 0)
                    action = notificationType == "permission_prompt" ? "Permission needed" : "Input needed";
            }
        }
        string message = action + ".";
        string cwd = Text(payload, "cwd");
        if (cwd.Length > 0)
            message += "\nGo to: " + cwd;
        string tag = shortAgentId.Length > 0 ? shortAgentId : shortId.Length > 0 ? shortId : "copilot";
        return notify(new[] { "--bell", title, message, tag });
    }

    private static string FindPython()
    {
        foreach (string name in new[] { "py.exe", "python3.exe", "python.exe" })
            foreach (string directory in Env("PATH").Split(Path.PathSeparator))
            {
                if (directory.Length == 0)
                    continue;
                string path = Path.Combine(directory.Trim('"'), name);
                if (File.Exists(path))
                    return path;
            }
        throw new FileNotFoundException(
            "Python 3.9+ is required; install it or set COPILOT_NOTIFY_ICONS=0.");
    }

    private static int Activity(string hookEvent)
    {
        if (Env("ZELLIJ_SESSION_NAME").Length == 0 || Env("ZELLIJ_PANE_ID").Length == 0 ||
            Env("COPILOT_NOTIFY_ICONS") == "0")
            return 0;
        string python = FindPython();
        string script = Path.GetFullPath(Path.Combine(
            AppDomain.CurrentDomain.BaseDirectory, "..", "..", "scripts", "activity.py"));
        var start = new ProcessStartInfo(python) {
            UseShellExecute = false,
            RedirectStandardInput = true,
            Arguments = (Path.GetFileName(python).Equals("py.exe", StringComparison.OrdinalIgnoreCase)
                ? "-3 " : "") + "\"" + script + "\" hook " + hookEvent
        };
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        string payload = Input();
        using (var process = Process.Start(start))
        {
            using (var input = new StreamWriter(process.StandardInput.BaseStream, new UTF8Encoding(false)))
                input.Write(payload);
            process.WaitForExit();
            return process.ExitCode;
        }
    }
}
