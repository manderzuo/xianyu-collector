using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class LauncherText
{
    internal const string Product = "\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Installer = "\u5b89\u88c5\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Updater = "\u66f4\u65b0\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Stopper = "\u505c\u6b62\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Diagnostics = "\u8bca\u65ad\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Starting = "\u6b63\u5728\u542f\u52a8\u670d\u52a1...";
    internal const string Ready = "\u5c31\u7eea";
    internal const string Failed = "\u64cd\u4f5c\u5931\u8d25";
}

internal sealed class LauncherForm : Form
{
    private readonly string packageRoot;
    private readonly string appRoot;
    private readonly Label statusLabel;
    private readonly Label versionLabel;
    private readonly RichTextBox logBox;
    private readonly Button startButton;
    private readonly Button updateButton;
    private readonly Button stopButton;
    private readonly Button diagnosticsButton;
    private readonly Button openButton;
    private bool busy;
    private readonly string autoScript;
    private readonly string autoArguments;
    private readonly string autoStatus;

    internal LauncherForm(string autoScript, string autoArguments, string autoStatus)
    {
        this.autoScript = autoScript;
        this.autoArguments = autoArguments;
        this.autoStatus = autoStatus;
        packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        appRoot = Path.Combine(packageRoot, "app");

        Text = LauncherText.Product;
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(720, 480);
        ClientSize = new Size(820, 560);
        BackColor = Color.FromArgb(15, 23, 42);
        ForeColor = Color.FromArgb(241, 245, 249);
        Font = new Font("Microsoft YaHei UI", 10F);
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;

        var header = new Panel { Dock = DockStyle.Top, Height = 118, Padding = new Padding(30, 22, 30, 10) };
        Controls.Add(header);

        var title = new Label {
            Text = LauncherText.Product,
            AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 22F, FontStyle.Bold),
            ForeColor = Color.FromArgb(248, 250, 252),
            Location = new Point(30, 20)
        };
        header.Controls.Add(title);

        versionLabel = new Label {
            AutoSize = true,
            ForeColor = Color.FromArgb(148, 163, 184),
            Location = new Point(33, 63)
        };
        header.Controls.Add(versionLabel);

        statusLabel = new Label {
            AutoSize = true,
            ForeColor = Color.FromArgb(34, 197, 94),
            Location = new Point(33, 87)
        };
        header.Controls.Add(statusLabel);

        logBox = new RichTextBox {
            Dock = DockStyle.Fill,
            ReadOnly = true,
            BackColor = Color.FromArgb(2, 6, 23),
            ForeColor = Color.FromArgb(186, 230, 253),
            BorderStyle = BorderStyle.FixedSingle,
            Font = new Font("Consolas", 9F),
            DetectUrls = false,
            HideSelection = false
        };
        Controls.Add(logBox);

        var footer = new FlowLayoutPanel {
            Dock = DockStyle.Bottom,
            Height = 78,
            Padding = new Padding(24, 16, 24, 12),
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = false,
            BackColor = Color.FromArgb(30, 41, 59)
        };
        Controls.Add(footer);

        startButton = MakeButton("\u542f\u52a8\u670d\u52a1", Color.FromArgb(59, 130, 246));
        updateButton = MakeButton("\u68c0\u67e5\u66f4\u65b0", Color.FromArgb(37, 99, 235));
        openButton = MakeButton("\u6253\u5f00\u7cfb\u7edf", Color.FromArgb(14, 116, 144));
        stopButton = MakeButton("\u505c\u6b62\u670d\u52a1", Color.FromArgb(71, 85, 105));
        diagnosticsButton = MakeButton("\u8bca\u65ad\u4e0e\u65e5\u5fd7", Color.FromArgb(71, 85, 105));
        footer.Controls.Add(startButton);
        footer.Controls.Add(updateButton);
        footer.Controls.Add(openButton);
        footer.Controls.Add(stopButton);
        footer.Controls.Add(diagnosticsButton);

        startButton.Click += async (s, e) => await RunScriptAsync("start.ps1", null, LauncherText.Starting);
        updateButton.Click += async (s, e) => await RunScriptAsync("update.ps1", null, "\u6b63\u5728\u6253\u5f00\u66f4\u65b0\u4e2d\u5fc3...");
        openButton.Click += (s, e) => OpenApplication();
        stopButton.Click += async (s, e) => await RunScriptAsync("stop.ps1", null, "\u6b63\u5728\u505c\u6b62\u670d\u52a1...");
        diagnosticsButton.Click += async (s, e) => await RunScriptAsync("diagnostics.ps1", "-NoOpen", "\u6b63\u5728\u6536\u96c6\u8bca\u65ad\u4fe1\u606f...");
        Shown += (s, e) => RefreshVersion();
        if (!string.IsNullOrWhiteSpace(this.autoScript))
        {
            Shown += async (s, e) => await RunScriptAsync(this.autoScript, this.autoArguments, this.autoStatus);
        }
    }

    private Button MakeButton(string text, Color color)
    {
        var button = new Button {
            Text = text,
            Width = 132,
            Height = 38,
            FlatStyle = FlatStyle.Flat,
            BackColor = color,
            ForeColor = Color.White,
            Margin = new Padding(6, 0, 6, 0),
            UseVisualStyleBackColor = false
        };
        button.FlatAppearance.BorderColor = Color.FromArgb(100, 116, 139);
        return button;
    }

    private void RefreshVersion()
    {
        var versionFile = Path.Combine(appRoot, "VERSION.txt");
        var buildFile = Path.Combine(appRoot, "BUILD_ID.txt");
        var version = ReadFirstLine(versionFile);
        var build = ReadFirstLine(buildFile);
        versionLabel.Text = string.IsNullOrWhiteSpace(build)
            ? "\u5f53\u524d\u7248\u672c: v" + version
            : "\u5f53\u524d\u7248\u672c: v" + version + "  (" + build + ")";
        statusLabel.Text = Directory.Exists(appRoot) ? LauncherText.Ready : "\u672a\u68c0\u6d4b\u5230\u5e94\u7528\u76ee\u5f55";
    }

    private static string ReadFirstLine(string path)
    {
        try
        {
            if (!File.Exists(path)) return "unknown";
            using (var reader = new StreamReader(path, Encoding.UTF8, true))
            {
                return (reader.ReadLine() ?? "unknown").Trim();
            }
        }
        catch
        {
            return "unknown";
        }
    }

    private string PowerShellPath()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe");
    }

    private static string QuoteArgument(string value)
    {
        if (value == null) return "\"\"";
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private async Task<int> RunScriptAsync(string scriptName, string extraArguments, string status)
    {
        if (busy) return -1;
        var scriptPath = Path.Combine(packageRoot, "scripts", scriptName);
        if (!File.Exists(scriptPath))
        {
            ShowFailure("\u6587\u4ef6\u7f3a\u5931", scriptPath);
            return -1;
        }

        SetBusy(true, status);
        AppendLog("\r\n[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] " + scriptName);
        try
        {
            var psi = new ProcessStartInfo {
                FileName = PowerShellPath(),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + QuoteArgument(scriptPath) + (string.IsNullOrWhiteSpace(extraArguments) ? "" : " " + extraArguments),
                WorkingDirectory = packageRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            using (var process = new Process { StartInfo = psi, EnableRaisingEvents = true })
            {
                var completion = new TaskCompletionSource<int>();
                process.OutputDataReceived += (s, e) => { if (e.Data != null) AppendLog(e.Data); };
                process.ErrorDataReceived += (s, e) => { if (e.Data != null) AppendLog("[stderr] " + e.Data); };
                process.Exited += (s, e) => completion.TrySetResult(process.ExitCode);
                if (!process.Start()) throw new InvalidOperationException("\u65e0\u6cd5\u542f\u52a8 PowerShell");
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                var exitCode = await completion.Task.ConfigureAwait(true);
                AppendLog("\u64cd\u4f5c\u7ed3\u675f，\u9000\u51fa\u7801: " + exitCode);
                if (exitCode != 0)
                {
                    statusLabel.Text = LauncherText.Failed;
                    statusLabel.ForeColor = Color.FromArgb(248, 113, 113);
                    ShowFailure("\u64cd\u4f5c\u5931\u8d25", "\u9000\u51fa\u7801: " + exitCode + "\r\n\r\n\u8bf7\u67e5\u770b app\\logs \u4e2d\u7684\u8be6\u7ec6\u65e5\u5fd7\u3002");
                }
                else
                {
                    statusLabel.Text = LauncherText.Ready;
                    statusLabel.ForeColor = Color.FromArgb(34, 197, 94);
                    RefreshVersion();
                }
                return exitCode;
            }
        }
        catch (Exception ex)
        {
            AppendLog(ex.ToString());
            ShowFailure("\u542f\u52a8\u5931\u8d25", ex.ToString());
            return -1;
        }
        finally
        {
            SetBusy(false, LauncherText.Ready);
        }
    }

    private void SetBusy(bool value, string status)
    {
        busy = value;
        statusLabel.Text = status;
        statusLabel.ForeColor = Color.FromArgb(186, 230, 253);
        startButton.Enabled = !value;
        updateButton.Enabled = !value;
        stopButton.Enabled = !value;
        diagnosticsButton.Enabled = !value;
        openButton.Enabled = !value;
        if (!value) RefreshVersion();
    }

    private void AppendLog(string line)
    {
        if (IsDisposed) return;
        if (InvokeRequired)
        {
            BeginInvoke(new Action<string>(AppendLog), line);
            return;
        }
        logBox.AppendText(line + Environment.NewLine);
        logBox.SelectionStart = logBox.TextLength;
        logBox.ScrollToCaret();
    }

    private void ShowFailure(string title, string detail)
    {
        if (InvokeRequired)
        {
            BeginInvoke(new Action<string, string>(ShowFailure), title, detail);
            return;
        }
        MessageBox.Show(this, detail, title, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private void OpenApplication()
    {
        try
        {
            var port = "20000";
            var envPath = Path.Combine(appRoot, ".env");
            if (File.Exists(envPath))
            {
                foreach (var line in File.ReadAllLines(envPath, Encoding.UTF8))
                {
                    if (line.StartsWith("FRONTEND_PORT=", StringComparison.OrdinalIgnoreCase))
                    {
                        var value = line.Substring("FRONTEND_PORT=".Length).Trim();
                        if (value.Length > 0) port = value;
                    }
                }
            }
            Process.Start(new ProcessStartInfo("http://127.0.0.1:" + port) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            ShowFailure("\u65e0\u6cd5\u6253\u5f00\u7f51\u9875", ex.ToString());
        }
    }
}

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var executableName = Path.GetFileNameWithoutExtension(Application.ExecutablePath);
        var autoScript = string.Empty;
        var autoArguments = string.Empty;
        var autoStatus = string.Empty;
        if (string.Equals(executableName, LauncherText.Installer, StringComparison.Ordinal))
        {
            autoScript = "install.ps1";
            autoStatus = "\u6b63\u5728\u5b89\u88c5\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf...";
        }
        else if (string.Equals(executableName, LauncherText.Updater, StringComparison.Ordinal))
        {
            autoScript = "update.ps1";
            autoStatus = "\u6b63\u5728\u6253\u5f00\u66f4\u65b0\u4e2d\u5fc3...";
        }
        else if (string.Equals(executableName, LauncherText.Stopper, StringComparison.Ordinal))
        {
            autoScript = "stop.ps1";
            autoStatus = "\u6b63\u5728\u505c\u6b62\u670d\u52a1...";
        }
        else if (string.Equals(executableName, LauncherText.Diagnostics, StringComparison.Ordinal))
        {
            autoScript = "diagnostics.ps1";
            autoArguments = "-NoOpen";
            autoStatus = "\u6b63\u5728\u6536\u96c6\u8bca\u65ad\u4fe1\u606f...";
        }
        Application.Run(new LauncherForm(autoScript, autoArguments, autoStatus));
    }
}
