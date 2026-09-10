// Role windows for the Xianyu desktop launcher.
//
// Every visible state (stage, progress, version, service health) is either
// received through the @@XIANYU_UI@@ protocol or read from real files /
// scripts. There is deliberately no keyword-based progress inference anymore.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class AppOps
{
    internal static bool TraceEnabled;

    internal static void Trace(string line)
    {
        if (!TraceEnabled) return;
        try
        {
            var dir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "launcher-trace.log"), DateTime.Now.ToString("HH:mm:ss.fff") + " " + line + Environment.NewLine, System.Text.Encoding.UTF8);
        }
        catch { }
    }

    internal static string ReadFirstLine(string path)
    {
        try { if (File.Exists(path)) { using (var reader = new StreamReader(path, System.Text.Encoding.UTF8)) { var line = reader.ReadLine(); return line == null ? "" : line.Trim(); } } }
        catch { }
        return "";
    }

    internal static bool RegisterRestartResume(string installPath)
    {
        try
        {
            var runOnce = Registry.CurrentUser.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce", true);
            if (runOnce == null) runOnce = Registry.CurrentUser.CreateSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce");
            if (runOnce == null) return false;
            runOnce.SetValue("XianyuInstallerResume", "\"" + Application.ExecutablePath + "\" --installer --resume --install-path " + ScriptRunner.Quote(installPath));
            return true;
        }
        catch { return false; }
    }

    internal static void ExecuteRestart()
    {
        try { Process.Start(new ProcessStartInfo { FileName = "shutdown.exe", Arguments = "/r /t 5", UseShellExecute = false, CreateNoWindow = true }); }
        catch { }
    }

    internal static void OpenUrl(string url)
    {
        try { Process.Start(url); }
        catch { }
    }

    internal static void OpenFolder(string folder)
    {
        try
        {
            if (!Directory.Exists(folder)) Directory.CreateDirectory(folder);
            Process.Start(new ProcessStartInfo { FileName = "explorer.exe", Arguments = "\"" + folder + "\"", UseShellExecute = false });
        }
        catch { }
    }

    internal static bool LaunchPackageExecutable(string packageRoot, string asciiName, string chineseName, string argument, string fallbackScript, string fallbackArguments)
    {
        foreach (var candidate in new[] { Path.Combine(packageRoot, chineseName + ".exe"), Path.Combine(packageRoot, asciiName + ".exe") })
        {
            if (!File.Exists(candidate)) continue;
            try
            {
                var psi = new ProcessStartInfo(candidate, argument) { WorkingDirectory = packageRoot, UseShellExecute = false };
                Process.Start(psi);
                return true;
            }
            catch { }
        }
        return false;
    }

    internal static Control[] Snapshot(Control.ControlCollection collection)
    {
        var list = new Control[collection.Count];
        collection.CopyTo(list, 0);
        return list;
    }

    internal static string FormatActivityTime(string timestamp)
    {
        DateTime parsed;
        if (!DateTime.TryParse(timestamp, CultureInfo.InvariantCulture, DateTimeStyles.None, out parsed))
        {
            if (!DateTime.TryParse(timestamp, out parsed)) return timestamp.Length > 16 ? timestamp.Substring(0, 16) : timestamp;
        }
        var now = DateTime.Now;
        if (parsed.Date == now.Date) return parsed.ToString("HH:mm");
        if (parsed.Date == now.AddDays(-1).Date) return "昨天 " + parsed.ToString("HH:mm");
        return parsed.ToString("MM-dd HH:mm");
    }
}

// ---------------------------------------------------------------------------
// Dialogs: confirm + failure summary. Both reuse the rounded window chrome.
// ---------------------------------------------------------------------------
internal sealed class XianyuDialog
{
    internal static bool Confirm(IWin32Window owner, string title, string body, string confirmText, bool danger)
    {
        bool dark = danger;
        using (var form = new SmallWindow(title, dark ? Ui.Surface : Ui.LightBackground, !dark))
        {
            form.Width = Ui.Px(dark ? 440 : 480);
            form.Height = Ui.Px(dark ? 210 : 240);
            var layout = form.Content;
            layout.Padding = dark ? Ui.Pad(24, 10, 24, 18) : Ui.Pad(28, 12, 28, 24);
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(36)));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(48)));
            var heading = new Label { Text = title, Font = Ui.Font(18, true), ForeColor = dark ? Ui.TextPrimary : Ui.LightText, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
            var bodyLabel = new Label { Text = body, Font = Ui.Font(14, false), ForeColor = dark ? Ui.TextSecondary : Ui.LightTextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.TopLeft };
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, WrapContents = false };
            var cancel = new ModernButton { Text = "取消", Variant = dark ? ModernButton.ButtonVariant.Secondary : ModernButton.ButtonVariant.LightSecondary, CustomBorderColor = dark ? Ui.Border : Ui.LightBorder, Width = Ui.Px(104), Height = Ui.Px(40), Margin = new Padding(0, 0, Ui.Px(8), 0) };
            cancel.Click += (s, e) => { form.DialogResult = DialogResult.Cancel; form.Close(); };
            var ok = new ModernButton { Text = confirmText, Variant = danger ? ModernButton.ButtonVariant.DangerSolid : ModernButton.ButtonVariant.Primary, Width = Ui.Px(150), Height = Ui.Px(40) };
            ok.Click += (s, e) => { form.DialogResult = DialogResult.OK; form.Close(); };
            buttons.Controls.Add(ok);
            buttons.Controls.Add(cancel);
            layout.Controls.Add(heading, 0, 0);
            layout.Controls.Add(bodyLabel, 0, 1);
            layout.Controls.Add(buttons, 0, 2);
            return form.ShowDialog(owner) == DialogResult.OK;
        }
    }

    internal sealed class SmallWindow : LauncherWindow
    {
        internal readonly TableLayoutPanel Content;

        internal SmallWindow(string title, Color back, bool light) : base(title, light)
        {
            BackColor = back;
            MaximizeBox = false;
            MinimumSize = new Size(Ui.Px(360), Ui.Px(160));
            Content = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, BackColor = back };
            Content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            SetContent(Content);
        }
    }
}

internal sealed class FailureSummaryCard : Card
{
    internal readonly Label Heading;
    internal readonly Label CodeLabel;
    internal readonly RichTextBox Detail;
    private readonly string packageRoot;
    private readonly string context;

    internal FailureSummaryCard(string title, string logPath, string packageRootPath, string errorContext)
    {
        packageRoot = packageRootPath;
        context = errorContext;
        ShowBorder = true;
        SurfaceColor = Color.FromArgb(0xFF, 0xF7, 0xF7);
        BorderColor = Color.FromArgb(0xFE, 0xD9, 0xD9);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, BackColor = SurfaceColor };
        layout.Padding = Ui.Pad(16, 12, 16, 12);
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(28)));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(24)));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(44)));
        Heading = new Label { Text = title, Font = Ui.Font(15, true), ForeColor = Color.FromArgb(0xB9, 0x1C, 0x1C), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
        CodeLabel = new Label { Text = "", Font = Ui.Font(13, false), ForeColor = Color.FromArgb(0x7F, 0x1D, 0x1D), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
        Detail = new RichTextBox
        {
            Dock = DockStyle.Fill,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            BackColor = SurfaceColor,
            ForeColor = Color.FromArgb(0x99, 0x1B, 0x1B),
            Font = Ui.Mono(12),
            WordWrap = false,
            ScrollBars = RichTextBoxScrollBars.Vertical
        };
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.LeftToRight, WrapContents = false };
        var copy = new ModernButton { Text = "复制错误信息", IconName = "clipboard-copy", Variant = ModernButton.ButtonVariant.LightSecondary, CustomBorderColor = Ui.LightBorder, Height = Ui.Px(36), Width = Ui.Px(150), Margin = new Padding(0, 4, Ui.Px(8), 0), TextDesignSize = 13 };
        copy.Click += (s, e) => { try { Clipboard.SetText(Heading.Text + Environment.NewLine + CodeLabel.Text + Environment.NewLine + Detail.Text); } catch { } };
        var openLog = new ModernButton { Text = "打开日志目录", IconName = "folder-open", Variant = ModernButton.ButtonVariant.LightSecondary, CustomBorderColor = Ui.LightBorder, Height = Ui.Px(36), Width = Ui.Px(150), Margin = new Padding(0, 4, Ui.Px(8), 0), TextDesignSize = 13 };
        openLog.Click += (s, e) => AppOps.OpenFolder(Path.Combine(Path.GetDirectoryName(packageRoot), "app", "logs"));
        var upload = new ModernButton { Text = "上传诊断日志（开发调试）", IconName = "shield", Variant = ModernButton.ButtonVariant.LightSecondary, CustomBorderColor = Ui.LightBorder, Height = Ui.Px(36), Width = Ui.Px(210), Margin = new Padding(0, 4, 0, 0), TextDesignSize = 13 };
        upload.Click += (s, e) =>
        {
            upload.Enabled = false;
            upload.Text = "正在上传…";
            Task.Run(() => UploadDiagnostics(packageRoot, context, Detail.Text)).ContinueWith(t =>
            {
                try
                {
                    BeginInvoke((MethodInvoker)(() =>
                    {
                        upload.Enabled = true;
                        upload.Text = "上传诊断日志（开发调试）";
                        XianyuDialog.Confirm(null, "诊断日志上传", t.Result, "关闭", false);
                    }));
                }
                catch { }
            });
        };
        buttons.Controls.Add(copy);
        buttons.Controls.Add(openLog);
        buttons.Controls.Add(upload);
        layout.Controls.Add(Heading, 0, 0);
        layout.Controls.Add(CodeLabel, 0, 1);
        layout.Controls.Add(Detail, 0, 2);
        layout.Controls.Add(buttons, 0, 3);
        Controls.Add(layout);
    }

    internal void Show(string title, string code, string message)
    {
        Heading.Text = title;
        CodeLabel.Text = string.IsNullOrEmpty(code) ? "错误代码：未知" : "错误代码：" + code;
        Detail.Text = message ?? "";
    }

    private static string UploadDiagnostics(string packageRoot, string context, string detail)
    {
        try
        {
            var appRoot = Path.Combine(packageRoot, "app");
            var scriptPath = Path.Combine(packageRoot, "scripts", "upload-diagnostics.ps1");
            if (!File.Exists(scriptPath)) return "上传脚本不存在。";
            var psi = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + ScriptRunner.Quote(scriptPath) + " -ProjectRoot " + ScriptRunner.Quote(appRoot) + " -Context " + ScriptRunner.Quote((context + " " + detail).Trim()),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                StandardOutputEncoding = System.Text.Encoding.UTF8
            };
            psi.EnvironmentVariables["XIANYU_NONINTERACTIVE"] = "1";
            using (var process = Process.Start(psi))
            {
                var output = process.StandardOutput.ReadToEnd();
                process.WaitForExit(15000);
                return output.Trim().Length > 0 ? output.Trim() : "上传完成。";
            }
        }
        catch (Exception ex) { return "上传诊断日志失败：" + ex.Message; }
    }
}

// ---------------------------------------------------------------------------
// Installer: five-state wizard driven only by real events.
// ---------------------------------------------------------------------------
internal sealed class InstallerWindow : LauncherWindow
{
    private static readonly string[] StageOrder = { "env", "config", "images_base", "images_app", "start", "health" };
    private static readonly string[] StageTitles = { "准备系统环境", "创建应用配置", "导入基础镜像", "导入业务镜像", "启动服务", "健康检查" };
    private static readonly int[] StageWeightEnds = { 10, 20, 45, 65, 80, 100 };
    private static readonly int[] StageWeightStarts = { 0, 10, 20, 45, 65, 80 };

    private readonly string packageRoot;
    private readonly string appRoot;
    private readonly bool resume;
    private readonly string resumeInstallPath;

    private int step;
    private TableLayoutPanel contentArea;
    private TableLayoutPanel footerArea;
    private StepIndicator indicator;

    // precheck
    private readonly Dictionary<string, StageRow> precheckRows = new Dictionary<string, StageRow>();
    private Panel precheckBanner;
    private OperationState precheckState = OperationState.Idle;

    // install
    private readonly Dictionary<string, StageRow> installRows = new Dictionary<string, StageRow>();
    private readonly Dictionary<string, int> installStageProgress = new Dictionary<string, int>();
    private ProgressBoard progressBar;
    private Label progressCaption;
    private Label progressCount;
    private CollapsibleSection logSection;
    private RichTextBox logBox;
    private FailureSummaryCard failureCard;
    private Panel installBanner;
    private OperationState installState = OperationState.Idle;
    private bool precheckPassed;
    private string frontendUrl = "";
    private string deployMode = "";
    private string shortcutCreated = "true";

    // path step
    private TextBox pathBox;
    private Control freeSpaceLabel;
    private Control requiredSpaceLabel;
    private StageRow writableRow;
    private StageRow spaceRow;
    private CheckBox shortcutCheck;
    private long requiredBytes;
    private readonly Timer pathDebounce = new Timer();

    internal InstallerWindow(bool resumeAfterRestart, string resumePath) : base(LauncherText.Installer, true)
    {
        resume = resumeAfterRestart;
        resumeInstallPath = resumePath ?? "";
        packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        appRoot = Path.Combine(packageRoot, "app");
        BackColor = Ui.LightBackground;
        contentHost.BackColor = Ui.LightBackground;

        var outer = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Ui.LightBackground };
        outer.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        outer.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(360)));
        outer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        var rail = new RailPanel(AppOps.ReadFirstLine(Path.Combine(appRoot, "VERSION.txt")));
        rail.Dock = DockStyle.Fill;
        contentArea = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 1, BackColor = Ui.LightBackground };
        contentArea.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        contentArea.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        footerArea = new TableLayoutPanel { Dock = DockStyle.Bottom, ColumnCount = 3, RowCount = 1, Height = Ui.Px(88), BackColor = Ui.LightBackground };
        footerArea.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        footerArea.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        footerArea.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var contentCol = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, BackColor = Ui.LightBackground };
        contentCol.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        contentCol.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(72)));
        contentCol.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        contentCol.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(88)));
        indicator = new StepIndicator();
        indicator.Dock = DockStyle.Fill;
        contentCol.Controls.Add(indicator, 0, 0);
        contentCol.Controls.Add(contentArea, 0, 1);
        contentCol.Controls.Add(footerArea, 0, 2);
        outer.Controls.Add(rail, 0, 0);
        outer.Controls.Add(contentCol, 1, 0);
        SetContent(outer);

        pathDebounce.Interval = 450;
        pathDebounce.Tick += (s, e) => { pathDebounce.Stop(); ValidatePathLive(); };

        ShowStep(0);
        if (resume && resumeInstallPath.Length > 0)
        {
            ShowStep(3);
            StartInstall(resumeInstallPath, true);
        }
    }

    private void ShowStep(int index)
    {
        step = index;
        indicator.SetStep(index);
        contentArea.Controls.Clear();
        footerArea.Controls.Clear();
        switch (index)
        {
            case 0: BuildWelcome(); break;
            case 1: BuildPrecheck(); break;
            case 2: BuildPath(); break;
            case 3: BuildInstall(); break;
            case 4: BuildComplete(); break;
        }
    }

    private static Label PageTitle(string text)
    {
        return new Label { Text = text, Font = Ui.Font(24, true), ForeColor = Ui.LightText, Dock = DockStyle.Top, Height = Ui.Px(44), TextAlign = ContentAlignment.MiddleLeft };
    }

    private static Label PageSubtitle(string text)
    {
        return new Label { Text = text, Font = Ui.Font(14, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Top, Height = Ui.Px(28), TextAlign = ContentAlignment.MiddleLeft };
    }

    // ---- step 1 -------------------------------------------------------------
    private void BuildWelcome()
    {
        var page = NewPage();
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 5, ColumnCount = 1 };
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(48)));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(16)));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var title = new Label { Text = "安装闲鱼管理系统", Font = Ui.Font(28, true), ForeColor = Ui.LightText, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
        var subtitle = new Label { Text = "自动配置 WSL 2 和 Docker 运行环境，部署前后端、消息及定时任务服务。", Font = Ui.Font(15, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, Height = Ui.Px(64), TextAlign = ContentAlignment.MiddleLeft };
        var infoCard = new Card { Dock = DockStyle.Fill, Height = Ui.Px(172), SurfaceColor = Color.White, BorderColor = Ui.LightBorder };
        var infoRows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, BackColor = Color.White };
        infoRows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        infoRows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        infoRows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        infoRows.Padding = Ui.Pad(16);
        infoRows.Controls.Add(InfoRow("container", "检测运行环境", "Docker Desktop、WSL 2、安装包与网络（安装前可确认）"), 0, 0);
        infoRows.Controls.Add(InfoRow("package", "部署全部服务", "前端、后端、消息与定时任务一键部署"), 0, 1);
        infoRows.Controls.Add(InfoRow("shield", "支持离线安装", "包含离线镜像时无需访问外网"), 0, 2);
        infoCard.Controls.Add(infoRows);
        body.Controls.Add(title, 0, 0);
        body.Controls.Add(subtitle, 0, 1);
        body.Controls.Add(infoCard, 0, 3);
        page.Controls.Add(body);
        contentArea.Controls.Add(page);

        AddFooterPrimary("开始检测", "chevron-right", (s, e) => ShowStep(1));
    }

    private static Control InfoRow(string icon, string title, string detail)
    {
        var row = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 2, RowCount = 1, Height = Ui.Px(40) };
        row.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        row.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(36)));
        row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        var iconBox = new IconBox(icon, Color.FromArgb(0x33, 0x41, 0x55));
        iconBox.Dock = DockStyle.Fill;
        var text = new Label { Text = title + " · " + detail, Font = Ui.Font(14, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true };
        row.Controls.Add(iconBox, 0, 0);
        row.Controls.Add(text, 1, 0);
        return row;
    }

    // ---- step 2: real environment check --------------------------------------
    private void BuildPrecheck()
    {
        var page = NewPage();
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1 };
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var title = PageTitle("环境检测");
        var subtitle = PageSubtitle("检测本机运行环境与安装包状态；全部就绪后即可开始安装。");
        var card = new Card { Dock = DockStyle.Top, Height = Ui.Px(304), SurfaceColor = Color.White, BorderColor = Ui.LightBorder };
        var rows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, BackColor = Color.White, Padding = Ui.Pad(12, 4, 12, 4) };
        rows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        rows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        rows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        rows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        precheckRows.Clear();
        string[,] defs = {
            { "precheck_docker", "Docker Desktop", "container" },
            { "precheck_wsl", "WSL 2", "monitor" },
            { "precheck_resources", "本地安装资源", "package" },
            { "precheck_network", "网络服务", "wifi" },
        };
        for (var i = 0; i < 4; i++)
        {
            var row = new StageRow(defs[i, 1], defs[i, 2]);
            row.SetState(StageStatus.Pending, "等待检测", "");
            precheckRows[defs[i, 0]] = row;
            rows.Controls.Add(row, 0, i);
        }
        card.Controls.Add(rows);
        precheckBanner = new Panel { Dock = DockStyle.Top, Height = Ui.Px(84), Visible = false };
        body.Controls.Add(title, 0, 0);
        body.Controls.Add(subtitle, 0, 1);
        var stacked = new Panel { Dock = DockStyle.Fill };
        stacked.Controls.Add(card);
        stacked.Controls.Add(precheckBanner);
        precheckBanner.BringToFront();
        body.Controls.Add(stacked, 0, 2);
        page.Controls.Add(body);
        contentArea.Controls.Add(page);

        var prev = AddFooterGhost("上一步", "chevron-left", (s, e) => ShowStep(0));
        var next = AddFooterPrimary("下一步", "chevron-right", (s, e) => ShowStep(2));
        next.Enabled = precheckPassed;

        if (precheckState == OperationState.Idle || precheckState == OperationState.Failed) RunPrecheck();
    }

    private ModernButton footerPrimary;

    private void RunPrecheck()
    {
        precheckState = OperationState.Checking;
        precheckPassed = false;
        SetFooterPrimaryEnabled(false);
        foreach (var pair in precheckRows) { pair.Value.ClearActions(); pair.Value.SetState(StageStatus.Pending, "等待检测", ""); }
        ScriptRunner.RunAsync(packageRoot, "install.ps1", "-CheckOnly", OnPrecheckLine).ContinueWith(t =>
        {
            try { BeginInvoke((MethodInvoker)(() => OnPrecheckFinished(t.Result == null ? 1 : t.Result.ExitCode))); } catch { }
        });
    }

    private void OnPrecheckLine(string line)
    {
        if (line == null) return;
        GuiEvent evt;
        if (!GuiProtocol.TryParse(line, out evt)) return;
        try { BeginInvoke((MethodInvoker)(() => ApplyPrecheckEvent(evt))); } catch { }
    }

    private void ApplyPrecheckEvent(GuiEvent evt)
    {
        if (evt.Type == "meta" && evt.Key == "deploy_mode") { deployMode = evt.Value; return; }
        if (evt.Type != "stage" && evt.Type != "result") return;
        StageRow row;
        if (evt.Type == "stage" && evt.Operation == "precheck" && precheckRows.TryGetValue(evt.Stage, out row))
        {
            if (!evt.HasStatus) return;
            row.SetState(evt.Status, string.IsNullOrEmpty(evt.Detail) ? StatusWord(evt.Status) : evt.Detail, evt.Code);
            row.ClearActions();
            if (evt.Status == StageStatus.Failed && evt.Stage == "precheck_docker")
            {
                row.AddAction("重新检测", "refresh-cw", delegate { RunPrecheck(); });
                row.AddAction("打开 Docker Desktop", "square-arrow-out-up-right", delegate { OpenDockerDesktop(); });
            }
            else if (evt.Status == StageStatus.Failed)
            {
                row.AddAction("重新检测", "refresh-cw", delegate { RunPrecheck(); });
            }
            else if (evt.Status == StageStatus.Warning && evt.Code == "E_WSL_RESTART_REQUIRED")
            {
                ShowRestartBanner("Windows 需要重新启动", "WSL 2 组件已启用。保存工作后可以立即重启，或稍后手动重启，系统将自动继续安装。", true);
            }
        }
    }

    internal static string StatusWord(StageStatus status)
    {
        switch (status)
        {
            case StageStatus.Running: return "处理中…";
            case StageStatus.Success: return "通过";
            case StageStatus.Warning: return "需要注意";
            case StageStatus.Failed: return "失败";
            case StageStatus.Skipped: return "跳过";
            default: return "等待";
        }
    }

    private void OnPrecheckFinished(int exitCode)
    {
        if (step != 1) return;
        if (exitCode == 3010)
        {
            precheckState = OperationState.Failed;
            AppOps.RegisterRestartResume(DefaultInstallPath());
            ShowRestartBanner("Windows 需要重新启动", "保存工作后重启，登录时安装程序将自动继续。", false);
            SetFooterPrimaryEnabled(false);
            return;
        }
        if (exitCode == 0)
        {
            var allOk = true;
            foreach (var pair in precheckRows)
            {
                if (pair.Value.Status == StageStatus.Pending || pair.Value.Status == StageStatus.Running || pair.Value.Status == StageStatus.Failed) allOk = false;
            }
            precheckPassed = allOk;
            precheckState = allOk ? OperationState.Ready : OperationState.Failed;
            SetFooterPrimaryEnabled(allOk);
            if (footerPrimary != null) footerPrimary.Text = "下一步";
            if (!allOk)
            {
                foreach (var pair in precheckRows)
                    if (pair.Value.Status == StageStatus.Pending || pair.Value.Status == StageStatus.Running)
                        pair.Value.SetState(StageStatus.Failed, "检测未完成，请重新检测", "E_PRECHECK");
            }
        }
        else
        {
            precheckState = OperationState.Failed;
            precheckPassed = false;
            SetFooterPrimaryEnabled(false);
            if (footerPrimary != null) footerPrimary.Text = "下一步";
        }
    }

    private void ShowRestartBanner(string heading, string body, bool withOpenDocker)
    {
        precheckBanner.Controls.Clear();
        precheckBanner.Visible = true;
        var card = new Card { Dock = DockStyle.Fill, Height = Ui.Px(76), SurfaceColor = Color.FromArgb(0xFF, 0xFB, 0xEB), BorderColor = Color.FromArgb(0xFD, 0xE6, 0x8A) };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = card.SurfaceColor };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        layout.Padding = Ui.Pad(14, 8, 14, 8);
        var text = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
        text.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(24)));
        text.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        text.Controls.Add(new Label { Text = heading, Font = Ui.Font(15, true), ForeColor = Color.FromArgb(0x92, 0x40, 0x0F), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 0);
        text.Controls.Add(new Label { Text = body, Font = Ui.Font(13, false), ForeColor = Color.FromArgb(0xB4, 0x53, 0x09), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true }, 0, 1);
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.LeftToRight, WrapContents = false, AutoSize = true };
        var later = new ModernButton { Text = "稍后重启", Variant = ModernButton.ButtonVariant.LightSecondary, CustomBorderColor = Color.FromArgb(0xF0, 0xC8, 0x70), Width = Ui.Px(110), Height = Ui.Px(40), Margin = new Padding(0, 6, Ui.Px(8), 0) };
        later.Click += (s, e) => { Close(); };
        var now = new ModernButton { Text = "立即重启", IconName = "power", Variant = ModernButton.ButtonVariant.Primary, Width = Ui.Px(120), Height = Ui.Px(40), Margin = new Padding(0, 6, 0, 0) };
        now.Click += (s, e) => { AppOps.ExecuteRestart(); };
        buttons.Controls.Add(later);
        buttons.Controls.Add(now);
        layout.Controls.Add(text, 0, 0);
        layout.Controls.Add(buttons, 1, 0);
        card.Controls.Add(layout);
        precheckBanner.Controls.Add(card);
    }

    private void OpenDockerDesktop()
    {
        ScriptRunner.RunAsync(packageRoot, "install.ps1", "-OpenDocker", null);
    }

    // ---- step 3: install path --------------------------------------------------
    private static string DefaultInstallPath()
    {
        try
        {
            foreach (var drive in DriveInfo.GetDrives())
            {
                if (drive.DriveType == DriveType.Removable || drive.DriveType == DriveType.CDRom) continue;
                if (!drive.IsReady) continue;
                if (string.Equals(drive.Name, "D:\\", StringComparison.OrdinalIgnoreCase)) return Path.Combine("D:\\", "XianYu-System");
            }
        }
        catch { }
        var systemDrive = Environment.GetEnvironmentVariable("SystemDrive");
        if (string.IsNullOrEmpty(systemDrive)) systemDrive = "C:";
        return Path.Combine(systemDrive + Path.DirectorySeparatorChar, "XianYu-System");
    }

    private void BuildPath()
    {
        var page = NewPage();
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 4, ColumnCount = 1 };
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        body.Controls.Add(PageTitle("选择安装位置"), 0, 0);
        body.Controls.Add(PageSubtitle("程序与运行数据将保存在下面的文件夹。"), 0, 1);

        var pathRow = new TableLayoutPanel { Dock = DockStyle.Top, Height = Ui.Px(48), ColumnCount = 3, RowCount = 1 };
        pathRow.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(8)));
        pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(110)));
        pathBox = new TextBox
        {
            Dock = DockStyle.Fill,
            Text = resumeInstallPath.Length > 0 ? resumeInstallPath : DefaultInstallPath(),
            Font = Ui.Font(14, false),
            BorderStyle = BorderStyle.FixedSingle,
            Height = Ui.Px(40)
        };
        pathBox.TextChanged += (s, e) => { pathDebounce.Stop(); pathDebounce.Start(); };
        var browse = new ModernButton { Text = "浏览…", IconName = "folder-open", Variant = ModernButton.ButtonVariant.LightSecondary, CustomBorderColor = Ui.LightBorder, Dock = DockStyle.Fill };
        browse.Click += (s, e) =>
        {
            using (var dialog = new FolderBrowserDialog())
            {
                dialog.Description = "选择安装位置";
                try { dialog.SelectedPath = Path.GetDirectoryName(pathBox.Text); } catch { }
                if (dialog.ShowDialog(this) == DialogResult.OK) pathBox.Text = dialog.SelectedPath;
            }
        };
        pathRow.Controls.Add(pathBox, 0, 0);
        pathRow.Controls.Add(browse, 2, 0);

        var card = new Card { Dock = DockStyle.Top, Height = Ui.Px(212), SurfaceColor = Color.White, BorderColor = Ui.LightBorder };
        var cardRows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, BackColor = Color.White, Padding = Ui.Pad(12, 6, 12, 6) };
        cardRows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(32)));
        cardRows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(32)));
        cardRows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(64)));
        cardRows.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        freeSpaceLabel = MetaLine("可用空间", "计算中…");
        requiredSpaceLabel = MetaLine("预计需要", "计算中…");
        writableRow = new StageRow("安装位置可写", "hard-drive");
        writableRow.SetState(StageStatus.Pending, "等待验证", "");
        spaceRow = new StageRow("磁盘空间充足", "package");
        spaceRow.SetState(StageStatus.Pending, "等待验证", "");
        cardRows.Controls.Add(freeSpaceLabel, 0, 0);
        cardRows.Controls.Add(requiredSpaceLabel, 0, 1);
        cardRows.Controls.Add(writableRow, 0, 2);
        cardRows.Controls.Add(spaceRow, 0, 3);
        card.Controls.Add(cardRows);

        shortcutCheck = new CheckBox { Text = "创建桌面快捷方式", Checked = true, Font = Ui.Font(14, false), ForeColor = Ui.LightText, AutoSize = true, Margin = Ui.Pad(2, 12, 0, 0), FlatStyle = FlatStyle.Standard };
        body.Controls.Add(pathRow, 0, 2);
        var stacked = new Panel { Dock = DockStyle.Fill };
        stacked.Controls.Add(card);
        stacked.Controls.Add(shortcutCheck);
        shortcutCheck.Top = card.Bottom + Ui.Px(4);
        shortcutCheck.Left = Ui.Pad(32).Left;
        body.Controls.Add(stacked, 0, 3);
        page.Controls.Add(body);
        contentArea.Controls.Add(page);

        AddFooterGhost("上一步", "chevron-left", (s, e) => ShowStep(1));
        footerPrimary = AddFooterPrimary("开始安装", "circle-check", (s, e) =>
        {
            if (!precheckPassed && precheckState != OperationState.Ready) return;
            ShowStep(3);
            StartInstall(pathBox.Text.Trim(), shortcutCheck.Checked);
        });
        footerPrimary.Enabled = false;

        ComputeRequiredSpaceAsync();
        ValidatePathLive();
    }

    private static Control MetaLine(string name, string value)
    {
        var row = new TableLayoutPanel { Dock = DockStyle.Top, Height = Ui.Px(32), ColumnCount = 2, RowCount = 1 };
        row.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        row.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(120)));
        row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        row.Controls.Add(new Label { Text = name, Font = Ui.Font(14, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 0);
        var valueLabel = new Label { Name = "value", Text = value, Font = Ui.Font(14, true), ForeColor = Ui.LightText, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
        row.Controls.Add(valueLabel, 1, 0);
        return row;
    }

    private static Label MetaValue(Control row, string name)
    {
        var layout = row as TableLayoutPanel;
        if (layout == null) return null;
        foreach (Control c in layout.Controls) if (c.Name == name) return c as Label;
        return null;
    }

    private void ComputeRequiredSpaceAsync()
    {
        Task.Run(() =>
        {
            long total = 0;
            try
            {
                foreach (var file in Directory.GetFiles(packageRoot, "*.*", SearchOption.AllDirectories))
                {
                    if (Path.GetFileName(file).Equals("logs", StringComparison.OrdinalIgnoreCase)) continue;
                    try { total += new FileInfo(file).Length; } catch { }
                }
            }
            catch { total = 0; }
            try
            {
                BeginInvoke((MethodInvoker)(() =>
                {
                    requiredBytes = total;
                    var value = MetaValue(requiredSpaceLabel, "value");
                    if (value != null) value.Text = FormatBytes(total) + "（安装包全部内容）";
                    ValidatePathLive();
                }));
            }
            catch { }
        });
    }

    private static string FormatBytes(long bytes)
    {
        if (bytes <= 0) return "未知";
        double gb = bytes / 1024.0 / 1024.0 / 1024.0;
        return gb >= 1 ? gb.ToString("0.0", CultureInfo.InvariantCulture) + " GB" : (bytes / 1024.0 / 1024.0).ToString("0.0", CultureInfo.InvariantCulture) + " MB";
    }

    private void ValidatePathLive()
    {
        var path = pathBox.Text.Trim();
        long free = 0;
        var writable = false;
        try
        {
            var root = Path.GetPathRoot(path);
            if (!string.IsNullOrEmpty(root))
            {
                var drive = new DriveInfo(root);
                if (drive.IsReady) free = drive.AvailableFreeSpace;
                var probeDir = Directory.Exists(path) ? path : (Directory.Exists(root) ? root : null);
                if (probeDir != null)
                {
                    var probeFile = Path.Combine(probeDir, ".xianyu-write-test-" + Guid.NewGuid().ToString("N") + ".tmp");
                    try { File.WriteAllText(probeFile, "x"); writable = true; } catch { writable = false; }
                    finally { try { if (File.Exists(probeFile)) File.Delete(probeFile); } catch { } }
                }
            }
        }
        catch { }
        var freeValue = MetaValue(freeSpaceLabel, "value");
        if (freeValue != null) freeValue.Text = free > 0 ? FormatBytes(free) : "无法读取";
        var enough = requiredBytes <= 0 || free >= requiredBytes + requiredBytes / 4;
        spaceRow.SetState(enough ? StageStatus.Success : StageStatus.Failed,
            enough ? (requiredBytes > 0 ? "剩余空间足够（需 " + FormatBytes(requiredBytes) + "）" : "剩余空间足够") : "剩余空间不足，至少需要 " + FormatBytes(requiredBytes),
            enough ? "" : "E_INSTALL_DISK_SPACE");
        writableRow.SetState(writable ? StageStatus.Success : StageStatus.Failed,
            writable ? "该位置可以正常读写" : "无法在目标位置创建文件",
            writable ? "" : "E_INSTALL_PATH_NOT_WRITABLE");
        if (footerPrimary != null) footerPrimary.Enabled = writable && enough;
    }

    // ---- step 4: real installation ----------------------------------------------
    private void BuildInstall()
    {
        var page = NewPage();
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 6, ColumnCount = 1 };
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(44)));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.Controls.Add(PageTitle("正在安装"), 0, 0);

        var captionRow = new TableLayoutPanel { Dock = DockStyle.Top, Height = Ui.Px(26), ColumnCount = 2 };
        captionRow.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        captionRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        captionRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        progressCaption = new Label { Text = "准备开始安装…", Font = Ui.Font(14, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true };
        progressCount = new Label { Text = "已完成 0 / 6", Font = Ui.Font(13, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, AutoSize = false, TextAlign = ContentAlignment.MiddleRight };
        captionRow.Controls.Add(progressCaption, 0, 0);
        captionRow.Controls.Add(progressCount, 1, 0);
        progressBar = new ProgressBoard { Dark = false, Dock = DockStyle.Top, Height = Ui.Px(14), Value = -1, Margin = new Padding(0, Ui.Px(6), 0, Ui.Px(6)) };
        installBanner = new Panel { Dock = DockStyle.Top, Height = Ui.Px(84), Visible = false };
        failureCard = new FailureSummaryCard("安装失败", Path.Combine(appRoot, "logs", "install.log"), packageRoot, "installer")
        {
            Dock = DockStyle.Top,
            Height = Ui.Px(188),
            Visible = false
        };

        var card = new Card { SurfaceColor = Color.White, BorderColor = Ui.LightBorder, Location = new Point(0, 0) };
        var rows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6, BackColor = Color.White, Padding = Ui.Pad(12, 2, 12, 2) };
        for (var i = 0; i < 6; i++) { rows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(64))); }
        installRows.Clear();
        installStageProgress.Clear();
        for (var i = 0; i < StageOrder.Length; i++)
        {
            var row = new StageRow(StageTitles[i], StageIcon(i));
            row.SetState(StageStatus.Pending, "等待开始", "");
            installRows[StageOrder[i]] = row;
            rows.Controls.Add(row, 0, i);
        }
        card.Controls.Add(rows);

        logSection = new CollapsibleSection("查看详细日志");
        logSection.SetExpandedHeight(220);
        logSection.Dock = DockStyle.Bottom;
        logBox = new RichTextBox
        {
            Dock = DockStyle.Fill,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            BackColor = Color.White,
            ForeColor = Color.FromArgb(0x47, 0x55, 0x69),
            Font = Ui.Mono(12),
            WordWrap = false
        };
        logSection.Body.Controls.Add(logBox);

        body.Controls.Add(captionRow, 0, 1);
        body.Controls.Add(progressBar, 0, 2);
        var stacked = new Panel { Dock = DockStyle.Fill };
        var cardScroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, BackColor = Ui.LightBackground };
        cardScroll.Controls.Add(card);
        card.Size = new Size(Ui.Px(1216), Ui.Px(396));
        cardScroll.Resize += delegate { card.Width = Math.Max(Ui.Px(320), cardScroll.ClientSize.Width); };
        stacked.Controls.Add(cardScroll);
        stacked.Controls.Add(failureCard);
        stacked.Controls.Add(installBanner);
        failureCard.BringToFront();
        installBanner.BringToFront();
        body.Controls.Add(stacked, 0, 4);
        body.Controls.Add(logSection, 0, 5);
        page.Controls.Add(body);
        contentArea.Controls.Add(page);
        footerArea.Padding = Ui.Pad(0);
    }

    private static string StageIcon(int index)
    {
        switch (index)
        {
            case 0: return "wrench";
            case 1: return "file-search";
            case 2: return "container";
            case 3: return "package";
            case 4: return "power";
            default: return "circle-check";
        }
    }

    private void StartInstall(string installPath, bool createShortcut)
    {
        installState = OperationState.Running;
        ScriptRunner.RunAsync(packageRoot, "install.ps1",
            "-InstallPath " + ScriptRunner.Quote(installPath) + " -CreateDesktopShortcut " + (createShortcut ? "True" : "False") + " -NoOpenBrowser",
            OnInstallLine).ContinueWith(t =>
            {
                try { BeginInvoke((MethodInvoker)(() => OnInstallFinished(t.Result == null ? 1 : t.Result.ExitCode))); } catch { }
            });
    }

    private void OnInstallLine(string line)
    {
        if (line == null) return;
        GuiEvent evt;
        if (GuiProtocol.TryParse(line, out evt))
        {
            try { BeginInvoke((MethodInvoker)(() => ApplyInstallEvent(evt))); } catch { }
            return;
        }
        AppendLog(line);
    }

    private void AppendLog(string line)
    {
        if (logBox == null) return;
        try
        {
            BeginInvoke((MethodInvoker)(() =>
            {
                if (step != 3 || logBox.IsDisposed) return;
                if (logBox.TextLength > 400000) logBox.Clear();
                logBox.AppendText(line + Environment.NewLine);
                logBox.SelectionStart = logBox.TextLength;
                logBox.ScrollToCaret();
            }));
        }
        catch { }
    }

    private void ApplyInstallEvent(GuiEvent evt)
    {
        if (evt.Type == "meta")
        {
            if (evt.Key == "frontend_url") frontendUrl = evt.Value;
            else if (evt.Key == "deploy_mode") deployMode = evt.Value;
            else if (evt.Key == "shortcut_created") shortcutCreated = evt.Value;
            return;
        }
        if (evt.Type == "stage" && evt.Operation == "install")
        {
            StageRow row;
            if (!installRows.TryGetValue(evt.Stage, out row)) return;
            installStageProgress[evt.Stage] = evt.Progress;
            if (evt.HasStatus)
            {
                row.SetState(evt.Status, string.IsNullOrEmpty(evt.Detail) ? StatusWord(evt.Status) : evt.Detail, evt.Code);
                if (evt.Status == StageStatus.Failed)
                {
                    installState = OperationState.Failed;
                    ShowInstallFailure(row);
                }
            }
            RecomputeInstallProgress();
            return;
        }
        if (evt.Type == "result")
        {
            if (evt.ResultStatus == "completed") { /* handled by exit code with full data */ }
            else if (evt.ResultStatus == "restart_required")
            {
                installState = OperationState.Failed;
                AppOps.RegisterRestartResume(pathBox != null ? pathBox.Text.Trim() : DefaultInstallPath());
                ShowInstallRestart();
            }
            else if (evt.ResultStatus == "failed")
            {
                installState = OperationState.Failed;
                var active = ActiveInstallStageRow();
                ShowInstallFailure(active);
            }
        }
    }

    private StageRow ActiveInstallStageRow()
    {
        foreach (var stage in StageOrder)
        {
            StageRow row;
            if (installRows.TryGetValue(stage, out row) && row.Status == StageStatus.Failed) return row;
        }
        return null;
    }

    private void RecomputeInstallProgress()
    {
        int doneCount = 0;
        var overall = -1;
        for (var i = 0; i < StageOrder.Length; i++)
        {
            var stage = StageOrder[i];
            StageRow row;
            if (!installRows.TryGetValue(stage, out row)) continue;
            if (row.Status == StageStatus.Success || row.Status == StageStatus.Skipped) { doneCount++; continue; }
            if (row.Status == StageStatus.Running)
            {
                int reported;
                if (installStageProgress.TryGetValue(stage, out reported) && reported >= 0)
                {
                    overall = StageWeightStarts[i] + (StageWeightEnds[i] - StageWeightStarts[i]) * reported / 100;
                }
                else
                {
                    overall = StageWeightStarts[i];
                    progressBar.Value = -1;
                }
                progressCaption.Text = row.DetailLabel.Text;
                break;
            }
        }
        if (overall >= 0) progressBar.Value = overall;
        progressCount.Text = "已完成 " + doneCount + " / " + StageOrder.Length;
    }

    private void ShowInstallFailure(StageRow failedRow)
    {
        failureCard.Visible = true;
        var code = failedRow != null ? failedRow.ErrorCode : "";
        var title = failedRow != null ? failedRow.DetailLabel.Text : "";
        failureCard.Show("安装失败", string.IsNullOrEmpty(code) ? "E_INSTALL" : code, string.IsNullOrEmpty(title) ? "安装未能完成，请查看详细日志。" : title);
        logSection.SetExpanded(true);
        installBanner.Visible = false;
        var banner = failureCard;
        banner.BringToFront();
    }

    private void ShowInstallRestart()
    {
        installBanner.Controls.Clear();
        installBanner.Visible = true;
        var card = new Card { Dock = DockStyle.Fill, SurfaceColor = Color.FromArgb(0xFF, 0xFB, 0xEB), BorderColor = Color.FromArgb(0xFD, 0xE6, 0x8A) };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = card.SurfaceColor, Padding = Ui.Pad(14, 8, 14, 8) };
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        layout.Controls.Add(new Label { Text = "Windows 需要重新启动后继续安装。重启后安装程序会自动回到这里。", Font = Ui.Font(14, false), ForeColor = Color.FromArgb(0xB4, 0x53, 0x09), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 0);
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.LeftToRight, WrapContents = false, AutoSize = true };
        var later = new ModernButton { Text = "稍后重启", Variant = ModernButton.ButtonVariant.LightSecondary, CustomBorderColor = Color.FromArgb(0xF0, 0xC8, 0x70), Width = Ui.Px(110), Height = Ui.Px(40), Margin = new Padding(0, 6, Ui.Px(8), 0) };
        later.Click += (s, e) => Close();
        var now = new ModernButton { Text = "立即重启", IconName = "power", Variant = ModernButton.ButtonVariant.Primary, Width = Ui.Px(120), Height = Ui.Px(40), Margin = new Padding(0, 6, 0, 0) };
        now.Click += (s, e) => AppOps.ExecuteRestart();
        buttons.Controls.Add(later);
        buttons.Controls.Add(now);
        layout.Controls.Add(buttons, 1, 0);
        card.Controls.Add(layout);
        installBanner.Controls.Add(card);
        installBanner.BringToFront();
    }

    private void OnInstallFinished(int exitCode)
    {
        if (step != 3) return;
        if (exitCode == 0 && installState != OperationState.Failed)
        {
            foreach (var stage in StageOrder)
            {
                StageRow row;
                if (installRows.TryGetValue(stage, out row) && row.Status != StageStatus.Success && row.Status != StageStatus.Skipped && row.Status != StageStatus.Warning)
                    row.SetState(StageStatus.Success, "完成", "");
            }
            progressBar.Value = 100;
            ShowStep(4);
            return;
        }
        if (exitCode == 3010)
        {
            AppOps.RegisterRestartResume(pathBox != null ? pathBox.Text.Trim() : DefaultInstallPath());
            ShowInstallRestart();
            return;
        }
        if (installState != OperationState.Failed)
        {
            installState = OperationState.Failed;
            ShowInstallFailure(ActiveInstallStageRow());
        }
        // Offer retry in the failure card area.
        if (failureCard.Visible && failureCard.Controls.Count > 0)
        {
            // failureCard keeps its own buttons; add retry to banner row instead.
        }
        var retryBtn = new ModernButton
        {
            Text = "重试安装",
            IconName = "refresh-cw",
            Variant = ModernButton.ButtonVariant.LightSecondary,
            CustomBorderColor = Ui.LightBorder,
            Width = Ui.Px(120),
            Height = Ui.Px(36),
            Anchor = AnchorStyles.Top | AnchorStyles.Right
        };
        retryBtn.Location = new Point(failureCard.Right - retryBtn.Width - Ui.Px(16), failureCard.Bottom + Ui.Px(6));
        var holder = failureCard.Parent;
        if (holder != null)
        {
            retryBtn.Click += (s, e) => { holder.Controls.Remove(retryBtn); installState = OperationState.Running; failureCard.Visible = false; StartInstall(pathBox.Text.Trim(), shortcutCheck != null && shortcutCheck.Checked); };
            holder.Controls.Add(retryBtn);
            retryBtn.BringToFront();
        }
    }

    // ---- step 5: completion ------------------------------------------------------
    private void BuildComplete()
    {
        var page = NewPage();
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1 };
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(72)));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var titleRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        titleRow.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        titleRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(56)));
        titleRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        var iconBox = new IconBox("circle-check", Color.FromArgb(0x16, 0xA3, 0x4A), 2.2F);
        iconBox.Dock = DockStyle.Fill;
        titleRow.Controls.Add(iconBox, 0, 0);
        titleRow.Controls.Add(new Label { Text = "安装完成", Font = Ui.Font(28, true), ForeColor = Ui.LightText, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 1, 0);
        body.Controls.Add(titleRow, 0, 0);

        var card = new Card { Dock = DockStyle.Top, Height = Ui.Px(190), SurfaceColor = Color.White, BorderColor = Ui.LightBorder };
        var cardRows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, BackColor = Color.White, Padding = Ui.Pad(16, 8, 16, 8) };
        cardRows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(38)));
        cardRows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(38)));
        cardRows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(38)));
        cardRows.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        cardRows.Controls.Add(SummaryRow("服务状态", "全部服务已启动并通过健康检查"), 0, 0);
        cardRows.Controls.Add(SummaryRow("前端服务", string.IsNullOrEmpty(frontendUrl) ? "已就绪" : "已启动，可通过下方地址访问"), 0, 1);
        var urlRow = SummaryRow("访问地址", frontendUrl.Length > 0 ? frontendUrl : "未获取到地址");
        cardRows.Controls.Add(urlRow, 0, 2);
        var shortcutRow = new CheckBox { Text = "桌面快捷方式", Checked = shortcutCreated == "true", Font = Ui.Font(14, false), ForeColor = Ui.LightText, AutoSize = true, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
        shortcutRow.CheckedChanged += (s, e) =>
        {
            ScriptRunner.RunAsync(packageRoot, "install.ps1", "-ShortcutOnly -CreateDesktopShortcut " + (shortcutRow.Checked ? "True" : "False"), null);
        };
        cardRows.Controls.Add(shortcutRow, 0, 3);
        card.Controls.Add(cardRows);
        body.Controls.Add(card, 0, 1);
        var urlText = new Label
        {
            Text = string.IsNullOrEmpty(frontendUrl) ? "" : frontendUrl,
            Font = Ui.Mono(15),
            ForeColor = Ui.Primary,
            Dock = DockStyle.Top,
            Height = Ui.Px(30),
            TextAlign = ContentAlignment.MiddleLeft,
            Cursor = Cursors.Hand,
            Visible = frontendUrl.Length > 0
        };
        urlText.Click += (s, e) => { if (frontendUrl.Length > 0) AppOps.OpenUrl(frontendUrl); };
        body.Controls.Add(urlText, 0, 2);
        page.Controls.Add(body);
        contentArea.Controls.Add(page);

        AddFooterGhost("完成", "", (s, e) => Close());
        AddFooterPrimary("启动闲鱼管理系统", "square-arrow-out-up-right", (s, e) =>
        {
            if (frontendUrl.Length > 0) AppOps.OpenUrl(frontendUrl);
            else AppOps.OpenUrl("http://127.0.0.1:20000");
        });
    }

    private static Control SummaryRow(string name, string value)
    {
        var row = new TableLayoutPanel { Dock = DockStyle.Top, Height = Ui.Px(34), ColumnCount = 2 };
        row.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        row.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(110)));
        row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        row.Controls.Add(new Label { Text = name, Font = Ui.Font(14, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 0, 0);
        row.Controls.Add(new Label { Text = value, Font = Ui.Font(14, false), ForeColor = Ui.LightText, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true }, 1, 0);
        return row;
    }

    // ---- shared helpers -----------------------------------------------------------
    private static Panel NewPage()
    {
        return new Panel { Dock = DockStyle.Fill, Padding = Ui.Pad(32, 8, 32, 8), BackColor = Color.Transparent };
    }

    private ModernButton AddFooterPrimary(string text, string icon, EventHandler handler)
    {
        var button = new ModernButton { Text = text, IconName = icon, Variant = ModernButton.ButtonVariant.Primary, Width = Ui.Px(200), Height = Ui.Px(40), Anchor = AnchorStyles.Right, Margin = Ui.Pad(0, 24, 0, 0) };
        button.Click += handler;
        footerArea.Controls.Add(button, 2, 0);
        footerPrimary = button;
        return button;
    }

    private ModernButton AddFooterGhost(string text, string icon, EventHandler handler)
    {
        var button = new ModernButton { Text = text, IconName = icon, Variant = ModernButton.ButtonVariant.Ghost, Width = Ui.Px(150), Height = Ui.Px(40), ForeColor = Ui.LightTextSecondary, Anchor = AnchorStyles.Left, Margin = Ui.Pad(0, 24, 0, 0) };
        button.Click += handler;
        footerArea.Controls.Add(button, 0, 0);
        return button;
    }

    private void SetFooterPrimaryEnabled(bool value2)
    {
        if (footerPrimary != null) footerPrimary.Enabled = value2;
    }

    private sealed class RailPanel : Panel
    {
        private readonly string version;

        internal RailPanel(string productVersion)
        {
            version = productVersion;
            BackColor = Ui.RailBackground;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.Clear(Ui.RailBackground);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
            var box = new RectangleF(Ui.Px(32), Ui.Px(80), Ui.Px(64), Ui.Px(64));
            if (Ui.BrandLogo != null)
            {
                // Transparent-background goldfish: draw as-is, never on a filled tile.
                Ui.DrawLogo(g, box);
            }
            else Lucide.DrawLogoMark(g, new RectangleF(Ui.Px(32), Ui.Px(88), Ui.Px(44), Ui.Px(44)), Color.FromArgb(0x23, 0x73, 0xF0));
            TextRenderer.DrawText(g, LauncherText.Product, Ui.Font(20, true), new Rectangle(Ui.Px(32), Ui.Px(150), Width - Ui.Px(64), Ui.Px(34)), Ui.TextPrimary, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
            TextRenderer.DrawText(g, "Windows 桌面部署与管理工具", Ui.Font(14, false), new Rectangle(Ui.Px(32), Ui.Px(190), Width - Ui.Px(64), Ui.Px(26)), Ui.TextSecondary, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
            if (!string.IsNullOrWhiteSpace(version))
                TextRenderer.DrawText(g, "版本 v" + version, Ui.Font(13, false), new Rectangle(Ui.Px(32), Ui.Px(232), Width - Ui.Px(64), Ui.Px(22)), Ui.TextMuted, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
            using (var pen = new Pen(Color.FromArgb(28, 0x27, 0x52, 0x8E), 1F))
                g.DrawLine(pen, Ui.Px(32), Height - Ui.Px(56), Width - Ui.Px(32), Height - Ui.Px(56));
            TextRenderer.DrawText(g, "安装过程不会删除已有数据", Ui.Font(13, false), new Rectangle(Ui.Px(32), Height - Ui.Px(48), Width - Ui.Px(64), Ui.Px(26)), Ui.TextMuted, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
        }
    }

    private sealed class StepIndicator : Panel
    {
        private int current;
        private static readonly string[] Names = { "欢迎", "环境检测", "安装位置", "正在安装", "完成" };

        internal StepIndicator()
        {
            Dock = DockStyle.Top;
            Height = Ui.Px(72);
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            BackColor = Ui.LightBackground;
        }

        internal void SetStep(int index) { current = index; Invalidate(); }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Ui.LightBackground);
            var chipW = Ui.Px(96);
            var chipH = Ui.Px(30);
            var gap = Ui.Px(24);
            var totalW = Names.Length * chipW + (Names.Length - 1) * gap;
            var x = Math.Max(Ui.Px(12), Width - totalW - Ui.Px(12));
            var y = (Height - chipH) / 2;
            for (var i = 0; i < Names.Length; i++)
            {
                var rect = new Rectangle(x + i * (chipW + gap), y, chipW, chipH);
                var isCurrent = i == current;
                var isDone = i < current;
                using (var brush = new SolidBrush(isCurrent ? Color.FromArgb(0xE8, 0xF0, 0xFE) : (isDone ? Color.FromArgb(0xEC, 0xF7, 0xEF) : Color.FromArgb(0xF1, 0xF5, 0xF9))))
                    g.FillRoundedRectangle(brush, rect, Ui.Px(15));
                var color = isCurrent ? Ui.Primary : (isDone ? Color.FromArgb(0x16, 0xA3, 0x4A) : Color.FromArgb(0x94, 0xA3, 0xB8));
                if (isDone)
                {
                    using (var pen = new Pen(color, Math.Max(1.6F, Ui.Px(2) * 1F)))
                    {
                        pen.StartCap = LineCap.Round; pen.EndCap = LineCap.Round;
                        g.DrawLines(pen, new[] { new PointF(rect.X + Ui.Px(14), rect.Y + chipH / 2F), new PointF(rect.X + Ui.Px(19), rect.Y + chipH / 2F + Ui.Px(5)), new PointF(rect.X + Ui.Px(28), rect.Y + chipH / 2F - Ui.Px(5)) });
                    }
                }
                else
                {
                    TextRenderer.DrawText(g, (i + 1).ToString(CultureInfo.InvariantCulture), Ui.Font(13, true), new Rectangle(rect.X + Ui.Px(8), rect.Y, Ui.Px(16), chipH), color, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
                }
                TextRenderer.DrawText(g, isDone ? Names[i] : Names[i], Ui.Font(13, isCurrent), new Rectangle(rect.X + Ui.Px(30), rect.Y, chipW - Ui.Px(34), chipH), isCurrent ? Ui.Primary : color, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
            }
        }
    }
}

internal sealed class IconBox : Control
{
    private readonly string icon;
    private readonly Color color;
    private readonly float stroke;

    internal IconBox(string iconName, Color iconColor) : this(iconName, iconColor, 1.9F) { }

    internal IconBox(string iconName, Color iconColor, float strokeWidth24)
    {
        icon = iconName;
        color = iconColor;
        stroke = strokeWidth24;
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor, true);
        BackColor = Color.Transparent;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Parent != null ? Ui.ResolveBackdrop(Parent) : BackColor);
        float size = Math.Min(Width, Height) * 0.85F;
        if (icon == "__logo")
        {
            if (Ui.BrandLogo != null)
            {
                Ui.DrawLogo(g, new RectangleF(0, 0, Width, Height));
                return;
            }
            Lucide.DrawLogoMark(g, new RectangleF((Width - size) / 2F, (Height - size) / 2F, size, size), Color.FromArgb(0x23, 0x73, 0xF0));
            return;
        }
        Lucide.Draw(g, icon, new RectangleF((Width - size) / 2F, (Height - size) / 2F, size, size), color, stroke);
    }
}

// ---------------------------------------------------------------------------
// Updater: light body under a navy header band; six real stages driven by
// the update worker's structured events. No keyword guessing, no fake checks.
// ---------------------------------------------------------------------------
internal sealed class UpdaterWindow : LauncherWindow
{
    private static readonly string[] UpdateStages = { "fetch", "prepare", "protect", "apply", "restart", "health" };
    private static readonly string[] UpdateStageTitles = { "获取更新", "准备更新", "保护当前版本", "应用更新", "重启服务", "健康检查" };
    private static readonly int[] UpdateWeightStarts = { 0, 10, 20, 30, 70, 85 };
    private static readonly int[] UpdateWeightEnds = { 10, 20, 30, 70, 85, 100 };
    private static readonly string[] UpdateStageIcons = { "refresh-cw", "file-search", "shield", "package", "power", "circle-check" };

    private readonly string packageRoot;
    private readonly string appRoot;
    private readonly string localVersion;
    private readonly string localBuild;

    private readonly Dictionary<string, StageRow> stageRows = new Dictionary<string, StageRow>();
    private readonly Dictionary<string, int> stageProgress = new Dictionary<string, int>();
    private Label versionLine;
    private StatusBadge shieldBadge;
    private Panel bannerArea;
    private Panel rollbackArea;
    private ProgressBoard progressBar;
    private Label progressCaption;
    private Label progressCount;
    private CollapsibleSection logSection;
    private RichTextBox logBox;
    private FailureSummaryCard failureCard;
    private ModernButton laterButton;
    private ModernButton nowButton;

    private OperationState state = OperationState.Idle;
    private string latestVersion = "";
    private string latestBuild = "";
    private string updateReason = "";
    private string notes = "";
    private bool pendingRestartClient;

    internal UpdaterWindow() : base(LauncherText.Updater, true)
    {
        packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        appRoot = Path.Combine(packageRoot, "app");
        localVersion = AppOps.ReadFirstLine(Path.Combine(appRoot, "VERSION.txt"));
        localBuild = AppOps.ReadFirstLine(Path.Combine(appRoot, "BUILD_ID.txt"));
        BackColor = Ui.LightBackground;
        contentHost.BackColor = Ui.LightBackground;

        var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6, BackColor = Ui.LightBackground, Padding = Ui.Pad(32, 12, 32, 16) };
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(110)));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        body.Controls.Add(BuildHero(), 0, 0);
        bannerArea = new Panel { Dock = DockStyle.Fill, Height = Ui.Px(0), Margin = new Padding(0, Ui.Px(12), 0, 0) };
        body.Controls.Add(bannerArea, 0, 1);
        var progressPanel = new TableLayoutPanel { Dock = DockStyle.Top, Height = Ui.Px(52), ColumnCount = 2, RowCount = 2, Margin = new Padding(0, Ui.Px(12), 0, 0) };
        progressPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        progressPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        progressPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(24)));
        progressPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        progressCaption = new Label { Text = "正在检查更新…", Font = Ui.Font(14, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true };
        progressCount = new Label { Text = "", Font = Ui.Font(13, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, AutoSize = false, TextAlign = ContentAlignment.MiddleRight };
        progressBar = new ProgressBoard { Dark = false, Dock = DockStyle.Fill, Value = -1 };
        progressPanel.Controls.Add(progressCaption, 0, 0);
        progressPanel.Controls.Add(progressCount, 1, 0);
        progressPanel.Controls.Add(progressBar, 0, 1);
        progressPanel.SetColumnSpan(progressBar, 2);
        body.Controls.Add(progressPanel, 0, 2);

        var stagesScroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, BackColor = Ui.LightBackground };
        var stagesCard = new Card { SurfaceColor = Color.White, BorderColor = Ui.LightBorder, Location = new Point(0, 0) };
        var rows = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6, BackColor = Color.White, Padding = Ui.Pad(12, 2, 12, 2) };
        for (var i = 0; i < 6; i++) { rows.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(64))); }
        for (var i = 0; i < UpdateStages.Length; i++)
        {
            var row = new StageRow(UpdateStageTitles[i], UpdateStageIcons[i]);
            row.SetState(StageStatus.Pending, "等待开始", "");
            stageRows[UpdateStages[i]] = row;
            rows.Controls.Add(row, 0, i);
        }
        stagesCard.Controls.Add(rows);
        stagesScroll.Controls.Add(stagesCard);
        stagesCard.Size = new Size(Ui.Px(1216), Ui.Px(396));
        stagesScroll.Resize += delegate { stagesCard.Width = Math.Max(Ui.Px(320), stagesScroll.ClientSize.Width); };
        body.Controls.Add(stagesScroll, 0, 4);

        rollbackArea = new Panel { Dock = DockStyle.Bottom, Height = Ui.Px(56), Visible = false };
        bannerArea.Controls.Add(rollbackArea);
        failureCard = new FailureSummaryCard("更新未完成", Path.Combine(appRoot, "logs", "update.log"), packageRoot, "updater")
        {
            Dock = DockStyle.Top,
            Height = Ui.Px(140),
            Visible = false
        };
        bannerArea.Controls.Add(failureCard);

        logSection = new CollapsibleSection("查看详细日志");
        logSection.SetExpandedHeight(200);
        logBox = new RichTextBox
        {
            Dock = DockStyle.Fill,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            BackColor = Color.White,
            ForeColor = Color.FromArgb(0x47, 0x55, 0x69),
            Font = Ui.Mono(12),
            WordWrap = false
        };
        logSection.Body.Controls.Add(logBox);
        logSection.Dock = DockStyle.Fill;
        body.Controls.Add(logSection, 0, 5);

        var buttonRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        buttonRow.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        buttonRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        buttonRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        laterButton = new ModernButton { Text = "稍后更新", Variant = ModernButton.ButtonVariant.Ghost, Width = Ui.Px(130), Height = Ui.Px(40), ForeColor = Ui.LightTextSecondary, Margin = new Padding(0, 0, Ui.Px(8), 0) };
        laterButton.Click += (s, e) => Close();
        nowButton = new ModernButton { Text = "立即更新", IconName = "refresh-cw", Variant = ModernButton.ButtonVariant.Primary, Width = Ui.Px(150), Height = Ui.Px(40) };
        nowButton.Click += OnPrimaryClick;
        var pairFlow = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, WrapContents = false, AutoSize = true };
        pairFlow.Controls.Add(nowButton);
        pairFlow.Controls.Add(laterButton);
        buttonRow.Controls.Add(pairFlow, 1, 0);
        var footerWrap = new Panel { Dock = DockStyle.Fill };
        footerWrap.Controls.Add(buttonRow);

        var outerRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        outerRow.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        outerRow.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(76)));
        outerRow.Controls.Add(body, 0, 0);
        outerRow.Controls.Add(footerWrap, 0, 1);
        SetContent(outerRow);

        Shown += (s, e) => StartCheck();
    }

    private Control BuildHero()
    {
        var hero = new HeroBand();
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Color.Transparent };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        layout.Padding = Ui.Pad(20, 14, 20, 14);
        var textBlock = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, BackColor = Color.Transparent };
        textBlock.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(30)));
        textBlock.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(24)));
        textBlock.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var titleLine = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        titleLine.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        titleLine.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(44)));
        titleLine.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        var logo = new IconBox("__logo", Color.Transparent, 0F) { Dock = DockStyle.Fill };
        titleLine.Controls.Add(logo, 0, 0);
        titleLine.Controls.Add(new Label { Text = LauncherText.Product + "更新", Font = Ui.Font(20, true), ForeColor = Ui.TextPrimary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 1, 0);
        versionLine = new Label
        {
            Text = "当前版本 v" + (string.IsNullOrEmpty(localVersion) ? "未知" : localVersion) + (string.IsNullOrEmpty(localBuild) ? "" : " · " + localBuild),
            Font = Ui.Font(13, false),
            ForeColor = Ui.TextSecondary,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft
        };
        textBlock.Controls.Add(titleLine, 0, 0);
        textBlock.Controls.Add(versionLine, 0, 1);
        shieldBadge = new StatusBadge { Dock = DockStyle.Right, Width = Ui.Px(260), AutoSize = false };
        shieldBadge.Set("回滚保护：检测中…", Color.FromArgb(0x64, 0x74, 0x8B), Ui.TextSecondary);
        var rightBlock = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 1 };
        rightBlock.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        rightBlock.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var shieldWrap = new Panel { Dock = DockStyle.Fill, Width = Ui.Px(280) };
        shieldWrap.Controls.Add(shieldBadge);
        shieldBadge.Top = (shieldWrap.Height - Ui.Px(20)) / 2;
        layout.Controls.Add(textBlock, 0, 0);
        layout.Controls.Add(shieldWrap, 1, 0);
        hero.Controls.Add(layout);
        return hero;
    }

    private sealed class HeroBand : Panel
    {
        internal HeroBand()
        {
            Dock = DockStyle.Fill;
            BackColor = Ui.Surface;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Ui.Surface);
            using (var pen = new Pen(Ui.Border)) { g.DrawLine(pen, 0, Height - 1, Width, Height - 1); }
            var watermark = new RectangleF(Width - Ui.Px(160), Height - Ui.Px(104), Ui.Px(100), Ui.Px(100));
            if (Ui.BrandLogo != null)
            {
                // Real logo at ~10% alpha, no tile behind it.
                Ui.DrawLogo(g, watermark, 0.10F);
            }
            else Lucide.DrawLogoMark(g, watermark, Color.FromArgb(24, 0x27, 0x52, 0x8E));
        }
    }

    private void OnPrimaryClick(object sender, EventArgs e)
    {
        switch (state)
        {
            case OperationState.Ready:
                StartUpdate();
                break;
            case OperationState.Failed:
                StartUpdate();
                break;
            case OperationState.Completed:
                Close();
                break;
            case OperationState.Idle:
                if (latestVersion.Length > 0) { } // already checked: nothing to do
                break;
            case OperationState.RollingBack:
            case OperationState.Running:
                break;
        }
    }

    private void StartCheck()
    {
        if (!IsInstalledRuntime())
        {
            ShowNotInstalled();
            return;
        }
        SetState(OperationState.Checking);
        ScriptRunner.RunAsync(packageRoot, "update.ps1", "-Headless -CheckOnly", OnLine).ContinueWith(t =>
        {
            try { BeginInvoke((MethodInvoker)(() => OnScriptFinished(t.Result == null ? 1 : t.Result.ExitCode, true))); } catch { }
        });
    }

    private void StartUpdate()
    {
        if (!IsInstalledRuntime())
        {
            ShowNotInstalled();
            return;
        }
        ResetStages();
        SetState(OperationState.Running);
        ScriptRunner.RunAsync(packageRoot, "update.ps1", "-Headless", OnLine).ContinueWith(t =>
        {
            try { BeginInvoke((MethodInvoker)(() => OnScriptFinished(t.Result == null ? 1 : t.Result.ExitCode, false))); } catch { }
        });
    }

    private bool IsInstalledRuntime()
    {
        return File.Exists(Path.Combine(appRoot, "docker-compose.yml"))
            && File.Exists(Path.Combine(appRoot, ".env"));
    }

    private void ShowNotInstalled()
    {
        ResetStages();
        foreach (var pair in stageRows)
            pair.Value.SetState(StageStatus.Skipped, "尚未安装，未执行更新检查", "");
        ApplyCapability("unavailable:config_missing");
        ShowBanner(StageStatus.Warning, "当前目录尚未完成安装，请先运行“安装闲鱼管理系统.exe”", "E_NOT_INSTALLED");
        state = OperationState.Completed;
        nowButton.Enabled = true;
        nowButton.SetLoading(false);
        nowButton.Text = "关闭";
        nowButton.IconName = "";
        laterButton.Visible = false;
        progressBar.Value = 100;
        progressCaption.Text = "未安装，已跳过更新检查";
    }

    private void ResetStages()
    {
        foreach (var pair in stageRows) { pair.Value.ClearActions(); pair.Value.SetState(StageStatus.Pending, "等待开始", ""); }
        stageProgress.Clear();
        rollbackShown = false;
        failureShown = false;
        rollbackArea.Visible = false;
        rollbackArea.Controls.Clear();
        foreach (Control c in AppOps.Snapshot(bannerArea.Controls))
            if (c != rollbackArea && c != failureCard) bannerArea.Controls.Remove(c);
        failureCard.Visible = false;
        bannerShown = false;
        ResizeBannerArea();
        logSection.SetExpanded(false);
    }

    private void SetState(OperationState value2)
    {
        AppOps.Trace("setstate " + value2 + " (prev state=" + state + ", nowButton='" + nowButton.Text + "')");
        state = value2;
        switch (value2)
        {
            case OperationState.Checking:
                nowButton.Enabled = false;
                nowButton.SetLoading(true);
                nowButton.Text = "正在检查更新";
                laterButton.Enabled = true;
                laterButton.Text = "取消";
                progressCaption.Text = "正在检查更新…";
                progressBar.Value = -1;
                break;
            case OperationState.Ready:
                nowButton.Enabled = true;
                nowButton.SetLoading(false);
                nowButton.Text = "立即更新";
                nowButton.IconName = "refresh-cw";
                laterButton.Enabled = true;
                laterButton.Text = "稍后更新";
                progressBar.Value = 4;
                progressCaption.Text = "发现可用更新";
                break;
            case OperationState.Running:
                nowButton.Enabled = false;
                nowButton.SetLoading(true);
                nowButton.Text = "正在更新";
                laterButton.Enabled = false;
                break;
            case OperationState.RollingBack:
                progressBar.Value = -1;
                progressCaption.Text = "正在恢复原版本…";
                break;
        }
    }

    private void OnLine(string line)
    {
        if (line == null) return;
        AppOps.Trace("updater line: " + (line.Length > 160 ? line.Substring(0, 160) : line));
        GuiEvent evt;
        if (GuiProtocol.TryParse(line, out evt))
        {
            try { BeginInvoke((MethodInvoker)(() => ApplyEvent(evt))); } catch (Exception ex) { AppOps.Trace("updater begininvoke failed: " + ex.Message); }
            return;
        }
        AppendLog(line);
    }

    private void AppendLog(string line)
    {
        if (logBox == null) return;
        try
        {
            BeginInvoke((MethodInvoker)(() =>
            {
                if (logBox.IsDisposed) return;
                if (logBox.TextLength > 400000) logBox.Clear();
                logBox.AppendText(line + Environment.NewLine);
                logBox.SelectionStart = logBox.TextLength;
                logBox.ScrollToCaret();
            }));
        }
        catch { }
    }

    private void ApplyEvent(GuiEvent evt)
    {
        AppOps.Trace("updater apply type=" + evt.Type + " stage=" + evt.Stage + " result=" + evt.ResultStatus + " status=" + evt.Status);
        if (evt.Type == "meta")
        {
            switch (evt.Key)
            {
                case "latest_version": latestVersion = evt.Value; UpdateVersionLine(); break;
                case "latest_build": latestBuild = evt.Value; UpdateVersionLine(); break;
                case "update_reason": updateReason = evt.Value; break;
                case "notes": notes = evt.Value; break;
                case "rollback_capability": ApplyCapability(evt.Value); break;
            }
            return;
        }
        if (evt.Type == "stage")
        {
            StageRow row;
            if (evt.Stage == "rollback")
            {
                ApplyRollbackEvent(evt);
                return;
            }
            if (!stageRows.TryGetValue(evt.Stage, out row)) return;
            stageProgress[evt.Stage] = evt.Progress;
            if (evt.HasStatus) row.SetState(evt.Status, string.IsNullOrEmpty(evt.Detail) ? InstallerWindow.StatusWord(evt.Status) : evt.Detail, evt.Code);
            else row.DetailLabel.Text = evt.Detail;
            UpdateProgress();
            return;
        }
        if (evt.Type == "result")
        {
            switch (evt.ResultStatus)
            {
                case "available":
                    SetState(OperationState.Ready);
                    ShowBanner(StageStatus.Running, ReasonText(), "");
                    break;
                case "latest":
                    ShowBanner(StageStatus.Success, string.IsNullOrEmpty(evt.Detail) ? "当前已是最新版本" : evt.Detail, "");
                    state = OperationState.Idle;
                    nowButton.Enabled = true;
                    nowButton.SetLoading(false);
                    nowButton.Text = "关闭";
                    nowButton.IconName = "";
                    nowButton.Variant = ModernButton.ButtonVariant.Primary;
                    laterButton.Visible = false;
                    progressBar.Value = 100;
                    progressCaption.Text = "无需更新";
                    foreach (var pair in stageRows) pair.Value.SetState(StageStatus.Skipped, "无需执行", "");
                    break;
                case "completed":
                    ShowBanner(StageStatus.Success, "更新完成，当前版本 v" + latestVersion, "");
                    state = OperationState.Completed;
                    nowButton.Enabled = true;
                    nowButton.SetLoading(false);
                    nowButton.Text = "关闭";
                    nowButton.IconName = "circle-check";
                    nowButton.Variant = ModernButton.ButtonVariant.Primary;
                    laterButton.Visible = false;
                    progressBar.Value = 100;
                    progressCaption.Text = "更新完成";
                    break;
                case "restart_client":
                    pendingRestartClient = true;
                    ShowBanner(StageStatus.Warning, "更新包已下载，重新启动闲鱼管理系统后应用新版界面", "");
                    state = OperationState.Completed;
                    nowButton.Enabled = true;
                    nowButton.SetLoading(false);
                    nowButton.Text = "关闭";
                    nowButton.IconName = "";
                    laterButton.Visible = false;
                    progressBar.Value = 100;
                    break;
                case "rolled_back":
                    ShowFailure("safe", evt);
                    break;
                case "rollback_failed":
                    ShowFailure("rollback_failed", evt);
                    break;
                case "failed":
                    ShowFailure("failed", evt);
                    break;
            }
        }
    }

    private void UpdateVersionLine()
    {
        var left = "当前版本 v" + (string.IsNullOrEmpty(localVersion) ? "未知" : localVersion) + (string.IsNullOrEmpty(localBuild) ? "" : " · " + localBuild);
        if (latestVersion.Length > 0)
            left += "    →    最新版本 v" + latestVersion + (string.IsNullOrEmpty(latestBuild) ? "" : " · " + latestBuild);
        versionLine.Text = left;
    }

    private void ApplyCapability(string capability)
    {
        if (capability == "available") shieldBadge.Set("回滚保护 已开启", Ui.Success, Ui.TextSecondary);
        else if (capability.StartsWith("unavailable"))
        {
            var reason = capability.Length > 12 ? capability.Substring(12) : capability;
            shieldBadge.Set("回滚保护 受限：" + CapabilityReasonText(reason), Ui.Warning, Ui.Warning);
        }
        else shieldBadge.Set("回滚保护：检测中…", Color.FromArgb(0x64, 0x74, 0x8B), Ui.TextSecondary);
    }

    private static string CapabilityReasonText(string reason)
    {
        if (reason.Contains("image_missing")) return "部分旧版本镜像已不在本机";
        if (reason.Contains("config_missing")) return "配置文件不完整";
        if (reason.Contains("snapshot_failed")) return "快照保存失败";
        return reason;
    }

    private string ReasonText()
    {
        switch (updateReason)
        {
            case "version": return "发现新版本 v" + latestVersion + "，本次为正式版本升级。";
            case "build": return "版本未变化，但检测到新的维护构建。";
            case "client_repair": return "检测到本机客户端文件缺失，需要修复安装。";
            case "runtime_sync": return "当前版本尚未完成运行时镜像同步。";
            default: return "发现可用更新。";
        }
    }

    private void UpdateProgress()
    {
        int doneCount = 0;
        bool running = false;
        for (var i = 0; i < UpdateStages.Length; i++)
        {
            StageRow row;
            if (!stageRows.TryGetValue(UpdateStages[i], out row)) continue;
            if (row.Status == StageStatus.Success || row.Status == StageStatus.Skipped || row.Status == StageStatus.Warning)
            {
                doneCount++;
                if (!running) progressBar.Value = UpdateWeightEnds[i];
                continue;
            }
            if (row.Status == StageStatus.Failed) { progressBar.Value = UpdateWeightStarts[i]; break; }
            if (row.Status == StageStatus.Running)
            {
                running = true;
                int reported;
                stageProgress.TryGetValue(UpdateStages[i], out reported);
                progressCaption.Text = row.DetailLabel.Text;
                if (reported >= 0) progressBar.Value = UpdateWeightStarts[i] + (UpdateWeightEnds[i] - UpdateWeightStarts[i]) * Math.Min(100, reported) / 100;
                else progressBar.Value = -1;
                break;
            }
            break;
        }
        if (state == OperationState.Running || state == OperationState.Ready || state == OperationState.RollingBack)
            progressCount.Text = "已完成 " + doneCount + " / " + UpdateStages.Length;
    }

    private void ApplyRollbackEvent(GuiEvent evt)
    {
        if (evt.HasStatus && evt.Status == StageStatus.Running)
        {
            SetState(OperationState.RollingBack);
            ShowRollbackCard(StageStatus.Failed, "更新出现问题，正在恢复原版本…");
        }
    }

    private void ShowRollbackCard(StageStatus severity, string text)
    {
        rollbackShown = true;
        ResizeBannerArea();
        rollbackArea.Visible = true;
        rollbackArea.Controls.Clear();
        var card = new Card
        {
            Dock = DockStyle.Fill,
            SurfaceColor = severity == StageStatus.Failed ? Color.FromArgb(0xFF, 0xF7, 0xF7) : Color.FromArgb(0xFF, 0xFB, 0xEB),
            BorderColor = severity == StageStatus.Failed ? Color.FromArgb(0xFE, 0xD9, 0xD9) : Color.FromArgb(0xFD, 0xE6, 0x8A)
        };
        var inner = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = card.SurfaceColor, Padding = Ui.Pad(14, 0, 14, 0) };
        inner.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        inner.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(36)));
        inner.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        inner.Controls.Add(new IconBox(severity == StageStatus.Failed ? "circle-x" : "triangle-alert", severity == StageStatus.Failed ? Color.FromArgb(0xDC, 0x26, 0x26) : Color.FromArgb(0xB4, 0x53, 0x09)) { Dock = DockStyle.Fill }, 0, 0);
        inner.Controls.Add(new Label { Text = text, Font = Ui.Font(14, true), ForeColor = severity == StageStatus.Failed ? Color.FromArgb(0xB9, 0x1C, 0x1C) : Color.FromArgb(0xB4, 0x53, 0x09), Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 1, 0);
        card.Controls.Add(inner);
        rollbackArea.Controls.Add(card);
    }

    private void ShowFailure(string mode, GuiEvent evt)
    {
        AppOps.Trace("showfailure enter mode=" + mode);
        state = OperationState.Failed;
        nowButton.Enabled = true;
        nowButton.SetLoading(false);
        laterButton.Visible = true;
        laterButton.Enabled = true;
        progressBar.Value = -1;
        failureShown = true;
        failureCard.Visible = true;
        failureCard.Show("更新未完成", evt.Code, string.IsNullOrEmpty(evt.Detail) ? "更新失败，当前版本未受影响。可查看下方阶段状态或展开详细日志。" : evt.Detail);
        AppOps.Trace("showfailure after card visible");
        if (mode == "safe")
        {
            ShowBanner(StageStatus.Warning, "更新未完成，已安全恢复到版本 v" + (string.IsNullOrEmpty(localVersion) ? "原" : localVersion), "");
            ShowRollbackCard(StageStatus.Warning, "当前保持原版本运行，系统仍可正常使用");
            progressBar.Value = 100;
            progressCaption.Text = "已恢复到原版本";
            nowButton.Text = "重试";
        }
        else if (mode == "rollback_failed")
        {
            ShowBanner(StageStatus.Failed, "更新失败，自动恢复未完成", evt.Code);
            ShowRollbackCard(StageStatus.Failed, "部分服务可能没有正常启动，请运行自动诊断");
            progressCaption.Text = "更新未完成";
            nowButton.Text = "运行自动诊断";
            nowButton.IconName = "file-search";
            nowButton.Variant = ModernButton.ButtonVariant.DangerSolid;
            nowButton.Click -= OnPrimaryClick;
            nowButton.Click += (s, e) => AppOps.LaunchPackageExecutable(packageRoot, "xianyu-diagnostics", LauncherText.Diagnostics, "--diagnostics", "", "");
            laterButton.Text = "查看日志";
            laterButton.Click -= OnLaterClose;
            laterButton.Click += OnLaterClose;
            laterButton.Enabled = true;
        }
        else
        {
            ShowBanner(StageStatus.Failed, "更新失败，当前版本未受影响", evt.Code);
            progressCaption.Text = "更新未完成";
            nowButton.Text = "重试";
        }
        AppOps.Trace("showfailure exit nowButton='" + nowButton.Text + "' laterButton='" + laterButton.Text + "'");
    }

    private void OnLaterClose(object sender, EventArgs e)
    {
        try
        {
            var logPath = Path.Combine(appRoot, "logs", "update.log");
            if (File.Exists(logPath)) Process.Start(new ProcessStartInfo { FileName = "notepad.exe", Arguments = "\"" + logPath + "\"", UseShellExecute = false });
            else AppOps.OpenFolder(Path.Combine(appRoot, "logs"));
        }
        catch { }
    }

    private bool bannerShown;
    private bool rollbackShown;
    private bool failureShown;

    private void ResizeBannerArea()
    {
        int h = (bannerShown ? Ui.Px(60) : 0) + (failureShown ? Ui.Px(140) : 0) + (rollbackShown ? Ui.Px(56) : 0);
        bannerArea.Height = h;
        bannerArea.Margin = new Padding(0, h > 0 ? Ui.Px(12) : 0, 0, 0);
    }

    private void ShowBanner(StageStatus severity, string text, string code)
    {
        foreach (Control c in AppOps.Snapshot(bannerArea.Controls))
            if (c != rollbackArea && c != failureCard) bannerArea.Controls.Remove(c);
        bannerShown = text.Length > 0;
        if (!bannerShown) { ResizeBannerArea(); return; }
        var card = new Card
        {
            Dock = DockStyle.Top,
            Height = Ui.Px(60),
            SurfaceColor = severity == StageStatus.Failed ? Color.FromArgb(0xFF, 0xF7, 0xF7) : (severity == StageStatus.Warning ? Color.FromArgb(0xFF, 0xFB, 0xEB) : (severity == StageStatus.Success ? Color.FromArgb(0xF0, 0xFD, 0xF5) : Color.White)),
            BorderColor = severity == StageStatus.Failed ? Color.FromArgb(0xFE, 0xD9, 0xD9) : (severity == StageStatus.Warning ? Color.FromArgb(0xFD, 0xE6, 0x8A) : (severity == StageStatus.Success ? Color.FromArgb(0xBB, 0xF7, 0xD0) : Ui.LightBorder))
        };
        var inner = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = card.SurfaceColor, Padding = Ui.Pad(14, 0, 14, 0) };
        inner.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        inner.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(36)));
        inner.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        var glyphColor = severity == StageStatus.Failed ? Color.FromArgb(0xDC, 0x26, 0x26) : (severity == StageStatus.Warning ? Color.FromArgb(0xB4, 0x53, 0x09) : (severity == StageStatus.Success ? Color.FromArgb(0x16, 0xA3, 0x4A) : Ui.Primary));
        var glyph = severity == StageStatus.Failed ? "circle-x" : (severity == StageStatus.Warning ? "triangle-alert" : (severity == StageStatus.Success ? "circle-check" : "info"));
        inner.Controls.Add(new IconBox(glyph, glyphColor) { Dock = DockStyle.Fill }, 0, 0);
        var showNotes = severity == StageStatus.Running && notes.Length > 0;
        var textBlock = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = showNotes ? 2 : 1, ColumnCount = 1 };
        if (showNotes) { textBlock.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(24))); textBlock.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); }
        else textBlock.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        textBlock.Controls.Add(new Label { Text = text + (string.IsNullOrEmpty(code) ? "" : "（" + code + "）"), Font = Ui.Font(14, false), ForeColor = glyphColor, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true }, 0, 0);
        if (showNotes) textBlock.Controls.Add(new Label { Text = notes, Font = Ui.Font(13, false), ForeColor = Ui.LightTextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, AutoEllipsis = true }, 0, 1);
        inner.Controls.Add(textBlock, 1, 0);
        card.Controls.Add(inner);
        bannerArea.Controls.Add(card);
        card.SendToBack();
        ResizeBannerArea();
    }

    private void OnScriptFinished(int exitCode, bool wasCheck)
    {
        AppOps.Trace("updater script finished exit=" + exitCode + " check=" + wasCheck + " state=" + state);
        if (wasCheck)
        {
            if (state == OperationState.Ready) return;
            if (state == OperationState.Checking)
            {
                // no usable result reached the GUI (script crashed early)
                ShowBanner(StageStatus.Failed, "更新检查未完成，请查看日志", exitCode == 0 ? "" : "E_UPDATE");
                state = OperationState.Failed;
                nowButton.Enabled = true;
                nowButton.SetLoading(false);
                nowButton.Text = "重新检查";
                nowButton.IconName = "refresh-cw";
                nowButton.Click -= OnPrimaryClick;
                nowButton.Click += OnPrimaryClick;
                laterButton.Enabled = true;
                laterButton.Text = "关闭";
            }
            return;
        }
        if (state == OperationState.Completed) return;
        if (exitCode == 0 && state != OperationState.Failed && state != OperationState.RollingBack)
        {
            ShowBanner(StageStatus.Success, "更新完成", "");
            state = OperationState.Completed;
            nowButton.Enabled = true;
            nowButton.SetLoading(false);
            nowButton.Text = "关闭";
            nowButton.IconName = "";
            laterButton.Visible = false;
            progressBar.Value = 100;
            return;
        }
        if (state != OperationState.Failed)
        {
            // failure without a terminal result event (early crash): still surface summary
            ShowFailure("failed", new GuiEvent { Code = "E_UPDATE", Detail = "更新未完成（exit " + exitCode + "）" });
        }
    }
}

// ---------------------------------------------------------------------------
// Service console: one page, real container health from scripts\status.ps1.
// No sidebar, no decorative illustration, no fake timestamps.
// ---------------------------------------------------------------------------
internal sealed class DashboardWindow : LauncherWindow
{
    private static readonly string[] CardKeys = { "frontend", "backend", "websocket", "scheduler" };
    private static readonly string[] CardTitles = { "前端服务", "后端服务", "消息服务", "定时任务" };
    private static readonly string[] CardIcons = { "monitor", "server", "message-square", "clock-3" };

    private readonly string packageRoot;
    private readonly string appRoot;
    private readonly Dictionary<string, ServiceCard> cards = new Dictionary<string, ServiceCard>();

    private StatusBadge systemStatus;
    private ModernButton openBackend;
    private ActivitySurface activitySurface;
    private Label versionLabel;
    private LinkLabel updateLink;
    private ModernButton stopButton;
    private TableLayoutPanel gridFour;
    private TableLayoutPanel gridTwo;
    private Panel gridHost;
    private bool wide = true;
    private bool statusInFlight;
    private bool actionInFlight;
    private bool startFinished;
    private bool probeDone;
    private string frontendUrl = "";
    private long lastProbeTicks;
    private readonly Timer pollTimer = new Timer();

    internal DashboardWindow() : base(LauncherText.Product, false)
    {
        packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        appRoot = Path.Combine(packageRoot, "app");
        BackColor = Ui.Background;
        contentHost.BackColor = Ui.Background;

        var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, BackColor = Ui.Background, Padding = Ui.Pad(24, 20, 24, 10) };
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(108)));
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(104)));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(32)));

        // header
        var header = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Ui.Background };
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var headText = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, BackColor = Ui.Background };
        headText.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(42)));
        headText.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(26)));
        headText.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(26)));
        headText.Controls.Add(new Label { Text = "服务控制台", Font = Ui.Font(28, true), ForeColor = Ui.TextPrimary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, Margin = new Padding(0) }, 0, 0);
        headText.Controls.Add(new Label { Text = "查看本地服务运行状态", Font = Ui.Font(14, false), ForeColor = Ui.TextSecondary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, Margin = new Padding(0) }, 0, 1);
        systemStatus = new StatusBadge { Dock = DockStyle.Fill, Margin = new Padding(0) };
        systemStatus.Set("正在启动服务并检查更新…", Ui.TextMuted, Ui.TextSecondary);
        headText.Controls.Add(systemStatus, 0, 2);
        openBackend = new ModernButton { Text = "打开管理后台", IconName = "square-arrow-out-up-right", Variant = ModernButton.ButtonVariant.Primary, Width = Ui.Px(176), Height = Ui.Px(40), Margin = new Padding(0, Ui.Px(28), 0, 0) };
        openBackend.Click += (s, e) => OpenAdmin();
        header.Controls.Add(headText, 0, 0);
        header.Controls.Add(openBackend, 1, 0);
        body.Controls.Add(header, 0, 0);

        // service cards grid (4-wide ⇄ 2×2 responsive)
        gridHost = new Panel { Dock = DockStyle.Fill, BackColor = Ui.Background };
        gridFour = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, RowCount = 1, BackColor = Ui.Background };
        gridFour.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        gridFour.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25F));
        gridFour.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25F));
        gridFour.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25F));
        gridFour.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25F));
        gridTwo = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2, BackColor = Ui.Background, Visible = false };
        gridTwo.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
        gridTwo.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
        gridTwo.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));
        gridTwo.RowStyles.Add(new RowStyle(SizeType.Percent, 50F));
        gridHost.Controls.Add(gridFour);
        gridHost.Controls.Add(gridTwo);
        body.Controls.Add(gridHost, 0, 1);

        // lower columns: quick actions | recent activity
        var lower = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Ui.Background };
        lower.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        lower.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(400)));
        lower.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(16)));
        lower.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        lower.Controls.Add(BuildQuickActions(), 0, 0);
        activitySurface = new ActivitySurface { Dock = DockStyle.Fill };
        activitySurface.ViewAllClicked += delegate { AppOps.OpenFolder(Path.Combine(appRoot, "logs")); };
        lower.Controls.Add(activitySurface, 2, 0);
        body.Controls.Add(lower, 0, 2);

        // footer version line
        var footer = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, BackColor = Ui.Background };
        footer.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        versionLabel = new Label { Text = "", Font = Ui.Font(13, false), ForeColor = Ui.TextMuted, AutoSize = false, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
        updateLink = new LinkLabel { Text = "", Visible = false, AutoSize = true, Font = Ui.Font(13, false), LinkColor = Ui.PrimaryHover, ActiveLinkColor = Ui.Primary, Margin = new Padding(Ui.Px(12), Ui.Px(6), 0, 0) };
        updateLink.LinkClicked += (s, e) => LaunchUpdater();
        footer.Controls.Add(versionLabel, 0, 0);
        footer.Controls.Add(updateLink, 1, 0);
        body.Controls.Add(footer, 0, 3);

        for (var i = 0; i < CardKeys.Length; i++)
        {
            var card = new ServiceCard(CardTitles[i], CardIcons[i]);
            cards[CardKeys[i]] = card;
            gridFour.Controls.Add(card, i, 0);
            gridTwo.Controls.Add(card, i % 2, i / 2);
        }

        SetContent(body);

        Shown += OnShownOnce;
        pollTimer.Interval = 15000;
        pollTimer.Tick += (s, e) => RefreshStatus(false);
        RefreshLayoutMode();
    }

    private Control BuildQuickActions()
    {
        var card = new Card { Dock = DockStyle.Fill, SurfaceColor = Ui.Surface };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6, BackColor = Ui.Surface, Padding = Ui.Pad(16, 12, 16, 12) };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(30)));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(48)));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(48)));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(34)));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(48)));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.Controls.Add(SectionTitle("快捷操作"), 0, 0);
        var diag = new ModernButton { Text = "诊断与日志", IconName = "file-search", TrailingIconName = "chevron-right", Variant = ModernButton.ButtonVariant.Row, Dock = DockStyle.Fill, Margin = Ui.Pad(0, 0, 0, 0) };
        diag.Click += (s, e) => RunDiagnostics();
        layout.Controls.Add(diag, 0, 1);
        var upd = new ModernButton { Text = "检查更新", IconName = "refresh-cw", TrailingIconName = "chevron-right", Variant = ModernButton.ButtonVariant.Row, Dock = DockStyle.Fill };
        upd.Click += (s, e) => LaunchUpdater();
        layout.Controls.Add(upd, 0, 2);
        var dangerTitle = SectionTitle("危险操作");
        dangerTitle.ForeColor = Ui.TextMuted;
        layout.Controls.Add(dangerTitle, 0, 3);
        stopButton = new ModernButton { Text = "停止所有服务", IconName = "circle-stop", TrailingIconName = "chevron-right", Variant = ModernButton.ButtonVariant.Row, Dock = DockStyle.Fill, CustomForeColor = Ui.Danger };
        stopButton.Click += (s, e) => ConfirmStop();
        layout.Controls.Add(stopButton, 0, 4);
        card.Controls.Add(layout);
        return card;
    }

    private static Label SectionTitle(string text)
    {
        return new Label { Text = text, Font = Ui.Font(16, true), ForeColor = Ui.TextPrimary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
    }

    private void OnShownOnce(object sender, EventArgs e)
    {
        startFinished = false;
        systemStatus.Set("正在启动服务并检查更新…", Ui.Warning, Ui.TextSecondary);
        ScriptRunner.RunAsync(packageRoot, "start.ps1", "", null).ContinueWith(t =>
        {
            try { BeginInvoke((MethodInvoker)(() => { startFinished = true; RefreshStatus(true); pollTimer.Start(); })); } catch { }
        });
        RefreshStatus(true);
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        RefreshLayoutMode();
    }

    private void RefreshLayoutMode()
    {
        // During early OnResize the cards dictionary is still empty; moving on
        // would throw mid-loop and poison the cached `wide` flag, stranding the
        // cards in the hidden grid. Wait until construction finished.
        if (cards.Count != CardKeys.Length) return;
        bool wantWide = ClientSize.Width >= Ui.Px(1180);
        if (wantWide == wide && gridFour.Visible == wantWide) return;
        wide = wantWide;
        if (wantWide)
        {
            foreach (var key in CardKeys)
            {
                gridTwo.Controls.Remove(cards[key]);
                var idx = Array.IndexOf(CardKeys, key);
                gridFour.Controls.Add(cards[key], idx, 0);
            }
            gridTwo.Visible = false;
            gridFour.Visible = true;
        }
        else
        {
            foreach (var key in CardKeys)
            {
                gridFour.Controls.Remove(cards[key]);
                var idx = Array.IndexOf(CardKeys, key);
                gridTwo.Controls.Add(cards[key], idx % 2, idx / 2);
            }
            gridFour.Visible = false;
            gridTwo.Visible = true;
        }
    }

    private void RefreshStatus(bool probeUpdate)
    {
        long nowTicks = DateTime.UtcNow.Ticks;
        bool wantProbe = probeUpdate || (startFinished && !probeDone) || (probeDone && nowTicks - lastProbeTicks > TimeSpan.TicksPerMinute * 5);
        if (!startFinished) wantProbe = false;
        if (statusInFlight) return;
        statusInFlight = true;
        string args = wantProbe ? "-ProbeUpdate" : "";
        ScriptRunner.RunAsync(packageRoot, "status.ps1", args, delegate(string line)
        {
            if (line == null) return;
            if (!line.StartsWith("@@XIANYU_STATUS@@", StringComparison.Ordinal)) return;
            var json = line.Substring("@@XIANYU_STATUS@@".Length);
            try { BeginInvoke((MethodInvoker)(() => ApplyStatus(json, wantProbe))); } catch { }
        }).ContinueWith(t =>
        {
            try { BeginInvoke((MethodInvoker)(() => statusInFlight = false)); } catch { }
        });
    }

    private void ApplyStatus(string json, bool wasProbe)
    {
        var map = GuiProtocol.ParseObject(json);
        if (map == null) return;
        var summary = GuiProtocol.ParseObject(GetJson(map, "summary"));
        int online = summary == null ? 0 : GuiProtocol.GetInt(summary, "online", 0);
        int total = summary == null ? 4 : GuiProtocol.GetInt(summary, "total", 4);
        var level = summary == null ? "unknown" : GuiProtocol.GetString(summary, "level");
        switch (level)
        {
            case "ok": systemStatus.Set("系统运行正常 · " + online + " / " + total + " 个服务在线", Ui.Success, Ui.TextSecondary); break;
            case "partial": systemStatus.Set(online + " / " + total + " 个服务在线，部分服务未运行", Ui.Warning, Ui.Warning); break;
            case "down": systemStatus.Set("服务已全部停止", Ui.TextMuted, Ui.TextSecondary); break;
            case "error": systemStatus.Set("部分服务异常", Ui.Danger, Ui.Danger); break;
            case "unconfigured": systemStatus.Set("尚未完成安装配置", Ui.Warning, Ui.TextSecondary); break;
            default: systemStatus.Set("检测不到 Docker，无法获取服务状态", Ui.Danger, Ui.TextSecondary); break;
        }
        if (!startFinished && (level == "unknown" || level == "down")) systemStatus.Set("正在启动服务并检查更新…", Ui.Warning, Ui.TextSecondary);
        var services = GuiProtocol.ParseObject(GetJson(map, "services"));
        foreach (var key in CardKeys)
        {
            ServiceCard card;
            if (!cards.TryGetValue(key, out card)) continue;
            var svc = services == null ? null : GuiProtocol.ParseObject(GetJson(services, key));
            var state = svc == null ? "unknown" : GuiProtocol.GetString(svc, "state");
            var text = svc == null ? "未知状态" : GuiProtocol.GetString(svc, "text");
            card.Apply(state, text);
        }
        var url = GuiProtocol.GetString(map, "frontend_url");
        if (url.Length > 0)
        {
            var parts = url.Split('|');
            frontendUrl = parts[0];
            openBackend.Enabled = !(parts.Length > 1 && parts[1] == "unreachable") || true; // button stays clickable; URL probe shown in header
        }
        var activities = map.ContainsKey("activities") ? map["activities"] as System.Collections.IEnumerable : null;
        var records = new List<ActivityRecord>();
        if (activities != null)
        {
            foreach (var item in activities)
            {
                var dict = item as IDictionary<string, object>;
                if (dict == null) continue;
                records.Add(new ActivityRecord
                {
                    Kind = GuiProtocol.GetString(dict, "kind"),
                    Text = GuiProtocol.GetString(dict, "text"),
                    Time = AppOps.FormatActivityTime(GuiProtocol.GetString(dict, "time"))
                });
            }
        }
        activitySurface.SetRecords(records);
        var version = GuiProtocol.GetString(map, "version");
        var build = GuiProtocol.GetString(map, "build");
        versionLabel.Text = version.Length > 0 ? "v" + version + (build.Length > 0 ? "  ·  " + build : "") : "未检测到安装（app\\.env 不存在）";
        var probe = GuiProtocol.ParseObject(GetJson(map, "update_probe"));
        if (probe != null)
        {
            bool avail = GuiProtocol.GetBool(probe, "available");
            if (wasProbe && avail) { probeDone = true; lastProbeTicks = DateTime.UtcNow.Ticks; }
            if (wasProbe && !avail) { probeDone = true; lastProbeTicks = DateTime.UtcNow.Ticks; }
            updateLink.Visible = avail;
            if (avail)
            {
                var latest = GuiProtocol.GetString(probe, "latest_version");
                updateLink.Text = "● 有新版本 " + (latest.Length > 0 ? latest : "");
            }
        }
    }

    private static string GetJson(IDictionary<string, object> map, string key)
    {
        object value;
        if (map == null || !map.TryGetValue(key, out value) || value == null) return "{}";
        var serializer = new JavaScriptSerializer();
        return serializer.Serialize(value);
    }

    private void OpenAdmin()
    {
        var url = frontendUrl.Length > 0 ? frontendUrl : "http://127.0.0.1:20000";
        AppOps.OpenUrl(url);
    }

    private void LaunchUpdater()
    {
        if (!AppOps.LaunchPackageExecutable(packageRoot, "xianyu-updater", LauncherText.Updater, "--updater", "", ""))
            ScriptRunner.RunAsync(packageRoot, "update.ps1", "", null);
    }

    private void RunDiagnostics()
    {
        // Open the dedicated diagnostics window (staged progress, report
        // button, log folder access) instead of running a silent background
        // script the user never sees.
        if (!AppOps.LaunchPackageExecutable(packageRoot, "xianyu-diagnostics", LauncherText.Diagnostics, "--diagnostics", "", ""))
            systemStatus.Set("找不到诊断程序，请重新安装", Ui.Danger, Ui.TextSecondary);
    }

    private void ConfirmStop()
    {
        if (actionInFlight) return;
        if (!XianyuDialog.Confirm(this, "停止所有服务", "停止后管理系统将无法访问，下次需要手动启动。是否停止所有服务？", "停止所有服务", true)) return;
        actionInFlight = true;
        stopButton.Enabled = false;
        systemStatus.Set("正在停止服务…", Ui.Warning, Ui.TextSecondary);
        ScriptRunner.RunAsync(packageRoot, "stop.ps1", "", null).ContinueWith(t =>
        {
            try
            {
                BeginInvoke((MethodInvoker)(() =>
                {
                    actionInFlight = false;
                    stopButton.Enabled = true;
                    RefreshStatus(false);
                }));
            }
            catch { }
        });
    }

    private sealed class ServiceCard : Card
    {
        private readonly StatusBadge badge = new StatusBadge();
        private readonly Label name;

        internal ServiceCard(string title, string iconName)
        {
            SurfaceColor = Ui.Surface;
            Dock = DockStyle.Fill;
            Margin = Ui.Pad(6, 0, 6, 0);
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2, BackColor = Ui.Surface, Padding = Ui.Pad(16, 0, 16, 0) };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(48)));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 55F));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 45F));
            var iconBox = new IconBox(iconName, Ui.TextSecondary) { Dock = DockStyle.Fill };
            name = new Label { Text = title, Font = Ui.Font(16, true), ForeColor = Ui.TextPrimary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.BottomLeft };
            badge.Dock = DockStyle.Fill;
            layout.Controls.Add(iconBox, 0, 0);
            layout.SetRowSpan(iconBox, 2);
            layout.Controls.Add(name, 1, 0);
            layout.Controls.Add(badge, 1, 1);
            Controls.Add(layout);
        }

        internal void Apply(string state, string text)
        {
            Color dot;
            switch (state)
            {
                case "running": dot = Ui.Success; break;
                case "starting": dot = Ui.Warning; break;
                case "error": dot = Ui.Danger; break;
                case "stopped": dot = Ui.TextMuted; break;
                default: dot = Ui.TextMuted; break;
            }
            badge.Set(text.Length > 0 ? text : "未知状态", dot, state == "error" ? Ui.Danger : Ui.TextSecondary);
        }
    }

    private sealed class ActivityRecord
    {
        internal string Kind = "info";
        internal string Text = "";
        internal string Time = "";
    }

    private sealed class ActivitySurface : Card
    {
        private readonly List<ActivityRecord> records = new List<ActivityRecord>();
        private readonly Label title;
        private readonly ModernButton viewAll;

        internal event EventHandler ViewAllClicked;

        internal ActivitySurface()
        {
            SurfaceColor = Ui.Surface;
            Dock = DockStyle.Fill;
            var headerRow = new TableLayoutPanel { Dock = DockStyle.Top, Height = Ui.Px(44), ColumnCount = 2, BackColor = Ui.Surface, Padding = Ui.Pad(16, 8, 12, 0) };
            headerRow.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            headerRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            headerRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            title = new Label { Text = "最近活动", Font = Ui.Font(16, true), ForeColor = Ui.TextPrimary, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft };
            viewAll = new ModernButton { Text = "查看全部", TrailingIconName = "chevron-right", Variant = ModernButton.ButtonVariant.Ghost, AutoSize = false, Width = Ui.Px(116), Height = Ui.Px(28), TextDesignSize = 13 };
            viewAll.Click += (s, e) => { var h = ViewAllClicked; if (h != null) h(this, EventArgs.Empty); };
            headerRow.Controls.Add(title, 0, 0);
            headerRow.Controls.Add(viewAll, 1, 0);
            Controls.Add(headerRow);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            PaintRecords(e.Graphics);
        }

        internal void SetRecords(List<ActivityRecord> list)
        {
            records.Clear();
            if (list != null) records.AddRange(list);
            Invalidate();
        }

        private void PaintRecords(Graphics g)
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            float y = Ui.Px(48);
            float rowH = Ui.Px(40);
            float right = Width - Ui.Px(16);
            float timeW = Ui.Px(96);
            if (records.Count == 0)
            {
                TextRenderer.DrawText(g, "暂无活动记录", Ui.Font(14, false), new Rectangle(Ui.Px(16), (int)y, Width - Ui.Px(32), Ui.Px(24)), Ui.TextMuted, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
                return;
            }
            foreach (var record in records)
            {
                if (y + rowH > Height - Ui.Px(8)) continue;
                var color = record.Kind == "success" ? Ui.Success : (record.Kind == "warning" ? Ui.Warning : (record.Kind == "error" ? Ui.Danger : Ui.TextMuted));
                var iconName = record.Kind == "success" ? "circle-check" : (record.Kind == "warning" ? "triangle-alert" : (record.Kind == "error" ? "circle-x" : "info"));
                Lucide.Draw(g, iconName, new RectangleF(Ui.Px(16), y + rowH / 2F - Ui.Px(9), Ui.Px(18), Ui.Px(18)), color);
                TextRenderer.DrawText(g, record.Text, Ui.Font(14, false), new Rectangle((int)(Ui.Px(42)), (int)y, (int)(right - timeW - Ui.Px(52)), (int)rowH), Ui.TextSecondary, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
                TextRenderer.DrawText(g, record.Time, Ui.Font(13, false), new Rectangle((int)(right - timeW), (int)y, (int)timeW, (int)rowH), Ui.TextMuted, TextFormatFlags.Right | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
                y += rowH;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Stop / diagnostics standalone windows (entry points used by BAT + exe args).
// ---------------------------------------------------------------------------
internal sealed class TaskWindow : LauncherWindow
{
    private readonly LauncherRole role;
    private readonly string packageRoot;
    private readonly string appRoot;
    private StatusBadge statusBadge;
    private RichTextBox logBox;
    private CollapsibleSection logSection;
    private ModernButton closeButton;
    private string reportPath = "";

    internal TaskWindow(LauncherRole taskRole) : base(taskRole == LauncherRole.Stopper ? LauncherText.Stopper : LauncherText.Diagnostics, false)
    {
        role = taskRole;
        packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        appRoot = Path.Combine(packageRoot, "app");
        ClientSize = new Size(Ui.Px(640), Ui.Px(420));
        MinimumSize = new Size(Ui.Px(520), Ui.Px(360));

        var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, BackColor = Ui.Background, Padding = Ui.Pad(28, 20, 28, 20) };
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(64)));
        var head = new TableLayoutPanel { Dock = DockStyle.Top, Height = Ui.Px(64), ColumnCount = 2, RowCount = 1 };
        head.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        head.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(56)));
        head.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        head.Controls.Add(new IconBox(role == LauncherRole.Stopper ? "circle-stop" : "file-search", role == LauncherRole.Stopper ? Ui.Warning : Ui.PrimaryHover) { Dock = DockStyle.Fill }, 0, 0);
        statusBadge = new StatusBadge { Dock = DockStyle.Fill, Margin = new Padding(0) };
        statusBadge.Set(role == LauncherRole.Stopper ? "正在停止服务…" : "正在收集诊断信息…", Ui.Warning, Ui.TextPrimary);
        statusBadge.Font = Ui.Font(16, true);
        head.Controls.Add(statusBadge, 1, 0);

        var card = new Card { Dock = DockStyle.Fill, SurfaceColor = Ui.Surface };
        if (role == LauncherRole.Stopper)
        {
            logSection = new CollapsibleSection("查看详细日志");
            logSection.Dock = DockStyle.Fill;
            var scrollLog = new Panel { Dock = DockStyle.Fill, BackColor = Ui.Surface };
            logBox = new RichTextBox
            {
                Dock = DockStyle.Fill,
                ReadOnly = true,
                BorderStyle = BorderStyle.None,
                BackColor = Ui.Surface,
                ForeColor = Ui.TextSecondary,
                Font = Ui.Mono(12),
                WordWrap = false
            };
            logSection.Body.Controls.Add(scrollLog);
            scrollLog.Controls.Add(logBox);
            card.Controls.Add(logSection);
        }
        else
        {
            var message = new Label
            {
                Text = "正在收集系统诊断信息，完成后将自动生成并打开诊断报告。",
                Font = Ui.Font(14, false),
                ForeColor = Ui.TextSecondary,
                Dock = DockStyle.Fill,
                TextAlign = ContentAlignment.MiddleCenter,
                Padding = Ui.Pad(24)
            };
            card.Controls.Add(message);
        }

        closeButton = new ModernButton { Text = "关闭", Variant = ModernButton.ButtonVariant.Secondary, Width = Ui.Px(120), Height = Ui.Px(40), Margin = new Padding(0) };
        closeButton.Click += (s, e) => Close();
        var footerRow = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, WrapContents = false, Padding = new Padding(0, Ui.Px(12), 0, 0) };
        footerRow.Controls.Add(closeButton);

        body.Controls.Add(head, 0, 0);
        body.Controls.Add(card, 0, 1);
        body.Controls.Add(footerRow, 0, 2);
        SetContent(body);

        Shown += (s, e) =>
        {
            string script = role == LauncherRole.Stopper ? "stop.ps1" : "diagnostics.ps1";
            string args = role == LauncherRole.Stopper ? "" : "-NoOpen";
            ScriptRunner.RunAsync(packageRoot, script, args, OnLine).ContinueWith(t =>
            {
                try { BeginInvoke((MethodInvoker)(() => Finished(t.Result == null ? 1 : t.Result.ExitCode))); } catch { }
            });
        };
    }

    private void OnLine(string line)
    {
        if (line == null) return;
        GuiEvent evt;
        if (GuiProtocol.TryParse(line, out evt)) return;
        if (role == LauncherRole.Diagnostics && line.StartsWith("Diagnostic report saved:", StringComparison.OrdinalIgnoreCase))
            reportPath = line.Substring("Diagnostic report saved:".Length).Trim();
        if (logBox == null || logBox.IsDisposed) return;
        if (logBox.TextLength > 200000) logBox.Clear();
        logBox.AppendText(line + Environment.NewLine);
    }

    private void Finished(int exitCode)
    {
        if (exitCode == 0)
        {
            statusBadge.Set(role == LauncherRole.Stopper ? "服务已全部停止" : "诊断完成，报告已生成", Ui.Success, Ui.TextPrimary);
            if (role == LauncherRole.Diagnostics)
            {
                closeButton.Text = "完成";
                if (reportPath.Length > 0 && File.Exists(reportPath))
                {
                    try { Process.Start(new ProcessStartInfo { FileName = "notepad.exe", Arguments = "\"" + reportPath + "\"", UseShellExecute = false }); } catch { }
                }
            }
        }
        else
        {
            statusBadge.Set(role == LauncherRole.Stopper ? "停止失败，请查看日志" : "诊断信息收集失败", Ui.Danger, Ui.Danger);
            if (logSection != null) logSection.SetExpanded(true);
        }
    }
}

// ---------------------------------------------------------------------------
// Entry point: role detection (arguments first, then executable name) and
// pending client-maintenance handoff before the dashboard appears.
// ---------------------------------------------------------------------------
internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Ui.LoadBrandLogo();
        AppOps.TraceEnabled = Environment.GetEnvironmentVariable("XIANYU_LAUNCHER_TRACE") == "1";
        Application.ThreadException += delegate(object s, System.Threading.ThreadExceptionEventArgs e) { CrashLog(e.Exception); };
        AppDomain.CurrentDomain.UnhandledException += delegate(object s, UnhandledExceptionEventArgs e) { CrashLog(e.ExceptionObject as Exception); };
        try
        {
            using (var probe = Graphics.FromHwnd(IntPtr.Zero)) Ui.DpiScale = probe.DpiX / 96F;
        }
        catch { Ui.DpiScale = 1F; }
        if (Ui.DpiScale < 1F) Ui.DpiScale = 1F;
        Ui.ActualDpiScale = Ui.DpiScale;
        // Test hook: emulate 125/150/175/200% scaling without touching the
        // system DPI (accepts "1.5" or "150"). Never set in production.
        var forcedDpi = Environment.GetEnvironmentVariable("XIANYU_FORCE_DPI");
        float forcedScale;
        if (forcedDpi != null && float.TryParse(forcedDpi.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out forcedScale))
        {
            if (forcedScale > 50F) forcedScale /= 100F;
            if (forcedScale >= 1F) Ui.DpiScale = forcedScale;
        }

        var argument = args != null && args.Length > 0 ? args[0].Trim().ToLowerInvariant() : "";
        LauncherRole role;
        if (argument == "--installer" || argument == "setup" || argument == "install") role = LauncherRole.Installer;
        else if (argument == "--updater" || argument == "update" || argument == "updater") role = LauncherRole.Updater;
        else if (argument == "--stop" || argument == "stop" || argument == "stopper") role = LauncherRole.Stopper;
        else if (argument == "--diagnostics" || argument == "diagnostics") role = LauncherRole.Diagnostics;
        else role = DetectRoleFromExecutableName();

        var packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        var resume = argument == "--resume" || ContainsArgument(args, "--resume");
        var installPath = ReadArgument(args, "--install-path");

        // A downloaded client maintenance package is applied before any window
        // appears: the hidden applier waits for this process to exit, swaps
        // the launcher files, and relaunches the same executable.
        if (ApplyClientUpdateOnStartup(packageRoot)) return;

        switch (role)
        {
            case LauncherRole.Installer:
                Application.Run(new InstallerWindow(resume, installPath));
                return;
            case LauncherRole.Updater:
                Application.Run(new UpdaterWindow());
                return;
            case LauncherRole.Stopper:
                Application.Run(new TaskWindow(LauncherRole.Stopper));
                return;
            case LauncherRole.Diagnostics:
                Application.Run(new TaskWindow(LauncherRole.Diagnostics));
                return;
            default:
                Application.Run(new DashboardWindow());
                return;
        }
    }

    private static void CrashLog(Exception error)
    {
        try
        {
            if (error == null) return;
            var dir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "launcher-crash.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + error + Environment.NewLine,
                System.Text.Encoding.UTF8);
        }
        catch { }
    }

    private static LauncherRole DetectRoleFromExecutableName()
    {
        try
        {
            var name = Path.GetFileNameWithoutExtension(Application.ExecutablePath);
            if (name.Contains(LauncherText.Installer) || name.IndexOf("installer", StringComparison.OrdinalIgnoreCase) >= 0) return LauncherRole.Installer;
            if (name.Contains(LauncherText.Updater) || name.IndexOf("updater", StringComparison.OrdinalIgnoreCase) >= 0) return LauncherRole.Updater;
            if (name.Contains(LauncherText.Stopper) || name.IndexOf("stopper", StringComparison.OrdinalIgnoreCase) >= 0) return LauncherRole.Stopper;
            if (name.Contains(LauncherText.Diagnostics) || name.IndexOf("diagnostics", StringComparison.OrdinalIgnoreCase) >= 0) return LauncherRole.Diagnostics;
        }
        catch { }
        return LauncherRole.Dashboard;
    }

    private static bool ApplyClientUpdateOnStartup(string packageRoot)
    {
        try
        {
            var appRoot = Path.Combine(packageRoot, "app");
            var pendingDir = Path.Combine(appRoot, "updates", "pending");
            if (!Directory.Exists(pendingDir)) return false;
            var hasPending = false;
            foreach (var file in Directory.GetFiles(pendingDir, "*.zip")) { hasPending = true; break; }
            if (!hasPending) return false;
            var applier = Path.Combine(packageRoot, "scripts", "apply-client-update.ps1");
            if (!File.Exists(applier)) return false;
            var launcherPath = Path.Combine(packageRoot, "xianyu-launcher.exe");
            if (!File.Exists(launcherPath)) launcherPath = Application.ExecutablePath;
            var psi = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
                // Match the applier's canonical parameter name. The script
                // also accepts RelaunchPath so already-installed launchers
                // can finish a pending package during the migration.
                Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File " + ScriptRunner.Quote(applier) + " -PackageRoot " + ScriptRunner.Quote(packageRoot) + " -RestartExecutable " + ScriptRunner.Quote(launcherPath),
                UseShellExecute = false,
                CreateNoWindow = true
            };
            psi.EnvironmentVariables["XIANYU_NONINTERACTIVE"] = "1";
            Process.Start(psi);
            return true;
        }
        catch { return false; }
    }

    private static string ReadArgument(string[] args, string name)
    {
        if (args == null) return "";
        for (var i = 0; i < args.Length - 1; i++)
            if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase)) return args[i + 1] ?? "";
        return "";
    }

    private static bool ContainsArgument(string[] args, string name)
    {
        if (args == null) return false;
        for (var i = 0; i < args.Length; i++)
            if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }
}
