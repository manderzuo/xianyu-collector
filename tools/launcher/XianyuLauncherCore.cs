// Core infrastructure for the Xianyu desktop launcher family.
//
// Architecture rule (UI rework 2026-09): PowerShell decides real state, this
// GUI only displays it. The GUI never infers progress from log keywords.
// State flows in as "@@XIANYU_UI@@{json}" protocol lines; plain lines are
// rendered as log text only.
//
// Compiled with the .NET Framework csc (C# 5 syntax only: no string
// interpolation, no null-conditional operators, no expression-bodied members).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal enum LauncherRole { Installer, Dashboard, Updater, Stopper, Diagnostics }

internal enum StageStatus { Pending, Running, Success, Warning, Failed, Skipped }

internal enum OperationState { Idle, Checking, Ready, Running, RollingBack, Completed, Failed }

internal static class LauncherText
{
    internal const string Product = "\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Installer = "\u5b89\u88c5\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Updater = "\u66f4\u65b0\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Stopper = "\u505c\u6b62\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
    internal const string Diagnostics = "\u8bca\u65ad\u95f2\u9c7c\u7ba1\u7406\u7cfb\u7edf";
}

// ---------------------------------------------------------------------------
// Design tokens. Spacing follows the 4/8/12/16/24/32 rhythm from the design
// spec; colours reference the agreed token list, never ad-hoc hex values.
// All pixel helpers scale by the system DPI factor once (system aware).
// ---------------------------------------------------------------------------
internal static class Ui
{
    internal static float DpiScale = 1F;
    // Real DC scale of the display. XIANYU_FORCE_DPI emulation multiplies
    // point-based fonts by DpiScale/ActualDpiScale so text grows with the
    // forced layout exactly like it would under a true system DPI.
    internal static float ActualDpiScale = 1F;

    // First opaque BackColor walking up the parent chain. WinForms Panels
    // default to Transparent; clearing a UserPaint buffer with it renders
    // BLACK, so custom controls must always resolve a real backdrop.
    internal static Color ResolveBackdrop(Control c)
    {
        while (c != null)
        {
            var bc = c.BackColor;
            if (bc != Color.Transparent && bc.A != 0) return bc;
            c = c.Parent;
        }
        return SystemColors.Window;
    }

    // Real product logo, embedded as an assembly resource
    // (assets/xianyu-app-icon.png -> "xianyu-app-icon.png"). Null in dev
    // builds compiled without the resource; painters then fall back to the
    // hand-drawn placeholder mark.
    internal static Bitmap BrandLogo;

    internal static void LoadBrandLogo()
    {
        try
        {
            var asm = typeof(Ui).Assembly;
            foreach (var name in asm.GetManifestResourceNames())
            {
                if (!name.EndsWith("xianyu-app-icon.png", StringComparison.OrdinalIgnoreCase)) continue;
                using (var stream = asm.GetManifestResourceStream(name))
                using (var decoded = new Bitmap(stream)) BrandLogo = new Bitmap(decoded);
                break;
            }
        }
        catch { BrandLogo = null; }
    }

    // Aspect-fit the brand logo inside the given box, centered, no tile or
    // background of its own (the PNG already carries transparency).
    internal static void DrawLogo(Graphics g, RectangleF box) { DrawLogo(g, box, 1F); }

    internal static void DrawLogo(Graphics g, RectangleF box, float alpha)
    {
        if (BrandLogo == null) return;
        var old = g.InterpolationMode;
        g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
        float s = Math.Min(box.Width / BrandLogo.Width, box.Height / BrandLogo.Height);
        float w = BrandLogo.Width * s, h = BrandLogo.Height * s;
        var dest = new RectangleF(box.X + (box.Width - w) / 2F, box.Y + (box.Height - h) / 2F, w, h);
        if (alpha >= 0.999F)
        {
            g.DrawImage(BrandLogo, dest);
        }
        else
        {
            using (var attrs = new System.Drawing.Imaging.ImageAttributes())
            {
                var cm = new System.Drawing.Imaging.ColorMatrix();
                cm.Matrix33 = alpha;
                attrs.SetColorMatrix(cm);
                g.DrawImage(BrandLogo, new Rectangle((int)dest.X, (int)dest.Y, (int)Math.Round(dest.Width), (int)Math.Round(dest.Height)), 0, 0, BrandLogo.Width, BrandLogo.Height, GraphicsUnit.Pixel, attrs);
            }
        }
        g.InterpolationMode = old;
    }

    internal static int Px(int designPx) { return (int)Math.Round(designPx * DpiScale); }
    internal static Padding Pad(int designPx) { return new Padding(Px(designPx)); }
    internal static Padding Pad(int h, int v) { return new Padding(Px(h), Px(v), Px(h), Px(v)); }
    internal static Padding Pad(int l, int t, int r, int b) { return new Padding(Px(l), Px(t), Px(r), Px(b)); }
    // Fonts are declared in design pixels and converted to points for 96dpi;
    // GDI point sizing handles the physical DPI itself.
    internal static float Pt(int designPx) { return designPx * 0.75F * (DpiScale / (ActualDpiScale > 0.01F ? ActualDpiScale : 1F)); }

    internal static readonly Color Background = Color.FromArgb(0x07, 0x17, 0x2F);
    internal static readonly Color Surface = Color.FromArgb(0x0C, 0x23, 0x44);
    internal static readonly Color SurfaceHover = Color.FromArgb(0x10, 0x2B, 0x50);
    internal static readonly Color Border = Color.FromArgb(0x1B, 0x45, 0x6F);
    internal static readonly Color Primary = Color.FromArgb(0x2F, 0x6F, 0xED);
    internal static readonly Color PrimaryHover = Color.FromArgb(0x40, 0x7C, 0xF3);
    internal static readonly Color TextPrimary = Color.FromArgb(0xF5, 0xF7, 0xFB);
    internal static readonly Color TextSecondary = Color.FromArgb(0xA8, 0xB4, 0xC8);
    internal static readonly Color TextMuted = Color.FromArgb(0x72, 0x82, 0x9C);
    internal static readonly Color Success = Color.FromArgb(0x28, 0xC7, 0x6F);
    internal static readonly Color Warning = Color.FromArgb(0xF5, 0xB9, 0x42);
    internal static readonly Color Danger = Color.FromArgb(0xFF, 0x5C, 0x65);
    internal static readonly Color DangerDim = Color.FromArgb(0x2A, 0x14, 0x20);

    internal static readonly Color LightBackground = Color.FromArgb(0xFA, 0xFB, 0xFD);
    internal static readonly Color LightCard = Color.White;
    internal static readonly Color LightBorder = Color.FromArgb(0xE2, 0xE8, 0xF0);
    internal static readonly Color LightText = Color.FromArgb(0x0F, 0x17, 0x2A);
    internal static readonly Color LightTextSecondary = Color.FromArgb(0x64, 0x74, 0x8B);
    internal static readonly Color RailBackground = Color.FromArgb(0x02, 0x19, 0x3F);
    internal static readonly Color HeaderBand = Color.FromArgb(0x05, 0x14, 0x2F);

    internal const int Space4 = 4, Space8 = 8, Space12 = 12, Space16 = 16, Space24 = 24, Space32 = 32;
    internal const int Radius = 8;
    internal const int TitleBarDesign = 56;
    internal const int ButtonHeightDesign = 40;

    private static readonly Dictionary<string, Font> FontCache = new Dictionary<string, Font>();

    internal static Font Font(int designPx, bool bold)
    {
        string key = designPx + (bold ? "b" : "r");
        Font font;
        if (FontCache.TryGetValue(key, out font)) return font;
        font = new Font("Microsoft YaHei UI", Pt(designPx), bold ? FontStyle.Bold : FontStyle.Regular, GraphicsUnit.Point);
        FontCache[key] = font;
        return font;
    }

    internal static Font Mono(int designPx)
    {
        Font font;
        string key = "mono" + designPx;
        if (FontCache.TryGetValue(key, out font)) return font;
        font = new Font("Consolas", Pt(designPx), FontStyle.Regular, GraphicsUnit.Point);
        FontCache[key] = font;
        return font;
    }
}

// ---------------------------------------------------------------------------
// Rounded rectangle helpers (shared by custom paints).
// ---------------------------------------------------------------------------
internal static class GraphicsExtensions
{
    internal static void FillRoundedRectangle(this Graphics g, Brush brush, Rectangle r, int radius) { using (var p = Path(r, radius)) g.FillPath(brush, p); }
    internal static void FillRoundedRectangle(this Graphics g, Brush brush, RectangleF r, float radius) { using (var p = PathF(r, radius)) g.FillPath(brush, p); }
    internal static void DrawRoundedRectangle(this Graphics g, Pen pen, Rectangle r, int radius) { using (var p = Path(r, radius)) g.DrawPath(pen, p); }
    internal static void DrawRoundedRectangle(this Graphics g, Pen pen, RectangleF r, float radius) { using (var p = PathF(r, radius)) g.DrawPath(pen, p); }

    private static GraphicsPath Path(Rectangle r, int radius)
    {
        var p = new GraphicsPath();
        var d = Math.Max(2, radius * 2);
        var a = new Rectangle(r.X, r.Y, d, d);
        p.AddArc(a, 180, 90); a.X = r.Right - d; p.AddArc(a, 270, 90); a.Y = r.Bottom - d; p.AddArc(a, 0, 90); a.X = r.X; p.AddArc(a, 90, 90); p.CloseFigure();
        return p;
    }

    private static GraphicsPath PathF(RectangleF r, float radius)
    {
        var p = new GraphicsPath();
        var d = Math.Max(2F, radius * 2F);
        if (d > r.Width) d = r.Width;
        if (d > r.Height) d = r.Height;
        var a = new RectangleF(r.X, r.Y, d, d);
        p.AddArc(a, 180, 90); a.X = r.Right - d; p.AddArc(a, 270, 90); a.Y = r.Bottom - d; p.AddArc(a, 0, 90); a.X = r.X; p.AddArc(a, 90, 90); p.CloseFigure();
        return p;
    }
}

// ---------------------------------------------------------------------------
// Lucide icon rendering. The geometry below is the verbatim SVG primitive
// data from lucide-static (ISC licence) so every icon in the product shares
// one library, one stroke weight and one proportion system (24-unit grid).
// ---------------------------------------------------------------------------
internal sealed class SvgShape
{
    internal char Kind;              // p path, c circle, r rect, l line, o polyline
    internal string Data;            // path d
    internal double[] Numbers;       // primitive numbers
}

internal static class Lucide
{
    private static readonly Dictionary<string, List<SvgShape>> Cache = new Dictionary<string, List<SvgShape>>();
    private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();

    // Raw primitives copied verbatim from lucide-static icon files.
    private static readonly Dictionary<string, string[]> Data = new Dictionary<string, string[]>
    {
        { "square-arrow-out-up-right", new[] { "p:M21 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h6|p:m21 3-9 9|p:M15 3h6v6" } },
        { "refresh-cw", new[] { "p:M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8", "p:M21 3v5h-5", "p:M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16", "p:M8 16H3v5" } },
        { "file-search", new[] { "p:M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z", "p:M14 2v5a1 1 0 0 0 1 1h5", "c:11.5,14.5,2.5", "p:M13.3 16.3 15 18" } },
        { "circle-stop", new[] { "c:12,12,10", "r:9,9,6,6,1" } },
        { "monitor", new[] { "r:2,3,20,14,2", "l:8,21,16,21", "l:12,17,12,21" } },
        { "server", new[] { "r:2,2,20,8,2", "r:2,14,20,8,2", "l:6,6,6.01,6", "l:6,18,6.01,18" } },
        { "message-square", new[] { "p:M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z" } },
        { "clock-3", new[] { "c:12,12,10", "p:M12 6v6h4" } },
        { "circle-check", new[] { "c:12,12,10", "p:m16 9-5.5 5.5L8 12" } },
        { "triangle-alert", new[] { "p:m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3", "p:M12 9v4", "p:M12 17h.01" } },
        { "circle-x", new[] { "c:12,12,10", "p:m15 9-6 6", "p:m9 9 6 6" } },
        { "info", new[] { "c:12,12,10", "p:M12 16v-4", "p:M12 8h.01" } },
        { "chevron-down", new[] { "p:m6 9 6 6 6-6" } },
        { "chevron-right", new[] { "p:m9 18 6-6-6-6" } },
        { "chevron-left", new[] { "p:m15 18-6-6 6-6" } },
        { "shield", new[] { "p:M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" } },
        { "wifi", new[] { "p:M12 20h.01", "p:M2 8.82a15 15 0 0 1 20 0", "p:M5 12.859a10 10 0 0 1 14 0", "p:M8.5 16.429a5 5 0 0 1 7 0" } },
        { "hard-drive", new[] { "p:M10 16h.01", "p:M2.212 11.577a2 2 0 0 0-.212.896V18a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-5.527a2 2 0 0 0-.212-.896L18.55 5.11A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z", "p:M21.946 12.013H2.054", "p:M6 16h.01" } },
        { "container", new[] { "p:M22 7.7c0-.6-.4-1.2-.8-1.5l-6.3-3.9a1.72 1.72 0 0 0-1.7 0l-10.3 6c-.5.2-.9.8-.9 1.4v6.6c0 .5.4 1.2.8 1.5l6.3 3.9a1.72 1.72 0 0 0 1.7 0l10.3-6c.5-.3.9-1 .9-1.5Z", "p:M10 21.9V14L2.1 9.1", "p:m10 14 11.9-6.9", "p:M14 19.8v-8.1", "p:M18 17.5V9.4" } },
        { "folder-open", new[] { "p:m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2" } },
        { "clipboard-copy", new[] { "r:8,2,8,4,1", "p:M8 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2", "p:M16 4h2a2 2 0 0 1 2 2v4", "p:M21 14H11", "p:m15 10-4 4 4 4" } },
        { "circle-help", new[] { "c:12,12,10", "p:M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3", "p:M12 17h.01" } },
        { "package", new[] { "p:M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z", "p:M12 22V12", "o:3.29,7 12,12 20.71,7", "p:m7.5 4.27 9 5.15" } },
        { "power", new[] { "p:M12 2v10", "p:M18.4 6.6a9 9 0 1 1-12.77.04" } },
        { "hourglass", new[] { "p:M5 22h14", "p:M5 2h14", "p:M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22", "p:M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2" } },
        { "globe", new[] { "c:12,12,10", "p:M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20", "p:M2 12h20" } },
        { "wrench", new[] { "p:M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" } },
    };

    internal static void Draw(Graphics g, string name, RectangleF box, Color color)
    {
        Draw(g, name, box, color, 2F);
    }

    internal static void Draw(Graphics g, string name, RectangleF box, Color color, float strokeOn24)
    {
        var shapes = GetShapes(name);
        if (shapes == null) return;
        var state = g.Save();
        g.SmoothingMode = SmoothingMode.AntiAlias;
        float scale = Math.Min(box.Width, box.Height) / 24F;
        float originX = box.Left + (box.Width - 24F * scale) / 2F;
        float originY = box.Top + (box.Height - 24F * scale) / 2F;
        float stroke = strokeOn24 * scale;
        if (stroke < 1F) stroke = 1F;
        using (var pen = new Pen(color, stroke))
        {
            pen.StartCap = LineCap.Round;
            pen.EndCap = LineCap.Round;
            pen.LineJoin = LineJoin.Round;
            foreach (var shape in shapes)
            {
                switch (shape.Kind)
                {
                    case 'p': DrawPathShape(g, pen, shape.Data, originX, originY, scale, stroke); break;
                    case 'c':
                        {
                            float cx = originX + (float)shape.Numbers[0] * scale;
                            float cy = originY + (float)shape.Numbers[1] * scale;
                            float r = (float)shape.Numbers[2] * scale;
                            g.DrawEllipse(pen, cx - r, cy - r, r * 2F, r * 2F);
                            break;
                        }
                    case 'r':
                        {
                            float x = originX + (float)shape.Numbers[0] * scale;
                            float y = originY + (float)shape.Numbers[1] * scale;
                            float w = (float)shape.Numbers[2] * scale;
                            float h = (float)shape.Numbers[3] * scale;
                            float rx = shape.Numbers.Length > 4 ? (float)shape.Numbers[4] * scale : 0F;
                            if (rx > 0.01F) g.DrawRoundedRectangle(pen, new RectangleF(x, y, w, h), rx);
                            else g.DrawRectangle(pen, x, y, w, h);
                            break;
                        }
                    case 'l':
                        g.DrawLine(pen,
                            originX + (float)shape.Numbers[0] * scale, originY + (float)shape.Numbers[1] * scale,
                            originX + (float)shape.Numbers[2] * scale, originY + (float)shape.Numbers[3] * scale);
                        break;
                    case 'o':
                        {
                            var pts = new PointF[shape.Numbers.Length / 2];
                            for (var i = 0; i < pts.Length; i++)
                                pts[i] = new PointF(originX + (float)shape.Numbers[i * 2] * scale, originY + (float)shape.Numbers[i * 2 + 1] * scale);
                            if (pts.Length > 1) g.DrawLines(pen, pts);
                            break;
                        }
                }
            }
        }
        g.Restore(state);
    }

    private static List<SvgShape> GetShapes(string name)
    {
        List<SvgShape> shapes;
        if (Cache.TryGetValue(name, out shapes)) return shapes;
        string[] raw;
        if (!Data.TryGetValue(name, out raw)) return null;
        shapes = new List<SvgShape>();
        foreach (var rawEntry in raw)
        {
            // Entries may bundle several shapes separated by '|'; split first.
            foreach (var entry in rawEntry.Split('|'))
            {
                var idx = entry.IndexOf(':');
                if (idx < 0) continue;
                var shape = new SvgShape();
                shape.Kind = entry[0];
                var payload = entry.Substring(idx + 1);
                if (shape.Kind == 'p') shape.Data = payload;
                else
                {
                    var parts = payload.Replace(" ", ",").Split(',');
                    var nums = new List<double>();
                    foreach (var part in parts)
                    {
                        double value;
                        if (part.Length == 0) continue;
                        if (double.TryParse(part, NumberStyles.Float, CultureInfo.InvariantCulture, out value)) nums.Add(value);
                    }
                    shape.Numbers = nums.ToArray();
                }
                shapes.Add(shape);
            }
        }
        Cache[name] = shapes;
        return shapes;
    }

    // --- SVG path parser/mini-renderer (M L H V C S Q A Z, abs + rel) ------
    private static void DrawPathShape(Graphics g, Pen pen, string d, float ox, float oy, float scale, float stroke)
    {
        using (var path = new GraphicsPath())
        {
            var dots = new List<PointF>();
            double x = 0, y = 0, startX = 0, startY = 0;
            char cmd = ' ';
            int i = 0;
            bool firstMove = true;
            char lastCmd = ' ';
            double lastCx = 0, lastCy = 0;   // last control point for S/Q reflection
            int guard = 0;
            while (i < d.Length)
            {
                // Pathological-path fuse: never let a parse stall burn memory.
                if (++guard > 20000) break;
                char c = d[i];
                if (c == ' ' || c == ',') { i++; continue; }
                bool isLetter = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
                if (isLetter) { cmd = c; i++; }
                else if (cmd != ' ' && c != '-' && c != '+' && c != '.' && !(c >= '0' && c <= '9'))
                {
                    // Junk character (e.g. a stray '|'): drop the implicit-repeat
                    // command so we cannot spin on a non-consuming number read.
                    cmd = ' '; i++; continue;
                }
                if (cmd == ' ') { i++; continue; }
                bool rel = char.IsLower(cmd);
                char upper = char.ToUpper(cmd);
                switch (upper)
                {
                    case 'M':
                        {
                            double nx = ReadNumber(d, ref i), ny = ReadNumber(d, ref i);
                            if (rel) { x += nx; y += ny; } else { x = nx; y = ny; }
                            if (firstMove) { path.StartFigure(); firstMove = false; }
                            else path.StartFigure();
                            startX = x; startY = y;
                            cmd = rel ? 'l' : 'L';
                            lastCmd = 'M';
                            break;
                        }
                    case 'L':
                        {
                            double nx = ReadNumber(d, ref i), ny = ReadNumber(d, ref i);
                            double fx = rel ? x + nx : nx, fy = rel ? y + ny : ny;
                            AddSegment(g, path, dots, x, y, fx, fy, ox, oy, scale, stroke);
                            x = fx; y = fy; lastCmd = 'L'; lastCx = x; lastCy = y;
                            break;
                        }
                    case 'H':
                        {
                            double nx = ReadNumber(d, ref i);
                            double fx = rel ? x + nx : nx;
                            AddSegment(g, path, dots, x, y, fx, y, ox, oy, scale, stroke);
                            x = fx; lastCmd = 'L'; lastCx = x; lastCy = y;
                            break;
                        }
                    case 'V':
                        {
                            double ny = ReadNumber(d, ref i);
                            double fy = rel ? y + ny : ny;
                            AddSegment(g, path, dots, x, y, x, fy, ox, oy, scale, stroke);
                            y = fy; lastCmd = 'L'; lastCx = x; lastCy = y;
                            break;
                        }
                    case 'C':
                        {
                            double x1 = ReadNumber(d, ref i), y1 = ReadNumber(d, ref i);
                            double x2 = ReadNumber(d, ref i), y2 = ReadNumber(d, ref i);
                            double nx = ReadNumber(d, ref i), ny = ReadNumber(d, ref i);
                            if (rel) { x1 += x; y1 += y; x2 += x; y2 += y; nx += x; ny += y; }
                            path.AddBezier(
                                (float)(ox + x * scale), (float)(oy + y * scale),
                                (float)(ox + x1 * scale), (float)(oy + y1 * scale),
                                (float)(ox + x2 * scale), (float)(oy + y2 * scale),
                                (float)(ox + nx * scale), (float)(oy + ny * scale));
                            lastCx = x2; lastCy = y2; x = nx; y = ny; lastCmd = 'C';
                            break;
                        }
                    case 'S':
                        {
                            double x2 = ReadNumber(d, ref i), y2 = ReadNumber(d, ref i);
                            double nx = ReadNumber(d, ref i), ny = ReadNumber(d, ref i);
                            if (rel) { x2 += x; y2 += y; nx += x; ny += y; }
                            double x1 = (lastCmd == 'C' || lastCmd == 'S') ? 2 * x - lastCx : x;
                            double y1 = (lastCmd == 'C' || lastCmd == 'S') ? 2 * y - lastCy : y;
                            path.AddBezier(
                                (float)(ox + x * scale), (float)(oy + y * scale),
                                (float)(ox + x1 * scale), (float)(oy + y1 * scale),
                                (float)(ox + x2 * scale), (float)(oy + y2 * scale),
                                (float)(ox + nx * scale), (float)(oy + ny * scale));
                            lastCx = x2; lastCy = y2; x = nx; y = ny; lastCmd = 'S';
                            break;
                        }
                    case 'Q':
                        {
                            double x1 = ReadNumber(d, ref i), y1 = ReadNumber(d, ref i);
                            double nx = ReadNumber(d, ref i), ny = ReadNumber(d, ref i);
                            if (rel) { x1 += x; y1 += y; nx += x; ny += y; }
                            double c1x = x + 2.0 / 3.0 * (x1 - x), c1y = y + 2.0 / 3.0 * (y1 - y);
                            double c2x = nx + 2.0 / 3.0 * (x1 - nx), c2y = ny + 2.0 / 3.0 * (y1 - ny);
                            path.AddBezier(
                                (float)(ox + x * scale), (float)(oy + y * scale),
                                (float)(ox + c1x * scale), (float)(oy + c1y * scale),
                                (float)(ox + c2x * scale), (float)(oy + c2y * scale),
                                (float)(ox + nx * scale), (float)(oy + ny * scale));
                            lastCx = x1; lastCy = y1; x = nx; y = ny; lastCmd = 'Q';
                            break;
                        }
                    case 'A':
                        {
                            double rx = ReadNumber(d, ref i), ry = ReadNumber(d, ref i);
                            double rotDeg = ReadNumber(d, ref i);
                            double largeArc = ReadNumber(d, ref i), sweep = ReadNumber(d, ref i);
                            double nx = ReadNumber(d, ref i), ny = ReadNumber(d, ref i);
                            if (rel) { nx += x; ny += y; }
                            AddArcBeziers(path, x, y, rx, ry, rotDeg, largeArc > 0.5, sweep > 0.5, nx, ny, ox, oy, scale);
                            x = nx; y = ny; lastCmd = 'A'; lastCx = x; lastCy = y;
                            break;
                        }
                    case 'Z':
                        path.CloseFigure();
                        x = startX; y = startY;
                        firstMove = true; lastCmd = 'Z';
                        break;
                    default:
                        i++;
                        break;
                }
            }
            g.DrawPath(pen, path);
            using (var brush = new SolidBrush(pen.Color))
                foreach (var dot in dots) g.FillEllipse(brush, dot.X - stroke / 2F, dot.Y - stroke / 2F, stroke, stroke);
        }
    }

    private static void AddSegment(Graphics g, GraphicsPath path, List<PointF> dots, double x1, double y1, double x2, double y2, float ox, float oy, float scale, float stroke)
    {
        double dx = (x2 - x1) * scale, dy = (y2 - y1) * scale;
        if (Math.Abs(dx) < 0.05 && Math.Abs(dy) < 0.05)
        {
            dots.Add(new PointF(ox + (float)(x1 * scale), oy + (float)(y1 * scale)));
            return;
        }
        path.AddLine((float)(ox + x1 * scale), (float)(oy + y1 * scale), (float)(ox + x2 * scale), (float)(oy + y2 * scale));
    }

    // SVG endpoint arc parameterisation to cubic beziers (handles rotation).
    private static void AddArcBeziers(GraphicsPath path, double x1, double y1, double rxIn, double ryIn, double rotDeg, bool largeArc, bool sweep, double x2, double y2, float ox, float oy, float scale)
    {
        double phi = rotDeg * Math.PI / 180.0;
        double cosPhi = Math.Cos(phi), sinPhi = Math.Sin(phi);
        double dx = (x2 - x1) / 2.0, dy = (y2 - y1) / 2.0;
        double x1p = cosPhi * dx + sinPhi * dy;
        double y1p = -sinPhi * dx + cosPhi * dy;
        double rx = Math.Abs(rxIn), ry = Math.Abs(ryIn);
        if (rx < 1e-9 || ry < 1e-9) { path.AddLine((float)(ox + x1 * scale), (float)(oy + y1 * scale), (float)(ox + x2 * scale), (float)(oy + y2 * scale)); return; }
        double lambda = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry);
        if (lambda > 1) { double s = Math.Sqrt(lambda); rx *= s; ry *= s; }
        double num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p;
        double den = rx * rx * y1p * y1p + ry * ry * x1p * x1p;
        double co = 0;
        if (den > 0 && num >= -1e-9) {
            num = Math.Max(num, 0);
            co = (largeArc == sweep ? -1 : 1) * Math.Sqrt(num / den);
        }
        double cxp = co * rx * y1p / ry;
        double cyp = -co * ry * x1p / rx;
        double cx = cosPhi * cxp - sinPhi * cyp + (x1 + x2) / 2.0;
        double cy = sinPhi * cxp + cosPhi * cyp + (y1 + y2) / 2.0;
        double theta1 = Angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry);
        double dTheta = Angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry);
        if (!sweep && dTheta > 0) dTheta -= 2 * Math.PI;
        else if (sweep && dTheta < 0) dTheta += 2 * Math.PI;
        int segments = (int)Math.Ceiling(Math.Abs(dTheta) / (Math.PI / 2 + 1e-9));
        if (segments < 1) segments = 1;
        double delta = dTheta / segments;
        double t = 4.0 / 3.0 * Math.Tan(delta / 4.0);
        for (var k = 0; k < segments; k++)
        {
            double th1 = theta1 + delta * k;
            double th2 = theta1 + delta * (k + 1);
            double cos1 = Math.Cos(th1), sin1 = Math.Sin(th1);
            double cos2 = Math.Cos(th2), sin2 = Math.Sin(th2);
            double ep1x = cx + rx * cosPhi * cos1 - ry * sinPhi * sin1;
            double ep1y = cy + rx * sinPhi * cos1 + ry * cosPhi * sin1;
            double ep2x = cx + rx * cosPhi * cos2 - ry * sinPhi * sin2;
            double ep2y = cy + rx * sinPhi * cos2 + ry * cosPhi * sin2;
            double d1x = -rx * cosPhi * sin1 - ry * sinPhi * cos1;
            double d1y = -rx * sinPhi * sin1 + ry * cosPhi * cos1;
            double d2x = -rx * cosPhi * sin2 - ry * sinPhi * cos2;
            double d2y = -rx * sinPhi * sin2 + ry * cosPhi * cos2;
            path.AddBezier(
                (float)(ox + ep1x * scale), (float)(oy + ep1y * scale),
                (float)(ox + (ep1x + t * d1x) * scale), (float)(oy + (ep1y + t * d1y) * scale),
                (float)(ox + (ep2x - t * d2x) * scale), (float)(oy + (ep2y - t * d2y) * scale),
                (float)(ox + ep2x * scale), (float)(oy + ep2y * scale));
        }
    }

    private static double Angle(double ux, double uy, double vx, double vy)
    {
        double dot = ux * vx + uy * vy;
        double len = Math.Sqrt((ux * ux + uy * uy) * (vx * vx + vy * vy));
        double a = len < 1e-12 ? 0 : Math.Acos(Math.Max(-1.0, Math.Min(1.0, dot / len)));
        if (ux * vy - uy * vx < 0) a = -a;
        return a;
    }

    private static double ReadNumber(string s, ref int i)
    {
        while (i < s.Length)
        {
            char c = s[i];
            if (c == ' ' || c == ',') { i++; continue; }
            break;
        }
        int start = i;
        if (i < s.Length && (s[i] == '-' || s[i] == '+')) i++;
        bool dotSeen = false;
        while (i < s.Length)
        {
            char c = s[i];
            if (c >= '0' && c <= '9') { i++; }
            else if (c == '.' && !dotSeen) { dotSeen = true; i++; }
            else break;
        }
        double value;
        if (i > start && double.TryParse(s.Substring(start, i - start), NumberStyles.Float, CultureInfo.InvariantCulture, out value)) return value;
        return 0;
    }

    internal static void DrawLogoMark(Graphics g, RectangleF bounds, Color blue)
    {
        using (var brush = new SolidBrush(blue)) g.FillRoundedRectangle(brush, bounds, Math.Max(8F, bounds.Width / 6F));
        var x = bounds.X; var y = bounds.Y; var w = bounds.Width; var h = bounds.Height;
        using (var white = new SolidBrush(Color.White))
        {
            g.FillEllipse(white, x + w * .20F, y + h * .20F, w * .48F, h * .48F);
            g.FillPolygon(white, new[] { new PointF(x + w * .56F, y + h * .49F), new PointF(x + w * .86F, y + h * .29F), new PointF(x + w * .82F, y + h * .66F) });
            g.FillEllipse(white, x + w * .20F, y + h * .56F, w * .36F, h * .24F);
        }
        using (var eye = new SolidBrush(blue)) g.FillEllipse(eye, x + w * .36F, y + h * .31F, Math.Max(2, w * .09F), Math.Max(2, h * .09F));
    }
}

// ---------------------------------------------------------------------------
// Structured state events from PowerShell. The GUI applies them verbatim.
// ---------------------------------------------------------------------------
internal sealed class GuiEvent
{
    internal string Type = "";
    internal string Operation = "";
    internal string Stage = "";
    internal StageStatus Status = StageStatus.Pending;
    internal bool HasStatus;
    internal int Progress = -1;
    internal string Detail = "";
    internal string Code = "";
    internal string Key = "";
    internal string Value = "";
    internal string ResultStatus = "";
    internal int Done = -1;
    internal int Total = -1;

    internal static StageStatus ParseStatus(string value)
    {
        if (value == "running") return StageStatus.Running;
        if (value == "success") return StageStatus.Success;
        if (value == "warning") return StageStatus.Warning;
        if (value == "failed") return StageStatus.Failed;
        if (value == "skipped") return StageStatus.Skipped;
        return StageStatus.Pending;
    }
}

internal static class GuiProtocol
{
    internal const string Prefix = "@@XIANYU_UI@@";
    private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();

    internal static bool TryParse(string line, out GuiEvent evt)
    {
        evt = null;
        if (line == null || !line.StartsWith(Prefix, StringComparison.Ordinal)) return false;
        try
        {
            var map = Serializer.DeserializeObject(line.Substring(Prefix.Length)) as IDictionary<string, object>;
            if (map == null) return false;
            var parsed = new GuiEvent();
            parsed.Type = GetString(map, "type");
            parsed.Operation = GetString(map, "operation");
            parsed.Stage = GetString(map, "stage");
            var status = GetString(map, "status");
            if (status.Length > 0)
            {
                parsed.Status = GuiEvent.ParseStatus(status);
                parsed.HasStatus = true;
            }
            parsed.Progress = GetInt(map, "progress", -1);
            parsed.Detail = GetString(map, "detail");
            parsed.Code = GetString(map, "code");
            parsed.Key = GetString(map, "key");
            parsed.Value = GetString(map, "value");
            parsed.Done = GetInt(map, "done", -1);
            parsed.Total = GetInt(map, "total", -1);
            if (parsed.Type == "result") parsed.ResultStatus = status;
            evt = parsed;
            return true;
        }
        catch { return false; }
    }

    internal static IDictionary<string, object> ParseObject(string json)
    {
        try { return Serializer.DeserializeObject(json) as IDictionary<string, object>; }
        catch { return null; }
    }

    internal static string GetString(IDictionary<string, object> map, string key)
    {
        object value;
        if (map != null && map.TryGetValue(key, out value) && value != null) return Convert.ToString(value, CultureInfo.InvariantCulture);
        return "";
    }

    internal static int GetInt(IDictionary<string, object> map, string key, int fallback)
    {
        object value;
        if (map != null && map.TryGetValue(key, out value) && value != null)
        {
            try { return Convert.ToInt32(value, CultureInfo.InvariantCulture); } catch { }
        }
        return fallback;
    }

    internal static bool GetBool(IDictionary<string, object> map, string key)
    {
        object value;
        if (map != null && map.TryGetValue(key, out value) && value != null)
        {
            if (value is bool) return (bool)value;
            string text = Convert.ToString(value, CultureInfo.InvariantCulture).Trim().ToLowerInvariant();
            return text == "true" || text == "1" || text == "yes";
        }
        return false;
    }
}

// ---------------------------------------------------------------------------
// Buttons. One component, four variants (Primary / Secondary / Danger /
// Ghost), plus a full-width Row list style. Icon and text are laid out as a
// centred group: true vertical centring, fixed 8px gap, no manual offsets.
// ---------------------------------------------------------------------------
internal sealed class ModernButton : Button
{
    internal enum ButtonVariant { Primary, Secondary, Danger, DangerSolid, Ghost, Row, LightSecondary }

    internal string IconName = "";
    internal string TrailingIconName = "";
    internal ButtonVariant Variant = ButtonVariant.Secondary;
    internal bool Loading;
    internal bool IconOnly;
    internal int TextDesignSize = 14;
    internal int RadiusDesign = 8;
    internal Color CustomBackColor = Color.Empty;
    internal Color CustomForeColor = Color.Empty;
    internal Color CustomBorderColor = Color.Empty;

    private bool hover;
    private bool pressed;
    private readonly Timer spinnerTimer = new Timer();
    private int spinnerAngle;

    internal ModernButton()
    {
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        UseVisualStyleBackColor = false;
        TabStop = false;
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        spinnerTimer.Interval = 60;
        spinnerTimer.Tick += (s, e) => { spinnerAngle = (spinnerAngle + 24) % 360; Invalidate(); };
    }

    internal void SetLoading(bool loading)
    {
        Loading = loading;
        if (loading) spinnerTimer.Start(); else spinnerTimer.Stop();
        Invalidate();
    }

    protected override void OnEnabledChanged(EventArgs e) { base.OnEnabledChanged(e); Invalidate(); }
    protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hover = false; pressed = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e) { pressed = true; Invalidate(); base.OnMouseDown(e); }
    protected override void OnMouseUp(MouseEventArgs e) { pressed = false; Invalidate(); base.OnMouseUp(e); }

    private Color BackFor()
    {
        if (!Enabled) return Variant == ButtonVariant.Row ? Ui.Surface : Color.FromArgb(0x23, 0x2C, 0x3B);
        if (Variant == ButtonVariant.Primary) return pressed ? Color.FromArgb(0x27, 0x60, 0xCF) : (hover ? Ui.PrimaryHover : Ui.Primary);
        if (Variant == ButtonVariant.DangerSolid) return pressed ? Color.FromArgb(0xD9, 0x4A, 0x52) : (hover ? Color.FromArgb(0xFF, 0x74, 0x7C) : Ui.Danger);
        if (Variant == ButtonVariant.Danger) return pressed ? Color.FromArgb(0x24, 0x14, 0x1B) : (hover ? Color.FromArgb(0x1B, 0x10, 0x14) : (CustomBackColor.IsEmpty ? Ui.Surface : CustomBackColor));
        if (Variant == ButtonVariant.Ghost) return hover ? Color.FromArgb(0x11, 0x22, 0x3F) : Color.Transparent;
        if (Variant == ButtonVariant.Row) return pressed ? Ui.SurfaceHover : (hover ? Ui.SurfaceHover : (CustomBackColor.IsEmpty ? Ui.Surface : CustomBackColor));
        if (Variant == ButtonVariant.LightSecondary) return pressed ? Color.FromArgb(0xE8, 0xEC, 0xF2) : (hover ? Color.FromArgb(0xF1, 0xF5, 0xF9) : (CustomBackColor.IsEmpty ? Color.White : CustomBackColor));
        return pressed ? Color.FromArgb(0x0A, 0x1E, 0x3C) : (hover ? Ui.SurfaceHover : (CustomBackColor.IsEmpty ? Ui.Surface : CustomBackColor));
    }

    private Color ForeFor()
    {
        if (!Enabled) return Ui.TextMuted;
        if (!CustomForeColor.IsEmpty) return CustomForeColor;
        if (Variant == ButtonVariant.Danger) return Ui.Danger;
        if (Variant == ButtonVariant.Ghost) return Ui.TextSecondary;
        if (Variant == ButtonVariant.LightSecondary) return Color.FromArgb(0x33, 0x41, 0x55);
        return Ui.TextPrimary;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Parent != null ? Ui.ResolveBackdrop(Parent) : Color.Transparent);
        var bounds = new Rectangle(0, 0, Width - 1, Height - 1);
        var back = BackFor();
        var fore = ForeFor();
        if (Variant != ButtonVariant.Ghost && back.A > 0)
            using (var brush = new SolidBrush(back)) g.FillRoundedRectangle(brush, bounds, Ui.Px(RadiusDesign));
        if (Variant == ButtonVariant.Row && Enabled && hover)
            using (var pen = new Pen(Ui.Border, 1F)) g.DrawRoundedRectangle(pen, bounds, Ui.Px(RadiusDesign));
        if (Variant == ButtonVariant.LightSecondary && !CustomBorderColor.IsEmpty)
            using (var pen = new Pen(CustomBorderColor, 1F)) g.DrawRoundedRectangle(pen, bounds, Ui.Px(RadiusDesign));
        if (Variant == ButtonVariant.Danger && !Enabled) fore = Color.FromArgb(0x8A, 0x4A, 0x50);

        int iconSize = Ui.Px(18);
        int gap = Ui.Px(8);
        float textRightLimit = Width - Ui.Px(12);
        float textLeftLimit = Ui.Px(12);
        int trailingSize = Ui.Px(16);

        var flags = TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding | TextFormatFlags.EndEllipsis;
        Size textSize = Size.Empty;
        if (!string.IsNullOrEmpty(Text))
            textSize = TextRenderer.MeasureText(g, Text, Ui.Font(TextDesignSize, Variant == ButtonVariant.Primary || Variant == ButtonVariant.DangerSolid), new Size(Ui.Px(900), Height), flags | TextFormatFlags.NoPrefix);
        if (IconOnly)
        {
            var iconBox = new RectangleF((Width - iconSize) / 2F, (Height - iconSize) / 2F, iconSize, iconSize);
            if (Loading) DrawSpinner(g, iconBox, fore);
            else if (IconName.Length > 0) Lucide.Draw(g, IconName, iconBox, fore);
            return;
        }
        bool hasIcon = IconName.Length > 0;
        float groupWidth = (hasIcon ? iconSize + gap : 0) + textSize.Width + (TrailingIconName.Length > 0 ? gap + trailingSize : 0);
        float centerX = (Width - groupWidth) / 2F;
        if (Variant == ButtonVariant.Row)
        {
            centerX = Ui.Px(14);
            textRightLimit = Width - Ui.Px(14) - (TrailingIconName.Length > 0 ? trailingSize + gap : 0);
        }
        var textRect = new Rectangle((int)centerX + (hasIcon ? iconSize + gap : 0), 0, Math.Max(0, (int)(textRightLimit - centerX - (hasIcon ? iconSize + gap : 0))), Height);
        if (textRect.Width > 0 && !string.IsNullOrEmpty(Text))
            TextRenderer.DrawText(g, Text, Ui.Font(TextDesignSize, Variant == ButtonVariant.Primary || Variant == ButtonVariant.DangerSolid), textRect, fore, flags | TextFormatFlags.Left | TextFormatFlags.NoPrefix);
        if (hasIcon)
        {
            var iconBox = new RectangleF(centerX, (Height - iconSize) / 2F, iconSize, iconSize);
            if (Loading) DrawSpinner(g, iconBox, fore);
            else Lucide.Draw(g, IconName, iconBox, fore);
        }
        if (TrailingIconName.Length > 0)
        {
            var box = new RectangleF(textRect.Right + gap, (Height - trailingSize) / 2F, trailingSize, trailingSize);
            Lucide.Draw(g, TrailingIconName, box, Enabled ? Ui.TextMuted : Color.FromArgb(0x3A, 0x46, 0x5C), 2.2F);
        }
    }

    private void DrawSpinner(Graphics g, RectangleF box, Color color)
    {
        using (var pen = new Pen(color, Math.Max(2F, box.Width / 7F)))
        {
            pen.StartCap = LineCap.Round;
            pen.EndCap = LineCap.Round;
            g.DrawArc(pen, box.X + box.Width * 0.15F, box.Y + box.Height * 0.15F, box.Width * 0.7F, box.Height * 0.7F, spinnerAngle, 260);
        }
    }
}

// ---------------------------------------------------------------------------
// Card container: surface + very weak border (spec: no border soup).
// ---------------------------------------------------------------------------
internal class Card : Panel
{
    internal Color SurfaceColor = Ui.Surface;
    internal Color BorderColor = Ui.Border;
    internal bool ShowBorder = true;
    internal int RadiusDesign = 8;

    internal Card()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        SetStyle(ControlStyles.ContainerControl, true);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Parent != null ? Ui.ResolveBackdrop(Parent) : BackColor);
        var bounds = new RectangleF(0, 0, Width - 1F, Height - 1F);
        using (var brush = new SolidBrush(SurfaceColor)) g.FillRoundedRectangle(brush, bounds, Ui.Px(RadiusDesign));
        if (ShowBorder && BorderColor.A > 0)
            using (var pen = new Pen(BorderColor, 1F)) g.DrawRoundedRectangle(pen, bounds, Ui.Px(RadiusDesign));
    }

    protected override void OnPaintBackground(PaintEventArgs e) { /* surface drawn in OnPaint */ }
}

// ---------------------------------------------------------------------------
// Determinate + indeterminate progress board. When the backend cannot supply
// a trustworthy percentage the bar animates instead of faking a number.
// ---------------------------------------------------------------------------
internal sealed class ProgressBoard : Control
{
    private int value = -1;                       // -1 = indeterminate
    private bool dark = true;
    private readonly Timer marquee = new Timer();
    private int offset;

    internal ProgressBoard()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        DoubleBuffered = true;
        Height = Ui.Px(24);
        marquee.Interval = 30;
        marquee.Tick += (s, e) => { offset = (offset + 14) % (Math.Max(Width, 1) + Ui.Px(120)); Invalidate(); };
        marquee.Start();
    }

    internal bool Dark { get { return dark; } set { dark = value; Invalidate(); } }

    internal int Value
    {
        get { return value; }
        set
        {
            int v = value;
            if (v < 0) v = -1;
            if (v > 100) v = 100;
            if (v == value) return;
            this.value = v;
            Invalidate();
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        var trackColor = dark ? Color.FromArgb(0x22, 0x38, 0x58) : Color.FromArgb(0xE2, 0xE8, 0xF0);
        var fillColor = Ui.Primary;
        var track = new RectangleF(0, (Height - Ui.Px(8)) / 2F, Width, Ui.Px(8));
        using (var brush = new SolidBrush(trackColor)) g.FillRoundedRectangle(brush, track, Ui.Px(4));
        if (value >= 0)
        {
            float w = track.Width * value / 100F;
            if (w < Ui.Px(8)) w = Ui.Px(8);
            using (var brush = new SolidBrush(fillColor)) g.FillRoundedRectangle(brush, new RectangleF(0, track.Y, w, track.Height), Ui.Px(4));
        }
        else
        {
            int segW = Ui.Px(120);
            float x = (offset - segW) % (Width + segW);
            if (x < -segW) x += Width + segW;
            var clip = g.Clip;
            g.SetClip(new RectangleF(0, track.Y, Width, track.Height));
            using (var brush = new SolidBrush(fillColor)) g.FillRoundedRectangle(brush, new RectangleF(x, track.Y, segW, track.Height), Ui.Px(4));
            g.Clip = clip;
        }
    }
}

// ---------------------------------------------------------------------------
// One named stage with a real status glyph, detail text and optional action
// buttons. Used by the environment check, install and update flows.
// ---------------------------------------------------------------------------
internal sealed class StageRow : Panel
{
    internal readonly Label DetailLabel;
    internal StageStatus Status = StageStatus.Pending;
    internal string ErrorCode = "";
    private readonly string icon;
    private readonly FlowLayoutPanel actions;
    private bool lastDetailKnownVisible;

    internal StageRow(string title, string iconName)
    {
        icon = iconName;
        Dock = DockStyle.Top;
        Height = Ui.Px(64);
        BackColor = Color.Transparent;
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 3,
            RowCount = 1,
            BackColor = Color.Transparent
        };
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(44)));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var textPanel = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, BackColor = Color.Transparent };
        textPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(24)));
        textPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(26)));
        textPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        var titleLabel = new Label
        {
            Text = title,
            Font = Ui.Font(15, true),
            ForeColor = Color.FromArgb(0x33, 0x41, 0x55),
            AutoSize = false,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.BottomLeft
        };
        DetailLabel = new Label
        {
            Text = "",
            Font = Ui.Font(13, false),
            ForeColor = Color.FromArgb(0x94, 0xA3, 0xB8),
            AutoSize = false,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft,
            AutoEllipsis = true
        };
        textPanel.Controls.Add(titleLabel, 0, 0);
        textPanel.Controls.Add(DetailLabel, 0, 1);
        actions = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.LeftToRight, WrapContents = false, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = Color.Transparent, Padding = new Padding(0, 0, 0, 0) };
        layout.Controls.Add(textPanel, 1, 0);
        layout.Controls.Add(actions, 2, 0);
        Controls.Add(layout);
        Paint += OnRowPaint;
    }

    internal void SetState(StageStatus status, string detail, string code)
    {
        Status = status;
        ErrorCode = code ?? "";
        if (detail != null) DetailLabel.Text = detail;
        Invalidate();
    }

    internal void SetState(StageStatus status, string detail) { SetState(status, detail, ""); }

    internal void ClearActions() { actions.Controls.Clear(); }

    internal void AddAction(string text, string iconName, Action handler)
    {
        var button = new ModernButton
        {
            Text = text,
            IconName = iconName,
            Variant = ModernButton.ButtonVariant.LightSecondary,
            CustomBorderColor = Color.FromArgb(0xCB, 0xD5, 0xE1),
            Height = Ui.Px(40),
            Width = Ui.Px(150),
            Margin = new Padding(0, Ui.Px(12), Ui.Px(8), 0),
            TextDesignSize = 13
        };
        button.Click += (s, e) => handler();
        actions.Controls.Add(button);
        Invalidate();
    }

    private void OnRowPaint(object sender, PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        var iconColor = IconColor();
        var iconBox = new RectangleF(Ui.Px(6), (Height - Ui.Px(28)) / 2F, Ui.Px(28), Ui.Px(28));
        Lucide.Draw(g, icon, iconBox, iconColor, 1.9F);
    }

    private Color IconColor()
    {
        switch (Status)
        {
            case StageStatus.Running: return Color.FromArgb(0x3B, 0x82, 0xF6);
            case StageStatus.Success: return Color.FromArgb(0x16, 0xA3, 0x4A);
            case StageStatus.Warning: return Color.FromArgb(0xD9, 0x9A, 0x1B);
            case StageStatus.Failed: return Color.FromArgb(0xDC, 0x26, 0x26);
            default: return Color.FromArgb(0x94, 0xA3, 0xB8);
        }
    }
}

// ---------------------------------------------------------------------------
// Status badge (small coloured dot + text, spec §7: no giant green checks).
// ---------------------------------------------------------------------------
internal sealed class StatusBadge : Control
{
    internal string Text2 = "";
    internal Color DotColor = Ui.TextMuted;
    internal Color TextColor = Ui.TextSecondary;
    internal int FontSizeDesign = 14;

    internal StatusBadge()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor, true);
        BackColor = Color.Transparent;
        Height = Ui.Px(20);
    }

    internal void Set(string text, Color dot, Color text2)
    {
        Text2 = text;
        DotColor = dot;
        TextColor = text2;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        var bg = BackColor;
        var ancestor = Parent;
        while ((bg == Color.Transparent || bg.A == 0) && ancestor != null) { bg = ancestor.BackColor; ancestor = ancestor.Parent; }
        if (bg == Color.Transparent || bg.A == 0) bg = SystemColors.Window;
        g.Clear(bg);
        float dotSize = Ui.Px(8);
        float dotY = (Height - dotSize) / 2F;
        using (var brush = new SolidBrush(DotColor)) g.FillEllipse(brush, 0, dotY, dotSize, dotSize);
        var textRect = new Rectangle((int)dotSize + Ui.Px(8), 0, Math.Max(0, Width - (int)dotSize - Ui.Px(8)), Height);
        TextRenderer.DrawText(g, Text2, Ui.Font(FontSizeDesign, false), textRect, TextColor, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
    }
}

// ---------------------------------------------------------------------------
// Collapsible section: header row with rotating chevron + body panel.
// ---------------------------------------------------------------------------
internal sealed class CollapsibleSection : Panel
{
    internal readonly Panel Body = new Panel();
    private readonly ModernButton header;
    private readonly TableLayoutPanel root;
    private bool expanded;
    private int collapsedHeight = Ui.Px(40);
    private int expandedHeight = Ui.Px(240);

    internal CollapsibleSection(string title)
    {
        Dock = DockStyle.Top;
        root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, collapsedHeight));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 0));
        header = new ModernButton
        {
            Text = title,
            IconName = "info",
            Variant = ModernButton.ButtonVariant.Ghost,
            Dock = DockStyle.Fill,
            TrailingIconName = "chevron-down"
        };
        header.TextAlign = ContentAlignment.MiddleLeft;
        Body.Dock = DockStyle.Fill;
        Body.Visible = false;
        root.Controls.Add(header, 0, 0);
        root.Controls.Add(Body, 0, 1);
        Controls.Add(root);
        header.Click += (s, e) => SetExpanded(!expanded);
        Height = collapsedHeight;
    }

    internal bool Expanded { get { return expanded; } }

    internal void SetExpanded(bool value2)
    {
        expanded = value2;
        collapsedHeight = Ui.Px(40);
        root.RowStyles[0].Height = collapsedHeight;
        root.RowStyles[1].Height = expanded ? expandedHeight : 0;
        Body.Visible = expanded;
        Height = collapsedHeight + (expanded ? expandedHeight : 0) + Padding.Top + Padding.Bottom;
        header.TrailingIconName = expanded ? "chevron-down" : "chevron-down";
        Invalidate();
    }

    internal void SetExpandedHeight(int designPx) { expandedHeight = Ui.Px(designPx); }

    internal void SetHeader(string text) { header.Text = text; }
}

// ---------------------------------------------------------------------------
// Shared borderless window with rounded corners and a 56px title bar.
// ---------------------------------------------------------------------------
internal class LauncherWindow : Form
{
    private Panel titleBar;
    protected TableLayoutPanel contentHost;
    private ChromeButton minimizeButton;
    private ChromeButton maximizeButton;
    private ChromeButton closeButton;
    private Label titleLabel;
    private bool lightChrome;

    internal LauncherWindow(string title, bool light)
    {
        lightChrome = light;
        Text = title;
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        BackColor = light ? Ui.LightBackground : Ui.Background;
        Font = Ui.Font(14, false);
        SetStyle(ControlStyles.ResizeRedraw, true);
        AutoScaleMode = AutoScaleMode.None;

        TryLoadIcon();

        contentHost = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1, BackColor = BackColor, Padding = new Padding(0) };
        contentHost.RowStyles.Add(new RowStyle(SizeType.Absolute, Ui.Px(56)));
        contentHost.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        contentHost.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));

        titleBar = new Panel { Dock = DockStyle.Fill, BackColor = light ? Color.White : Ui.HeaderBand, Padding = Ui.Pad(20, 0, 8, 0) };
        var barLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 6, RowCount = 1 };
        barLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        barLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(36)));
        barLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Ui.Px(10)));
        barLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        barLayout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        barLayout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        barLayout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var logoBox = new LogoBox();
        logoBox.Dock = DockStyle.Fill;
        titleLabel = new Label
        {
            Text = title,
            Font = Ui.Font(16, true),
            ForeColor = light ? Color.FromArgb(0x0F, 0x17, 0x2A) : Ui.TextPrimary,
            AutoSize = false,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft
        };
        minimizeButton = new ChromeButton(ChromeButton.GlyphKind.Minimize, light);
        maximizeButton = new ChromeButton(ChromeButton.GlyphKind.Maximize, light);
        closeButton = new ChromeButton(ChromeButton.GlyphKind.Close, light);
        minimizeButton.Click += (s, e) => WindowState = FormWindowState.Minimized;
        maximizeButton.Click += (s, e) => WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized;
        closeButton.Click += (s, e) => Close();
        barLayout.Controls.Add(logoBox, 0, 0);
        barLayout.Controls.Add(titleLabel, 2, 0);
        barLayout.Controls.Add(minimizeButton, 3, 0);
        barLayout.Controls.Add(maximizeButton, 4, 0);
        barLayout.Controls.Add(closeButton, 5, 0);
        titleBar.Controls.Add(barLayout);
        titleBar.MouseDown += MoveWindowDrag;
        titleLabel.MouseDown += MoveWindowDrag;
        logoBox.MouseDown += MoveWindowDrag;

        contentHost.Controls.Add(titleBar, 0, 0);
        Controls.Add(contentHost);

        // Clamp the default size to the working area at high DPI. Use the
        // PRIMARY screen: the window opens with CenterScreen, so sizing it
        // against whatever monitor the cursor happens to hover is wrong.
        var area = Screen.PrimaryScreen.WorkingArea;
        int width = Math.Min(Ui.Px(1280), area.Width - Ui.Px(40));
        int height = Math.Min(Ui.Px(768), area.Height - Ui.Px(40));
        ClientSize = new Size(width, height);
        MinimumSize = new Size(Math.Min(Ui.Px(1040), width), Math.Min(Ui.Px(680), height));
    }

    private void TryLoadIcon()
    {
        // The window/taskbar icon comes from the EXE's embedded win32icon
        // resource (assets/xianyu-launcher.ico at build time); in-window
        // painting uses Ui.BrandLogo. Nothing file-based to load.
    }

    protected void SetTitle(string title) { titleLabel.Text = title; }

    // Subclasses place their role content under the title bar.
    protected void SetContent(Control control)
    {
        control.Dock = DockStyle.Fill;
        contentHost.Controls.Add(control, 0, 1);
    }

    protected ChromeButton MaximizeControl { get { return maximizeButton; } }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        ApplyRoundedRegion();
    }

    private void ApplyRoundedRegion()
    {
        if (ClientSize.Width < 4 || ClientSize.Height < 4) return;
        if (WindowState == FormWindowState.Maximized) { Region = null; return; }
        using (var path = new GraphicsPath())
        {
            var radius = Ui.Px(14);
            var d = radius * 2;
            path.AddArc(0, 0, d, d, 180, 90);
            path.AddArc(ClientSize.Width - d, 0, d, d, 270, 90);
            path.AddArc(ClientSize.Width - d, ClientSize.Height - d, d, d, 0, 90);
            path.AddArc(0, ClientSize.Height - d, d, d, 90, 90);
            path.CloseFigure();
            Region = new Region(path);
        }
    }

    private void MoveWindowDrag(object sender, MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left) return;
        ReleaseCapture();
        SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero);
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [System.Runtime.InteropServices.DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    internal sealed class LogoBox : Control
    {
        internal LogoBox()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor, true);
            BackColor = Color.Transparent;
            Width = Ui.Px(36);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            var box = new RectangleF((Width - Ui.Px(32)) / 2F, (Height - Ui.Px(32)) / 2F, Ui.Px(32), Ui.Px(32));
            if (Ui.BrandLogo != null)
            {
                Ui.DrawLogo(g, box);
                return;
            }
            Lucide.DrawLogoMark(g, new RectangleF((Width - Ui.Px(24)) / 2F, (Height - Ui.Px(24)) / 2F, Ui.Px(24), Ui.Px(24)), Color.FromArgb(0x23, 0x73, 0xF0));
        }
    }
}

internal sealed class ChromeButton : Control
{
    internal enum GlyphKind { Minimize, Maximize, Restore, Close }

    private GlyphKind kind;
    private readonly bool light;
    private bool hover;
    private bool pressed;

    internal ChromeButton(GlyphKind glyphKind, bool lightChrome)
    {
        kind = glyphKind;
        light = lightChrome;
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor, true);
        BackColor = Color.Transparent;
        Width = Ui.Px(44);
        Height = Ui.Px(40);
        Margin = new Padding(0, 0, Ui.Px(2), 0);
    }

    internal void SetGlyph(GlyphKind value2) { kind = value2; Invalidate(); }

    protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hover = false; pressed = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e) { pressed = true; Invalidate(); base.OnMouseDown(e); }
    protected override void OnMouseUp(MouseEventArgs e) { pressed = false; Invalidate(); base.OnMouseUp(e); }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Parent != null ? Ui.ResolveBackdrop(Parent) : Color.Transparent);
        if (hover)
        {
            var hoverColor = kind == GlyphKind.Close ? Color.FromArgb(0xEF, 0x44, 0x44) : (light ? Color.FromArgb(0xE8, 0xEC, 0xF2) : Color.FromArgb(0x18, 0x2A, 0x4A));
            using (var brush = new SolidBrush(hoverColor)) g.FillRoundedRectangle(brush, new RectangleF(1, (Height - Ui.Px(32)) / 2F, Width - 2F, Ui.Px(32)), Ui.Px(6));
        }
        var color = kind == GlyphKind.Close && hover ? Color.White : (light ? Color.FromArgb(0x33, 0x41, 0x55) : Ui.TextSecondary);
        float line = Math.Max(1.6F, Ui.Px(2) * 0.9F);
        using (var pen = new Pen(color, line))
        {
            pen.StartCap = LineCap.Round;
            pen.EndCap = LineCap.Round;
            float cx = Width / 2F, cy = Height / 2F, r = Ui.Px(6);
            switch (kind)
            {
                case GlyphKind.Minimize:
                    g.DrawLine(pen, cx - r, cy, cx + r, cy);
                    break;
                case GlyphKind.Maximize:
                    g.DrawRectangle(pen, cx - r, cy - r, r * 2, r * 2);
                    break;
                case GlyphKind.Restore:
                    g.DrawRectangle(pen, cx - r + 2, cy - r + 2, r * 2 - 4, r * 2 - 4);
                    break;
                case GlyphKind.Close:
                    g.DrawLine(pen, cx - r, cy - r, cx + r, cy + r);
                    g.DrawLine(pen, cx + r, cy - r, cx - r, cy + r);
                    break;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Script runner: launches the bundled PowerShell scripts with the GUI
// protocol enabled and streams stdout lines back to the window.
// ---------------------------------------------------------------------------
internal sealed class ScriptRunner
{
    internal sealed class Result
    {
        internal int ExitCode;
    }

    private static void Trace(string line)
    {
        AppOps.Trace("runner " + line);
    }

    private static int replayRunIndex;

    private static string ResolveReplayFile(string replay)
    {
        if (File.Exists(replay)) return replay;
        if (!Directory.Exists(replay)) return null;
        var files = Directory.GetFiles(replay, "*.txt");
        System.Array.Sort(files, System.StringComparer.Ordinal);
        if (files.Length == 0) return null;
        var idx = System.Threading.Interlocked.Increment(ref replayRunIndex) - 1;
        if (idx >= files.Length) idx = files.Length - 1;
        return files[idx];
    }

    internal static Task<Result> RunAsync(string packageRoot, string scriptName, string arguments, Action<string> onLine)
    {
        var completion = new TaskCompletionSource<Result>();
        try
        {
            // Test hook: replay a recorded stdout stream instead of running the
            // script. Enables deterministic UI-state verification with zero
            // side effects (no docker, no network). Directives: #exit N, #delay MS.
            // A directory value sequences one file per run (1-*.txt, 2-*.txt, ...).
            var replayFile = ResolveReplayFile(Environment.GetEnvironmentVariable("XIANYU_GUI_REPLAY"));
            if (replayFile != null)
            {
                Trace("replay: " + replayFile);
                var replayLines = File.ReadAllLines(replayFile, Encoding.UTF8);
                var t = Task.Run(delegate
                {
                    var exitCode = 0;
                    foreach (var line in replayLines)
                    {
                        if (line.StartsWith("#exit ")) { int.TryParse(line.Substring(6).Trim(), out exitCode); continue; }
                        if (line.StartsWith("#delay ")) { int d; if (int.TryParse(line.Substring(7).Trim(), out d)) System.Threading.Thread.Sleep(d); continue; }
                        if (onLine != null) { try { onLine(line); } catch { } }
                        System.Threading.Thread.Sleep(120);
                    }
                    return new Result { ExitCode = exitCode };
                });
                t.ContinueWith(delegate { completion.TrySetResult(t.Result); });
                return completion.Task;
            }
            var path = Path.Combine(packageRoot, "scripts", scriptName);
            if (!File.Exists(path))
            {
                completion.TrySetResult(new Result { ExitCode = -1 });
                if (onLine != null) onLine("[launcher] script not found: " + path);
                return completion.Task;
            }
            Trace("run: " + path + " " + arguments);
            var psi = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File " + Quote(path) + (string.IsNullOrEmpty(arguments) ? "" : " " + arguments),
                WorkingDirectory = packageRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            psi.EnvironmentVariables["XIANYU_NONINTERACTIVE"] = "1";
            psi.EnvironmentVariables["XIANYU_GUI_PROTOCOL"] = "1";
            var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            var anyLine = onLine;
            process.OutputDataReceived += (s, e) =>
            {
                if (e.Data == null) { Trace("stdout EOF"); return; }
                Trace("out: " + (e.Data.Length > 140 ? e.Data.Substring(0, 140) : e.Data));
                if (anyLine != null) { try { anyLine(e.Data); } catch (Exception ex) { Trace("out handler threw: " + ex.Message); } }
            };
            process.ErrorDataReceived += (s, e) =>
            {
                if (e.Data == null) { Trace("stderr EOF"); return; }
                Trace("err: " + (e.Data.Length > 140 ? e.Data.Substring(0, 140) : e.Data));
                if (anyLine != null) { try { anyLine("[stderr] " + e.Data); } catch (Exception ex) { Trace("err handler threw: " + ex.Message); } }
            };
            process.Exited += (s, e) =>
            {
                Trace("process Exited raw");
                try { process.WaitForExit(); } catch (Exception ex) { Trace("wait threw " + ex.Message); }
                completion.TrySetResult(new Result { ExitCode = process.ExitCode });
                Trace("completion set");
                process.Dispose();
            };
            if (!process.Start()) { completion.TrySetResult(new Result { ExitCode = -2 }); return completion.Task; }
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
        catch (Exception ex)
        {
            if (onLine != null) onLine("[launcher] " + ex.Message);
            completion.TrySetResult(new Result { ExitCode = -3 });
        }
        return completion.Task;
    }

    internal static string Quote(string value) { return "\"" + (value ?? "").Replace("\"", "\\\"") + "\""; }
}
