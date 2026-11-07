using System;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

internal static class HookClientCapture
{
    private static int Main(string[] args)
    {
        return HookClient.Run(args, delegate(string[] notification) {
            File.AppendAllText(Environment.GetEnvironmentVariable("CAPTURE_FILE"),
                new JavaScriptSerializer().Serialize(notification) + "\n", new UTF8Encoding(false));
            string code = Environment.GetEnvironmentVariable("NOTIFIER_EXIT");
            return String.IsNullOrEmpty(code) ? 0 : Int32.Parse(code);
        });
    }
}
