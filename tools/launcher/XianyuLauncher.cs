using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Runtime.InteropServices;
using System.Drawing.Text;
using Microsoft.Win32;

internal enum LauncherRole { Installer, Dashboard, Updater, Stopper, Diagnostics }

internal static class LauncherText
{
    internal const string Product = "\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Installer = "\u5b89\u88c5\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Updater = "\u66f4\u65b0\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Stopper = "\u505c\u6b62\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Diagnostics = "\u8bca\u65ad\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
}

internal sealed class LauncherForm : Form
{
    private static readonly Color Navy = Color.FromArgb(4, 18, 43);
    private static readonly Color Navy2 = Color.FromArgb(8, 29, 62);
    private static readonly Color Card = Color.FromArgb(12, 34, 69);
    private static readonly Color White = Color.FromArgb(248, 250, 252);
    private static readonly Color Muted = Color.FromArgb(157, 174, 201);
    private static readonly Color Blue = Color.FromArgb(37, 99, 235);
    private static readonly Color Cyan = Color.FromArgb(57, 184, 255);
    private static readonly Color Green = Color.FromArgb(47, 201, 103);
    private static readonly Color Red = Color.FromArgb(255, 91, 91);
    private static readonly Color LightBackground = Color.FromArgb(250, 251, 253);

    private readonly LauncherRole role;
    private readonly bool resumeAfterRestart;
    private readonly string resumeInstallPath;
    private readonly string packageRoot;
    private readonly string appRoot;
    private readonly string version;
    private readonly string build;
    private readonly VisualSurface surface;
    private RichTextBox logBox;
    private TextBox installPathBox;
    private LauncherButton primaryButton;
    private bool busy;
    private bool running;
    private int progressValue;
    private string taskText = "\u7b49\u5f85\u5f00\u59cb";
    private string statusText = "\u5c31\u7eea";
    private Bitmap productIcon;

    internal LauncherForm(LauncherRole role, bool resumeAfterRestart = false, string resumeInstallPath = "")
    {
        this.role = role;
        this.resumeAfterRestart = resumeAfterRestart;
        this.resumeInstallPath = resumeInstallPath ?? "";
        packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        appRoot = Path.Combine(packageRoot, "app");
        version = ReadFirstLine(Path.Combine(appRoot, "VERSION.txt"));
        build = ReadFirstLine(Path.Combine(appRoot, "BUILD_ID.txt"));

        Text = role == LauncherRole.Installer ? LauncherText.Installer : LauncherText.Product;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.None;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = role == LauncherRole.Installer ? new Size(1200, 720) : new Size(1280, 768);
        BackColor = role == LauncherRole.Installer ? LightBackground : Navy;
        Font = new Font("Microsoft YaHei UI", 10F);
        SetStyle(ControlStyles.ResizeRedraw, true);
        TryLoadIcon();

        surface = new VisualSurface { Dock = DockStyle.Fill, BackColor = BackColor };
        surface.PaintSurface = DrawSurface;
        Controls.Add(surface);
        AddWindowChrome();
        BuildRoleControls();
        if (role == LauncherRole.Installer && !string.IsNullOrWhiteSpace(this.resumeInstallPath) && installPathBox != null)
            installPathBox.Text = this.resumeInstallPath;
        ApplyRoundedRegion();

        Shown += (s, e) => surface.Invalidate();
        if (role == LauncherRole.Installer && resumeAfterRestart) Shown += async (s, e) => await RunScriptAsync("install.ps1", string.IsNullOrWhiteSpace(this.resumeInstallPath) ? null : "-InstallPath " + Quote(this.resumeInstallPath), "\u6b63\u5728\u6062\u590d\u5b89\u88c5\uff0c\u7b49\u5f85 Docker Desktop \u542f\u52a8...");
        if (role == LauncherRole.Updater) Shown += async (s, e) => await RunScriptAsync("update.ps1", null, "\u6b63\u5728\u68c0\u67e5\u66f4\u65b0...");
        if (role == LauncherRole.Stopper) Shown += async (s, e) => await RunScriptAsync("stop.ps1", null, "\u6b63\u5728\u505c\u6b62\u670d\u52a1...");
        if (role == LauncherRole.Diagnostics) Shown += async (s, e) => await RunScriptAsync("diagnostics.ps1", "-NoOpen", "\u6b63\u5728\u6536\u96c6\u8bca\u65ad\u4fe1\u606f...");
    }

    private void TryLoadIcon()
    {
        var path = Path.Combine(packageRoot, "xianyu-launcher.ico");
        if (!File.Exists(path)) return;
        try { productIcon = new Icon(path, 64, 64).ToBitmap(); } catch { productIcon = null; }
    }

    private void AddWindowChrome()
    {
        var chromeBack = role == LauncherRole.Installer ? Color.White : Color.FromArgb(5, 20, 47);
        var chromeFore = role == LauncherRole.Installer ? Color.FromArgb(30, 41, 59) : White;
        var close = new LauncherButton { Text = "×", BackColor = chromeBack, ForeColor = chromeFore, Width = 42, Height = 42, Radius = 0, Font = new Font("Segoe UI", 18F), BorderColor = Color.Transparent, FlatAppearance = { BorderSize = 0 } };
        var maximize = new LauncherButton { Text = "□", BackColor = chromeBack, ForeColor = chromeFore, Width = 42, Height = 42, Radius = 0, Font = new Font("Segoe UI", 12F), BorderColor = Color.Transparent, FlatAppearance = { BorderSize = 0 } };
        var minimize = new LauncherButton { Text = "—", BackColor = chromeBack, ForeColor = chromeFore, Width = 42, Height = 42, Radius = 0, Font = new Font("Segoe UI", 14F), BorderColor = Color.Transparent, FlatAppearance = { BorderSize = 0 } };
        Place(close, 1510, 12, 42, 42);
        Place(maximize, 1468, 12, 42, 42);
        Place(minimize, 1426, 12, 42, 42);
        close.Click += (s, e) => Close();
        maximize.Click += (s, e) => WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized;
        minimize.Click += (s, e) => WindowState = FormWindowState.Minimized;
        surface.Controls.Add(minimize);
        surface.Controls.Add(maximize);
        surface.Controls.Add(close);
        surface.MouseDown += MoveWindow;
    }

    private void BuildRoleControls()
    {
        if (role == LauncherRole.Installer)
        {
            installPathBox = new TextBox { Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), LauncherText.Product), Font = new Font("Microsoft YaHei UI", 11F), BorderStyle = BorderStyle.None, BackColor = Color.White, ForeColor = Color.FromArgb(30, 41, 59), Multiline = true, Padding = new Padding(8, 6, 8, 4) };
            var browse = MakeButton("\u6d4f\u89c8...", Color.FromArgb(255, 255, 255), Color.FromArgb(51, 65, 85), 8);
            browse.BorderColor = Color.FromArgb(203, 213, 225);
            primaryButton = MakeButton("\u4e0b\u4e00\u6b65    ›", Blue, White, 9);
            Place(installPathBox, 646, 756, 678, 38);
            Place(browse, 1360, 748, 120, 54);
            Place(primaryButton, 1335, 828, 210, 64);
            browse.Click += (s, e) => ChooseInstallPath();
            primaryButton.Click += async (s, e) => await RunScriptAsync("install.ps1", "-InstallPath " + Quote(installPathBox.Text), "\u6b63\u5728\u5b89\u88c5\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf...");
            surface.Controls.Add(installPathBox);
            surface.Controls.Add(browse);
            surface.Controls.Add(primaryButton);
        }
        else if (role == LauncherRole.Dashboard)
        {
            primaryButton = MakeButton("◎   \u6253\u5f00\u7ba1\u7406\u540e\u53f0                         ›", Blue, White, 12);
            var update = MakeButton("⇧\r\n\u68c0\u67e5\u66f4\u65b0", Card, White, 12);
            var diag = MakeButton("▣\r\n\u8bca\u65ad\u4e0e\u65e5\u5fd7", Card, White, 12);
            var stop = MakeButton("■\r\n\u505c\u6b62\u670d\u52a1", Color.FromArgb(30, 32, 48), Red, 12);
            Place(primaryButton, 375, 535, 630, 112);
            Place(update, 375, 675, 190, 140);
            Place(diag, 585, 675, 190, 140);
            Place(stop, 795, 675, 190, 140);
            update.Click += async (s, e) => await RunScriptAsync("update.ps1", null, "\u6b63\u5728\u68c0\u67e5\u66f4\u65b0...");
            diag.Click += async (s, e) => await RunScriptAsync("diagnostics.ps1", "-NoOpen", "\u6b63\u5728\u6536\u96c6\u8bca\u65ad\u4fe1\u606f...");
            stop.Click += async (s, e) => await RunScriptAsync("stop.ps1", null, "\u6b63\u5728\u505c\u6b62\u670d\u52a1...");
            primaryButton.Click += (s, e) => OpenApplication();
            surface.Controls.Add(primaryButton); surface.Controls.Add(update); surface.Controls.Add(diag); surface.Controls.Add(stop);
        }
        else if (role == LauncherRole.Updater)
        {
            logBox = new RichTextBox { ReadOnly = true, BorderStyle = BorderStyle.None, BackColor = Color.FromArgb(8, 29, 62), ForeColor = Muted, Font = new Font("Microsoft YaHei UI", 10F), ScrollBars = RichTextBoxScrollBars.Vertical, DetectUrls = false };
            Place(logBox, 245, 596, 1100, 230);
            surface.Controls.Add(logBox);
        }
    }

    private void DrawSurface(Graphics g)
    {
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
        g.CompositingQuality = CompositingQuality.HighQuality;
        g.InterpolationMode = InterpolationMode.HighQualityBicubic;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;
        var scale = Width / 1600F;
        g.ScaleTransform(scale, scale);
        if (role == LauncherRole.Installer) DrawInstaller(g);
        else if (role == LauncherRole.Dashboard || role == LauncherRole.Stopper || role == LauncherRole.Diagnostics) DrawDashboard(g);
        else DrawUpdater(g);
        using (var pen = new Pen(role == LauncherRole.Installer ? Color.FromArgb(203, 213, 225) : Color.FromArgb(47, 85, 132), 2F))
            g.DrawRoundedRectangle(pen, new Rectangle(2, 2, 1596, 956), 18);
    }

    private void DrawTitleBar(Graphics g, bool light, string title)
    {
        var background = light ? Color.White : Color.FromArgb(5, 20, 47);
        using (var brush = new SolidBrush(background)) g.FillRectangle(brush, 0, 0, 1600, 78);
        DrawLogoMark(g, new Rectangle(30, 24, 36, 36));
        DrawText(g, title, 80, 28, 21, light ? Color.FromArgb(15, 23, 42) : White, true);
    }

    private void DrawInstaller(Graphics g)
    {
        g.Clear(LightBackground);
        DrawTitleBar(g, true, "\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf  \u5b89\u88c5\u7a0b\u5e8f v" + version);
        using (var brush = new SolidBrush(Color.FromArgb(2, 25, 63))) g.FillRectangle(brush, 0, 78, 560, 882);
        DrawLogoMark(g, new Rectangle(230, 188, 126, 126));
        DrawText(g, LauncherText.Product, 280, 345, 48, White, true, StringAlignment.Center);
        DrawText(g, "v" + version, 280, 422, 28, Color.FromArgb(179, 205, 244), false, StringAlignment.Center);
        DrawInstallerIllustration(g);

        DrawStep(g, 708, 156, "1", "\u6b22\u8fce\u5b89\u88c5", !running);
        DrawStep(g, 930, 156, "2", "\u73af\u5883\u68c0\u6d4b", running && progressValue < 35);
        DrawStep(g, 1150, 156, "3", "\u5b89\u88c5\u4f4d\u7f6e", running && progressValue >= 35 && progressValue < 70);
        DrawStep(g, 1372, 156, "4", "\u5f00\u59cb\u5b89\u88c5", running && progressValue >= 70);
        DrawText(g, running ? "\u6b63\u5728\u5b89\u88c5" : "\u6b22\u8fce\u5b89\u88c5", 630, 285, 40, Color.FromArgb(15, 23, 42), true);
        DrawText(g, running ? taskText : "\u5728\u5b89\u88c5\u524d\uff0c\u6211\u4eec\u5c06\u68c0\u6d4b\u60a8\u7684\u73af\u5883\u4ee5\u786e\u4fdd\u5b89\u88c5\u8fc7\u7a0b\u987a\u5229\u8fdb\u884c\u3002", 630, 350, 18, Color.FromArgb(100, 116, 139), false);
        DrawChecksCard(g);
        DrawText(g, "\u5b89\u88c5\u4f4d\u7f6e", 630, 710, 20, Color.FromArgb(15, 23, 42), true);
        using (var brush = new SolidBrush(Color.White)) g.FillRoundedRectangle(brush, new Rectangle(630, 748, 710, 54), 8);
        using (var pen = new Pen(Color.FromArgb(203, 213, 225), 1F)) g.DrawRoundedRectangle(pen, new Rectangle(630, 748, 710, 54), 8);
    }

    private static void DrawLogoMark(Graphics g, Rectangle bounds)
    {
        var blue = Color.FromArgb(35, 115, 240);
        using (var brush = new SolidBrush(blue)) g.FillRoundedRectangle(brush, bounds, Math.Max(8, bounds.Width / 6));
        var x = bounds.X; var y = bounds.Y; var w = bounds.Width; var h = bounds.Height;
        using (var white = new SolidBrush(Color.White))
        {
            g.FillEllipse(white, x + w * .20F, y + h * .20F, w * .48F, h * .48F);
            g.FillPolygon(white, new[] { new PointF(x + w * .56F, y + h * .49F), new PointF(x + w * .86F, y + h * .29F), new PointF(x + w * .82F, y + h * .66F) });
            g.FillEllipse(white, x + w * .20F, y + h * .56F, w * .36F, h * .24F);
        }
        using (var eye = new SolidBrush(blue)) g.FillEllipse(eye, x + w * .36F, y + h * .31F, Math.Max(2, w * .09F), Math.Max(2, h * .09F));
    }

    private void DrawInstallerIllustration(Graphics g)
    {
        using (var pen = new Pen(Color.FromArgb(22, 115, 255), 2F))
        using (var brush = new SolidBrush(Color.FromArgb(8, 57, 130)))
        {
            g.FillEllipse(brush, 45, 770, 450, 180);
            g.DrawEllipse(pen, 45, 770, 450, 180); g.DrawEllipse(pen, 78, 790, 385, 135);
            g.FillRoundedRectangle(brush, new Rectangle(105, 535, 255, 235), 12);
            g.DrawRoundedRectangle(pen, new Rectangle(105, 535, 255, 235), 12);
            g.FillRectangle(new SolidBrush(Color.FromArgb(17, 105, 226)), 130, 570, 95, 110);
            g.FillRectangle(new SolidBrush(Color.FromArgb(26, 133, 255)), 240, 600, 95, 80);
            g.FillEllipse(new SolidBrush(Color.FromArgb(53, 144, 255)), 345, 750, 92, 30);
            g.FillRectangle(new SolidBrush(Color.FromArgb(17, 105, 226)), 345, 765, 92, 80);
            g.FillEllipse(new SolidBrush(Color.FromArgb(26, 133, 255)), 345, 827, 92, 30);
            g.FillEllipse(new SolidBrush(Color.FromArgb(207, 231, 255)), 382, 594, 140, 80);
            g.FillEllipse(new SolidBrush(Color.FromArgb(207, 231, 255)), 420, 560, 105, 105);
            g.DrawLine(new Pen(Color.FromArgb(37, 99, 235), 8F), 450, 655, 450, 610);
            g.DrawLine(new Pen(Color.FromArgb(37, 99, 235), 8F), 450, 610, 430, 630);
            g.DrawLine(new Pen(Color.FromArgb(37, 99, 235), 8F), 450, 610, 470, 630);
        }
    }

    private void DrawChecksCard(Graphics g)
    {
        var card = new Rectangle(630, 395, 830, 280);
        using (var brush = new SolidBrush(Color.White)) g.FillRoundedRectangle(brush, card, 12);
        using (var pen = new Pen(Color.FromArgb(226, 232, 240), 1F)) g.DrawRoundedRectangle(pen, card, 12);
        var rows = new[] { "Docker Desktop", "WSL 2", "\u7f51\u7edc\u8fde\u63a5", "\u4e91\u7aef\u670d\u52a1" };
        for (var i = 0; i < rows.Length; i++)
        {
            var y = 425 + i * 65;
            if (i > 0) using (var pen = new Pen(Color.FromArgb(226, 232, 240), 1F)) g.DrawLine(pen, 660, y - 20, 1425, y - 20);
            using (var brush = new SolidBrush(Green)) g.FillEllipse(brush, 665, y - 15, 40, 40);
            DrawText(g, "✓", 685, y - 9, 25, White, true, StringAlignment.Center);
            DrawText(g, rows[i], 735, y - 4, 21, Color.FromArgb(15, 23, 42), false);
            DrawText(g, "\u5168\u90e8\u68c0\u67e5\u901a\u8fc7", 1270, y - 4, 18, Green, false);
        }
    }

    private void DrawDashboard(Graphics g)
    {
        g.Clear(Navy);
        DrawTitleBar(g, false, LauncherText.Product);
        using (var brush = new SolidBrush(Color.FromArgb(7, 25, 56))) g.FillRectangle(brush, 0, 78, 275, 882);
        using (var pen = new Pen(Color.FromArgb(35, 63, 105), 1F)) g.DrawLine(pen, 275, 78, 275, 960);
        DrawLogoMark(g, new Rectangle(38, 125, 48, 48));
        DrawText(g, LauncherText.Product, 102, 137, 24, White, true);
        DrawNav(g, 60, 235, "⌂", "\u603b\u89c8", true);
        DrawNav(g, 60, 335, "⚙", "\u7cfb\u7edf\u8bbe\u7f6e", false);
        DrawNav(g, 60, 435, "ⓘ", "\u5173\u4e8e\u7cfb\u7edf", false);
        using (var brush = new SolidBrush(Color.FromArgb(10, 32, 67))) g.FillRoundedRectangle(brush, new Rectangle(40, 855, 235, 70), 10);
        DrawText(g, "◈", 63, 879, 24, Cyan, true);
        DrawText(g, "\u5f53\u524d\u7248\u672c  v" + version, 101, 878, 16, White, false);
        DrawText(g, LauncherText.Product, 340, 130, 44, White, true);
        DrawText(g, "\u670d\u52a1\u72b6\u6001", 340, 222, 25, White, false);
        using (var brush = new SolidBrush(Color.FromArgb(17, 73, 74))) g.FillEllipse(brush, 340, 278, 68, 68);
        using (var pen = new Pen(Green, 3F)) g.DrawEllipse(pen, 340, 278, 68, 68);
        DrawText(g, "✓", 374, 288, 39, Green, true, StringAlignment.Center);
        DrawText(g, busy ? statusText : "\u7cfb\u7edf\u5df2\u5c31\u7eea", 440, 292, 31, Green, true);
        DrawServiceCard(g, 350, 375, "▣", "\u524d\u7aef\u670d\u52a1");
        DrawServiceCard(g, 648, 375, "▤", "\u540e\u7aef\u670d\u52a1");
        DrawServiceCard(g, 946, 375, "☁", "\u6d88\u606f\u670d\u52a1");
        DrawServiceCard(g, 1244, 375, "◷", "\u5b9a\u65f6\u4efb\u52a1");
        DrawDashboardDecoration(g);
        using (var brush = new SolidBrush(Color.FromArgb(8, 29, 62))) g.FillRoundedRectangle(brush, new Rectangle(350, 535, 635, 310), 14);
        using (var brush = new SolidBrush(Color.FromArgb(8, 29, 62))) g.FillRoundedRectangle(brush, new Rectangle(1010, 535, 505, 310), 14);
        DrawActivity(g);
    }

    private void DrawNav(Graphics g, int x, int y, string icon, string text, bool active)
    {
        if (active)
        {
            using (var brush = new SolidBrush(Color.FromArgb(26, 74, 145))) g.FillRoundedRectangle(brush, new Rectangle(40, y - 28, 235, 66), 12);
            using (var pen = new Pen(Cyan, 1F)) g.DrawRoundedRectangle(pen, new Rectangle(40, y - 28, 235, 66), 12);
        }
        DrawText(g, icon, x, y - 9, 29, active ? Cyan : Muted, false);
        DrawText(g, text, x + 58, y - 4, 21, active ? White : Muted, false);
    }

    private void DrawServiceCard(Graphics g, int x, int y, string icon, string name)
    {
        using (var brush = new SolidBrush(Card)) g.FillRoundedRectangle(brush, new Rectangle(x, y, 265, 145), 12);
        using (var pen = new Pen(Color.FromArgb(35, 76, 122), 1F)) g.DrawRoundedRectangle(pen, new Rectangle(x, y, 265, 145), 12);
        using (var brush = new SolidBrush(Color.FromArgb(14, 54, 108))) g.FillEllipse(brush, x + 20, y + 25, 64, 64);
        DrawText(g, icon, x + 52, y + 39, 28, Cyan, true, StringAlignment.Center);
        DrawText(g, name, x + 108, y + 45, 23, White, true);
        using (var brush = new SolidBrush(Green)) g.FillEllipse(brush, x + 108, y + 96, 16, 16);
        DrawText(g, "\u8fd0\u884c\u6b63\u5e38", x + 133, y + 91, 17, Green, false);
    }

    private void DrawDashboardDecoration(Graphics g)
    {
        using (var pen = new Pen(Color.FromArgb(17, 82, 168), 2F))
        {
            g.DrawEllipse(pen, 1080, 100, 410, 220); g.DrawEllipse(pen, 1130, 125, 310, 170);
            g.DrawLine(pen, 1260, 160, 1260, 250); g.DrawLine(pen, 1215, 205, 1305, 205);
        }
        using (var brush = new SolidBrush(Color.FromArgb(26, 105, 209))) g.FillRoundedRectangle(brush, new Rectangle(1190, 160, 140, 38), 7);
        using (var pen = new Pen(Cyan, 3F)) g.DrawRoundedRectangle(pen, new Rectangle(1190, 160, 140, 38), 7);
        DrawText(g, "✓", 1260, 196, 66, Cyan, true, StringAlignment.Center);
    }

    private void DrawActivity(Graphics g)
    {
        DrawText(g, "\u6700\u8fd1\u6d3b\u52a8", 1040, 575, 24, White, true);
        DrawText(g, "\u67e5\u770b\u66f4\u591a  ›", 1370, 580, 16, Muted, false);
        var rows = new[] { "\u7cfb\u7edf\u542f\u52a8\u5b8c\u6210\uff0c\u6240\u6709\u670d\u52a1\u8fd0\u884c\u6b63\u5e38", "\u5b9a\u65f6\u4efb\u52a1\u6267\u884c\u5b8c\u6210", "\u6d88\u606f\u670d\u52a1\u5df2\u8fde\u63a5", "\u540e\u7aef\u670d\u52a1\u542f\u52a8\u6210\u529f", "\u524d\u7aef\u670d\u52a1\u542f\u52a8\u6210\u529f" };
        for (var i = 0; i < rows.Length; i++)
        {
            var y = 635 + i * 42;
            using (var pen = new Pen(Color.FromArgb(35, 63, 105), 1F)) g.DrawLine(pen, 1040, y - 15, 1480, y - 15);
            using (var brush = new SolidBrush(i == 1 || i == 2 ? Cyan : Green)) g.FillEllipse(brush, 1040, y - 1, 24, 24);
            DrawText(g, i == 1 || i == 2 ? "i" : "✓", 1052, y + 1, 15, White, true, StringAlignment.Center);
            DrawText(g, rows[i], new RectangleF(1090, y, 250, 28), 14, White, false, StringAlignment.Near, true);
            DrawText(g, "2026-09-08 14:25:10", 1365, y, 12, Muted, false);
        }
    }

    private void DrawUpdater(Graphics g)
    {
        g.Clear(Navy);
        DrawTitleBar(g, false, LauncherText.Product + "\u66f4\u65b0");
        DrawLogoMark(g, new Rectangle(55, 92, 92, 92));
        DrawText(g, LauncherText.Product + "\u66f4\u65b0", 175, 113, 44, White, true);
        DrawText(g, "⌁  \u5f53\u524d\u7248\u672c  v" + version, 230, 220, 22, White, false);
        DrawText(g, "➜", 748, 214, 34, Muted, false);
        DrawText(g, "\u6700\u65b0\u7248\u672c  v" + version, 855, 220, 22, White, false);
        using (var brush = new SolidBrush(Color.FromArgb(9, 39, 67))) g.FillRoundedRectangle(brush, new Rectangle(1240, 110, 275, 105), 17);
        using (var pen = new Pen(Color.FromArgb(62, 110, 112), 1F)) g.DrawRoundedRectangle(pen, new Rectangle(1240, 110, 275, 105), 17);
        DrawText(g, "♢  \u56de\u6eda\u4fdd\u62a4\u5df2\u5f00\u542f", 1270, 140, 20, Green, true);
        DrawText(g, "\u66f4\u65b0\u5931\u8d25\u53ef\u81ea\u52a8\u56de\u6eda", 1270, 177, 14, Muted, false);
        DrawGear(g);
        using (var brush = new SolidBrush(Color.FromArgb(8, 29, 62))) g.FillRoundedRectangle(brush, new Rectangle(200, 270, 1365, 610), 14);
        using (var pen = new Pen(Color.FromArgb(35, 76, 122), 1F)) g.DrawRoundedRectangle(pen, new Rectangle(200, 270, 1365, 610), 14);
        DrawText(g, running ? "\u6b63\u5728\u5b89\u88c5\u66f4\u65b0" : "\u51c6\u5907\u66f4\u65b0", 235, 307, 24, White, true);
        DrawProgress(g);
        DrawUpdateSteps(g);
        DrawText(g, "▤  \u67e5\u770b\u8be6\u7ec6\u65e5\u5fd7", 235, 555, 18, White, false);
        DrawText(g, "⌃", 1500, 555, 18, Muted, false);
        using (var pen = new Pen(Color.FromArgb(35, 76, 122), 1F)) g.DrawRoundedRectangle(pen, new Rectangle(220, 580, 1325, 280), 10);
        DrawText(g, "♢  \u66f4\u65b0\u8fc7\u7a0b\u4e2d\u8bf7\u52ff\u5173\u95ed\u7a0b\u5e8f", 220, 915, 18, White, false);
    }

    private void DrawGear(Graphics g)
    {
        using (var pen = new Pen(Color.FromArgb(44, 145, 255), 7F))
        {
            g.DrawEllipse(pen, 930, 70, 160, 160); g.DrawEllipse(pen, 975, 115, 70, 70);
            for (var i = 0; i < 8; i++) { var a = i * Math.PI / 4; var x = 1010 + (int)(130 * Math.Cos(a)); var y = 150 + (int)(130 * Math.Sin(a)); g.DrawLine(pen, 1010, 150, x, y); }
        }
    }

    private void DrawProgress(Graphics g)
    {
        using (var brush = new SolidBrush(Color.FromArgb(34, 56, 88))) g.FillRoundedRectangle(brush, new Rectangle(235, 358, 1120, 30), 15);
        using (var brush = new SolidBrush(Cyan)) g.FillRoundedRectangle(brush, new Rectangle(235, 358, Math.Max(18, (int)(1120 * progressValue / 100F)), 30), 15);
        DrawText(g, progressValue + "%", 1380, 347, 34, White, true);
    }

    private void DrawUpdateSteps(Graphics g)
    {
        var labels = new[] { "\u4e0b\u8f7d\u66f4\u65b0\u5305", "\u6821\u9a8c\u6587\u4ef6", "\u5907\u4efd\u5f53\u524d\u7248\u672c", "\u66f4\u65b0\u7a0b\u5e8f\u6587\u4ef6", "\u66f4\u65b0\u670d\u52a1\u955c\u50cf", "\u5065\u5eb7\u68c0\u67e5" };
        for (var i = 0; i < 6; i++)
        {
            var x = 270 + i * 220;
            var done = progressValue >= (i + 1) * 17;
            using (var brush = new SolidBrush(done ? Green : Color.FromArgb(31, 48, 77))) g.FillEllipse(brush, x, 450, 50, 50);
            using (var pen = new Pen(Color.FromArgb(58, 91, 132), 3F)) if (i < 5) g.DrawLine(pen, x + 50, 475, x + 230, 475);
            DrawText(g, done ? "✓" : (i + 1).ToString(), x + 25, 461, 21, done ? White : Muted, true, StringAlignment.Center);
            DrawText(g, (i + 1) + "  " + labels[i], new RectangleF(x - 22, 520, 194, 28), 15, i == 4 && running ? Cyan : White, false, StringAlignment.Center, true);
        }
    }

    private void DrawStep(Graphics g, int x, int y, string number, string text, bool active)
    {
        using (var brush = new SolidBrush(active ? Blue : Color.FromArgb(226, 232, 240))) g.FillEllipse(brush, x - 24, y - 24, 48, 48);
        DrawText(g, number, x, y - 13, 23, active ? White : Color.FromArgb(71, 85, 105), false, StringAlignment.Center);
        DrawText(g, text, x, y + 49, 21, active ? Blue : Color.FromArgb(100, 116, 139), false, StringAlignment.Center);
        if (x < 1372) using (var pen = new Pen(Color.FromArgb(226, 232, 240), 3F)) g.DrawLine(pen, x + 50, y, x + 170, y);
    }

    private static void DrawText(Graphics g, string text, float x, float y, float size, Color color, bool bold, StringAlignment alignment = StringAlignment.Near)
    {
        using (var font = new Font("Microsoft YaHei UI", size, bold ? FontStyle.Bold : FontStyle.Regular))
        using (var brush = new SolidBrush(color))
        using (var format = new StringFormat { Alignment = alignment, LineAlignment = StringAlignment.Near })
            g.DrawString(text, font, brush, new PointF(x, y), format);
    }

    private static void DrawText(Graphics g, string text, RectangleF bounds, float size, Color color, bool bold, StringAlignment alignment, bool ellipsis)
    {
        using (var font = new Font("Microsoft YaHei UI", size, bold ? FontStyle.Bold : FontStyle.Regular))
        using (var brush = new SolidBrush(color))
        using (var format = new StringFormat { Alignment = alignment, LineAlignment = StringAlignment.Near, Trimming = ellipsis ? StringTrimming.EllipsisCharacter : StringTrimming.None, FormatFlags = StringFormatFlags.NoWrap })
            g.DrawString(text, font, brush, bounds, format);
    }

    private void ApplyRoundedRegion()
    {
        if (ClientSize.Width < 4 || ClientSize.Height < 4) return;
        using (var path = new GraphicsPath())
        {
            var radius = 18F;
            var d = radius * 2F;
            path.AddArc(0, 0, d, d, 180, 90);
            path.AddArc(ClientSize.Width - d, 0, d, d, 270, 90);
            path.AddArc(ClientSize.Width - d, ClientSize.Height - d, d, d, 0, 90);
            path.AddArc(0, ClientSize.Height - d, d, d, 90, 90);
            path.CloseFigure();
            Region = new Region(path);
        }
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        ApplyRoundedRegion();
        if (surface != null) surface.Invalidate();
    }

    private void Place(Control control, int x, int y, int w, int h)
    {
        var scale = Width / 1600F;
        control.SetBounds((int)(x * scale), (int)(y * scale), (int)(w * scale), (int)(h * scale));
    }

    private LauncherButton MakeButton(string text, Color back, Color fore, int radius)
    {
        return new LauncherButton { Text = text, BackColor = back, ForeColor = fore, Radius = radius, Font = new Font("Microsoft YaHei UI", 13F), BorderColor = Color.Transparent, FlatAppearance = { BorderSize = 0 } };
    }

    private void ChooseInstallPath()
    {
        using (var dialog = new FolderBrowserDialog())
        {
            dialog.SelectedPath = installPathBox.Text;
            if (dialog.ShowDialog(this) == DialogResult.OK) installPathBox.Text = dialog.SelectedPath;
        }
    }

    private void MoveWindow(object sender, MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left) return;
        ReleaseCapture(); SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero);
    }

    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    private async Task<int> RunScriptAsync(string scriptName, string extraArguments, string message)
    {
        if (busy) return -1;
        var path = Path.Combine(packageRoot, "scripts", scriptName);
        if (!File.Exists(path)) { SaveFailureLog(scriptName, -1, "Script not found: " + path); ShowFailure("\u6587\u4ef6\u7f3a\u5931", path); return -1; }
        busy = true; running = true; statusText = message; taskText = message; progressValue = 8; surface.Invalidate();
        AppendLog("\r\n[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] \u6267\u884c " + scriptName);
        try
        {
            var psi = new ProcessStartInfo { FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"), Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + Quote(path) + (string.IsNullOrWhiteSpace(extraArguments) ? "" : " " + extraArguments), WorkingDirectory = packageRoot, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
            psi.EnvironmentVariables["XIANYU_NONINTERACTIVE"] = "1";
            using (var process = new Process { StartInfo = psi, EnableRaisingEvents = true })
            {
                var done = new TaskCompletionSource<int>();
                var scriptOutput = new StringBuilder();
                process.OutputDataReceived += (s, e) =>
                {
                    if (e.Data == null) return;
                    lock (scriptOutput) scriptOutput.AppendLine(e.Data);
                    AppendLog(e.Data); UpdateProgress(e.Data);
                };
                process.ErrorDataReceived += (s, e) =>
                {
                    if (e.Data == null) return;
                    lock (scriptOutput) scriptOutput.AppendLine("[stderr] " + e.Data);
                    AppendLog("[stderr] " + e.Data);
                };
                process.Exited += (s, e) => done.TrySetResult(process.ExitCode);
                if (!process.Start()) throw new InvalidOperationException("\u65e0\u6cd5\u542f\u52a8 PowerShell");
                process.BeginOutputReadLine(); process.BeginErrorReadLine();
                var code = await done.Task.ConfigureAwait(true);
                process.WaitForExit();
                AppendLog("\u64cd\u4f5c\u7ed3\u675f\uff0c\u9000\u51fa\u7801: " + code);
                if (code == 3010) { progressValue = 70; statusText = "\u9700\u8981\u91cd\u542f"; HandleRestartRequired(); }
                else if (code != 0)
                {
                    progressValue = 0; statusText = "\u64cd\u4f5c\u5931\u8d25";
                    string captured;
                    lock (scriptOutput) captured = scriptOutput.ToString().Trim();
                    if (captured.Length > 24000) captured = "[\u65e5\u5fd7\u8fc7\u957f\uff0c\u4ec5\u4fdd\u7559\u672b\u5c3e]\r\n" + captured.Substring(captured.Length - 24000);
                    var detail = "\u9000\u51fa\u7801: " + code + "\r\n\r\n" + (captured.Length == 0 ? "\u672a\u6355\u83b7\u5230 PowerShell \u8f93\u51fa\u3002" : captured) + "\r\n\r\n\u8be6\u7ec6\u65e5\u5fd7\u4f4d于 app\\logs\u3002";
                    SaveFailureLog(scriptName, code, captured);
                    ShowFailure("\u64cd\u4f5c\u5931\u8d25", detail);
                }
                else { progressValue = 100; statusText = "\u64cd\u4f5c\u5b8c\u6210"; taskText = "\u64cd\u4f5c\u5b8c\u6210"; }
                surface.Invalidate(); return code;
            }
        }
        catch (Exception ex) { AppendLog(ex.ToString()); SaveFailureLog(scriptName, -1, ex.ToString()); statusText = "\u64cd\u4f5c\u5931\u8d25"; ShowFailure("\u542f\u52a8\u5931\u8d25", ex.ToString()); return -1; }
        finally { busy = false; surface.Invalidate(); }
    }

    private void SaveFailureLog(string scriptName, int exitCode, string captured)
    {
        try
        {
            var logDirectory = Path.Combine(appRoot, "logs");
            Directory.CreateDirectory(logDirectory);
            var path = Path.Combine(logDirectory, "launcher-error.log");
            var content = "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] " + scriptName + " exit_code=" + exitCode + Environment.NewLine + (captured ?? "") + Environment.NewLine + Environment.NewLine;
            File.AppendAllText(path, content, Encoding.UTF8);
        }
        catch (Exception ex) { AppendLog("\u65e0\u6cd5\u5199\u5165\u542f\u52a8\u5668\u9519\u8bef\u65e5\u5fd7: " + ex.Message); }
    }

    private void UpdateProgress(string line)
    {
        var text = (line ?? "").ToLowerInvariant();
        if (text.Contains("waiting for docker desktop") || text.Contains("try again")) { progressValue = Math.Max(progressValue, 32); taskText = "\u7b49\u5f85 Docker Desktop \u4e0a\u7ebf\uff0c\u8bf7\u5728 Docker \u4e2d\u70b9\u51fb Try again"; }
        else if (text.Contains("wsl") || text.Contains("docker desktop") || text.Contains("\u73af\u5883")) { progressValue = Math.Max(progressValue, 24); taskText = "\u6b63\u5728\u68c0\u67e5\u7cfb\u7edf\u73af\u5883"; }
        else if (text.Contains("import") || text.Contains("image") || text.Contains("\u955c\u50cf")) { progressValue = Math.Max(progressValue, 48); taskText = "\u6b63\u5728\u5bfc\u5165\u672c\u5730\u955c\u50cf"; }
        else if (text.Contains("start") || text.Contains("service") || text.Contains("\u542f\u52a8")) { progressValue = Math.Max(progressValue, 72); taskText = "\u6b63\u5728\u542f\u52a8\u4e1a\u52a1\u670d\u52a1"; }
        else if (text.Contains("health") || text.Contains("frontend") || text.Contains("\u5b8c\u6210")) { progressValue = Math.Max(progressValue, 92); taskText = "\u6b63\u5728\u5b8c\u6210\u5065\u5eb7\u68c0\u67e5"; }
        surface.Invalidate();
    }

    private void AppendLog(string line)
    {
        if (IsDisposed || logBox == null) return;
        if (InvokeRequired) { BeginInvoke(new Action<string>(AppendLog), line); return; }
        logBox.AppendText(line + Environment.NewLine); logBox.SelectionStart = logBox.TextLength; logBox.ScrollToCaret();
    }

    private void OpenApplication()
    {
        var port = "20000";
        var env = Path.Combine(appRoot, ".env");
        try { if (File.Exists(env)) foreach (var line in File.ReadAllLines(env, Encoding.UTF8)) if (line.StartsWith("FRONTEND_PORT=", StringComparison.OrdinalIgnoreCase)) port = line.Substring(14).Trim(); Process.Start(new ProcessStartInfo("http://127.0.0.1:" + port) { UseShellExecute = true }); }
        catch (Exception ex) { SaveFailureLog("open-application", -1, ex.ToString()); ShowFailure("\u65e0\u6cd5\u6253\u5f00\u7f51\u9875", ex.ToString()); }
    }

    private void ShowFailure(string title, string detail)
    {
        using (var dialog = new Form())
        {
            dialog.Text = title;
            dialog.StartPosition = FormStartPosition.CenterParent;
            dialog.ClientSize = new Size(760, 520);
            dialog.MinimizeBox = false;
            dialog.MaximizeBox = false;
            dialog.Font = new Font("Microsoft YaHei UI", 9F);
            dialog.BackColor = Color.White;

            var heading = new Label { Text = title, Location = new Point(22, 18), Size = new Size(710, 32), Font = new Font("Microsoft YaHei UI", 13F, FontStyle.Bold), ForeColor = Color.FromArgb(15, 23, 42) };
            var detailBox = new TextBox { Location = new Point(22, 62), Size = new Size(710, 330), Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, WordWrap = false, BorderStyle = BorderStyle.FixedSingle, Font = new Font("Consolas", 9F), BackColor = Color.FromArgb(248, 250, 252), Text = detail ?? "" };
            var result = new Label { Text = "日志不会自动上传。确认后可上传脱敏诊断包。", Location = new Point(22, 408), Size = new Size(710, 26), ForeColor = Color.FromArgb(100, 116, 139), AutoEllipsis = true };
            var upload = new Button { Text = "上传诊断日志", Location = new Point(360, 454), Size = new Size(128, 38), BackColor = Blue, ForeColor = Color.White, FlatStyle = FlatStyle.Flat };
            var open = new Button { Text = "打开日志目录", Location = new Point(498, 454), Size = new Size(118, 38), BackColor = Color.White, ForeColor = Color.FromArgb(30, 41, 59), FlatStyle = FlatStyle.Flat };
            var close = new Button { Text = "关闭", Location = new Point(626, 454), Size = new Size(106, 38), BackColor = Color.FromArgb(226, 232, 240), ForeColor = Color.FromArgb(30, 41, 59), FlatStyle = FlatStyle.Flat, DialogResult = DialogResult.OK };
            upload.FlatAppearance.BorderSize = 0; open.FlatAppearance.BorderColor = Color.FromArgb(203, 213, 225); close.FlatAppearance.BorderSize = 0;
            open.Click += (s, e) => { try { Process.Start("explorer.exe", "/select,\"" + Path.Combine(appRoot, "logs") + "\""); } catch (Exception ex) { result.Text = ex.Message; } };
            upload.Click += async (s, e) =>
            {
                upload.Enabled = false; open.Enabled = false; result.ForeColor = Color.FromArgb(37, 99, 235); result.Text = "正在生成并上传脱敏诊断包，请稍候...";
                var report = await UploadDiagnosticsAsync(title, detail);
                if (report.StartsWith("XY-", StringComparison.OrdinalIgnoreCase)) { result.ForeColor = Green; result.Text = "上传成功，报告编号：" + report; }
                else { result.ForeColor = Red; result.Text = report; upload.Enabled = true; open.Enabled = true; }
            };
            dialog.Controls.Add(heading); dialog.Controls.Add(detailBox); dialog.Controls.Add(result); dialog.Controls.Add(upload); dialog.Controls.Add(open); dialog.Controls.Add(close);
            dialog.AcceptButton = close;
            dialog.ShowDialog(this);
        }
    }

    private async Task<string> UploadDiagnosticsAsync(string title, string detail)
    {
        var script = Path.Combine(packageRoot, "scripts", "upload-diagnostics.ps1");
        if (!File.Exists(script)) return "诊断上传组件缺失，请先更新安装包。";
        try
        {
            if (!string.IsNullOrWhiteSpace(detail)) SaveFailureLog("diagnostic-context", 0, detail);
            var psi = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + Quote(script) + " -PackageRoot " + Quote(packageRoot) + " -ErrorSummary " + Quote(title) + " -ErrorStage " + Quote(role.ToString()),
                WorkingDirectory = packageRoot, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            };
            psi.EnvironmentVariables["XIANYU_NONINTERACTIVE"] = "1";
            using (var process = new Process { StartInfo = psi })
            {
                if (!process.Start()) return "无法启动诊断上传组件。";
                var outputTask = process.StandardOutput.ReadToEndAsync();
                var errorTask = process.StandardError.ReadToEndAsync();
                await Task.Run(() => process.WaitForExit()).ConfigureAwait(true);
                var output = await outputTask.ConfigureAwait(true);
                var error = await errorTask.ConfigureAwait(true);
                if (!string.IsNullOrWhiteSpace(output)) AppendLog(output.Trim());
                if (!string.IsNullOrWhiteSpace(error)) AppendLog("[stderr] " + error.Trim());
                if (process.ExitCode == 0)
                {
                    var marker = "Diagnostic report uploaded:";
                    var index = output.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
                    if (index >= 0) return output.Substring(index + marker.Length).Trim().Split(new[] { '\r', '\n', ' ' }, StringSplitOptions.RemoveEmptyEntries)[0];
                    return "XY-UPLOADED";
                }
                return "上传失败，请检查网络后重试。";
            }
        }
        catch (Exception ex) { AppendLog(ex.ToString()); return "上传失败：" + ex.Message; }
    }
    private void ShowInfo(string detail) { MessageBox.Show(this, detail, "\u63d0\u793a", MessageBoxButtons.OK, MessageBoxIcon.Information); }
    private void HandleRestartRequired()
    {
        var resumeRegistered = false;
        try
        {
            using (var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\RunOnce"))
            {
                if (key == null) throw new InvalidOperationException("RunOnce registry key is unavailable.");
                var resumeArguments = Quote(Application.ExecutablePath) + " --installer --resume";
                if (role == LauncherRole.Installer && installPathBox != null && !string.IsNullOrWhiteSpace(installPathBox.Text))
                    resumeArguments += " --install-path " + Quote(installPathBox.Text.Trim());
                key.SetValue("XianyuInstallerResume", resumeArguments, RegistryValueKind.String);
                resumeRegistered = true;
            }
            AppendLog("[xianyu] Installer resume registered for the next Windows sign-in.");
        }
        catch (Exception ex)
        {
            AppendLog("[xianyu] Unable to register automatic resume: " + ex.Message);
        }

        var detail = "WSL 2 \u6240\u9700\u7684 Windows \u7ec4\u4ef6\u5df2\u51c6\u5907\u5b8c\u6210\uff0c\u9700\u8981\u91cd\u542f\u7535\u8111\u3002\r\n\r\n" +
            (resumeRegistered ? "\u91cd\u542f\u5e76\u767b\u5f55 Windows \u540e\uff0c\u5b89\u88c5\u7a0b\u5e8f\u4f1a\u81ea\u52a8\u6062\u590d\uff0c\u5e76\u7b49\u5f85 Docker Desktop \u4e0a\u7ebf\u3002" : "\u65e0\u6cd5\u767b\u8bb0\u81ea\u52a8\u6062\u590d\uff0c\u91cd\u542f\u540e\u8bf7\u624b\u52a8\u518d\u6253\u5f00\u5b89\u88c5\u7a0b\u5e8f\u3002") +
            "\r\n\u5982\u679c Docker Desktop \u663e\u793a Try again\uff0c\u8bf7\u70b9\u51fb\u4e00\u6b21\uff1bDocker \u8fd0\u884c\u540e\u5b89\u88c5\u4f1a\u81ea\u52a8\u7ee7\u7eed\u3002\r\n\r\n\u662f\u5426\u73b0\u5728\u91cd\u542f\u7535\u8111\uff1f";
        var answer = MessageBox.Show(this, detail, "\u9700\u8981\u91cd\u542f\u7535\u8111", MessageBoxButtons.YesNo, MessageBoxIcon.Information);
        if (answer != DialogResult.Yes) return;
        try
        {
            var shutdown = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "shutdown.exe");
            Process.Start(new ProcessStartInfo { FileName = shutdown, Arguments = "/r /t 15 /c \"Xianyu setup will continue after Windows restarts.\"", UseShellExecute = false, CreateNoWindow = true });
        }
        catch (Exception ex) { ShowFailure("\u65e0\u6cd5\u81ea\u52a8\u91cd\u542f", ex.Message); }
    }
    private static string Quote(string value) { return "\"" + (value ?? "").Replace("\"", "\\\"") + "\""; }
    private static string ReadFirstLine(string path) { try { if (!File.Exists(path)) return "unknown"; using (var r = new StreamReader(path, Encoding.UTF8, true)) return (r.ReadLine() ?? "unknown").Trim(); } catch { return "unknown"; } }
}

internal sealed class VisualSurface : Panel
{
    internal Action<Graphics> PaintSurface;
    internal VisualSurface() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
    protected override void OnPaint(PaintEventArgs e) { if (PaintSurface != null) PaintSurface(e.Graphics); }
}

internal sealed class LauncherButton : Button
{
    internal int Radius { get; set; }
    internal Color BorderColor { get; set; }
    private bool hover;
    internal LauncherButton() { Radius = 8; BorderColor = Color.Transparent; FlatStyle = FlatStyle.Flat; FlatAppearance.BorderSize = 0; UseVisualStyleBackColor = false; SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true); }
    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        if (Radius <= 0 || Width < 4 || Height < 4) { Region = null; return; }
        using (var path = new GraphicsPath())
        {
            var d = Math.Min(Radius * 2, Math.Min(Width - 1, Height - 1));
            path.AddArc(0, 0, d, d, 180, 90);
            path.AddArc(Width - d - 1, 0, d, d, 270, 90);
            path.AddArc(Width - d - 1, Height - d - 1, d, d, 0, 90);
            path.AddArc(0, Height - d - 1, d, d, 90, 90);
            path.CloseFigure();
            Region = new Region(path);
        }
    }
    protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hover = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        e.Graphics.Clear(Parent == null ? BackColor : Parent.BackColor);
        var color = Enabled ? (hover ? Lighten(BackColor, 12) : BackColor) : Color.FromArgb(60, 72, 91);
        using (var brush = new SolidBrush(color)) e.Graphics.FillRoundedRectangle(brush, new Rectangle(0, 0, Width - 1, Height - 1), Radius);
        if (BorderColor.A > 0)
            using (var pen = new Pen(BorderColor, 1F)) e.Graphics.DrawRoundedRectangle(pen, new Rectangle(0, 0, Width - 1, Height - 1), Radius);
        TextRenderer.DrawText(e.Graphics, Text, Font, ClientRectangle, Enabled ? ForeColor : Color.FromArgb(148, 163, 184), TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
    }
    private static Color Lighten(Color color, int amount) { return Color.FromArgb(color.A, Math.Min(255, color.R + amount), Math.Min(255, color.G + amount), Math.Min(255, color.B + amount)); }
}

internal static class GraphicsExtensions
{
    internal static void FillRoundedRectangle(this Graphics g, Brush brush, Rectangle r, int radius) { using (var p = Path(r, radius)) g.FillPath(brush, p); }
    internal static void DrawRoundedRectangle(this Graphics g, Pen pen, Rectangle r, int radius) { using (var p = Path(r, radius)) g.DrawPath(pen, p); }
    private static GraphicsPath Path(Rectangle r, int radius)
    {
        var p = new GraphicsPath(); var d = Math.Max(2, radius * 2); var a = new Rectangle(r.X, r.Y, d, d);
        p.AddArc(a, 180, 90); a.X = r.Right - d; p.AddArc(a, 270, 90); a.Y = r.Bottom - d; p.AddArc(a, 0, 90); a.X = r.X; p.AddArc(a, 90, 90); p.CloseFigure(); return p;
    }
}

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        var name = Path.GetFileNameWithoutExtension(Application.ExecutablePath);
        var roleArg = args != null && args.Length > 0 ? args[0].ToLowerInvariant() : "";
        var resume = args != null && Array.Exists(args, value => string.Equals(value, "--resume", StringComparison.OrdinalIgnoreCase));
        var installPath = ReadArgument(args, "--install-path");
        var role = roleArg == "--installer" || name == LauncherText.Installer || name == "xianyu-installer" ? LauncherRole.Installer : roleArg == "--updater" || name == LauncherText.Updater || name == "xianyu-updater" ? LauncherRole.Updater : roleArg == "--stopper" || name == LauncherText.Stopper || name == "xianyu-stopper" ? LauncherRole.Stopper : roleArg == "--diagnostics" || name == LauncherText.Diagnostics || name == "xianyu-diagnostics" ? LauncherRole.Diagnostics : LauncherRole.Dashboard;
        Application.Run(new LauncherForm(role, resume, installPath));
    }

    private static string ReadArgument(string[] args, string name)
    {
        if (args == null) return "";
        for (var i = 0; i < args.Length - 1; i++)
            if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase)) return args[i + 1] ?? "";
        return "";
    }
}
