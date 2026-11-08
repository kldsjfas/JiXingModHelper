using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace JiXingModHelperHost;

internal static class Program
{
    internal static string WindowTitle { get; private set; } = "吉星派对 Mod 助手";

    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var version = ReadArgument(args, "--version");
        if (string.IsNullOrWhiteSpace(version)) version = "dev";
        WindowTitle = $"吉星派对 Mod 助手 · {version}";
        var instanceVersion = new string(version.Where(character => char.IsLetterOrDigit(character) || character is '.' or '-').ToArray());
        // 不同版本可以并排运行，重复启动当前版本时激活已有窗口。
        using var instanceMutex = new Mutex(true, $@"Local\JiXingModHelper.{instanceVersion}.SingleInstance", out var isFirstInstance);
        if (!isFirstInstance)
        {
            ActivateExistingWindow();
            return;
        }

        var url = ReadArgument(args, "--url");
        if (string.IsNullOrWhiteSpace(url))
        {
            MessageBox.Show("缺少本地页面地址。", WindowTitle, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        using var window = new MainWindow(
            url,
            ReadArgument(args, "--profile"),
            ReadArgument(args, "--icon")
        );
        Application.Run(window);
    }

    private static void ActivateExistingWindow()
    {
        var window = NativeMethods.FindWindow(null, WindowTitle);
        if (window == IntPtr.Zero)
        {
            return;
        }

        const int RestoreWindow = 9;
        NativeMethods.ShowWindow(window, RestoreWindow);
        NativeMethods.SetForegroundWindow(window);
    }

    private static string ReadArgument(IReadOnlyList<string> args, string name)
    {
        for (var index = 0; index + 1 < args.Count; index++)
        {
            if (string.Equals(args[index], name, StringComparison.OrdinalIgnoreCase))
            {
                return args[index + 1];
            }
        }
        return string.Empty;
    }
}

internal static class NativeMethods
{
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    internal static extern IntPtr FindWindow(string? className, string windowName);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    internal static extern bool ShowWindow(IntPtr window, int command);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    internal static extern bool SetForegroundWindow(IntPtr window);
}

internal sealed class MainWindow : Form
{
    private readonly WebView2 _webView = new() { Dock = DockStyle.Fill };
    private readonly string _url;
    private readonly string _profilePath;
    private bool _webViewInitialized;

    public MainWindow(string url, string profilePath, string iconPath)
    {
        _url = url;
        _profilePath = profilePath;
        Text = Program.WindowTitle;
        var workingArea = Screen.FromPoint(Cursor.Position).WorkingArea;
        // 小屏幕和任务栏较高时也要能看到完整窗口及关闭按钮。
        Size = new Size(Math.Min(1860, Math.Max(1, workingArea.Width - 40)),
                        Math.Min(1160, Math.Max(1, workingArea.Height - 40)));
        MinimumSize = new Size(Math.Min(960, Width), Math.Min(640, Height));
        StartPosition = FormStartPosition.CenterScreen;
        Controls.Add(_webView);

        if (File.Exists(iconPath))
        {
            try
            {
                Icon = new Icon(iconPath);
            }
            catch (ArgumentException)
            {
                // 图标文件无效时仍可正常显示页面。
            }
        }

        Shown += async (_, _) => await InitializeWebViewAsync();
    }

    private async Task InitializeWebViewAsync()
    {
        if (_webViewInitialized)
        {
            return;
        }
        _webViewInitialized = true;

        try
        {
            if (!string.IsNullOrWhiteSpace(_profilePath))
            {
                Directory.CreateDirectory(_profilePath);
            }
            var options = new CoreWebView2EnvironmentOptions
            {
                // 本应用只连本地 127.0.0.1，禁用代理避免系统代理/VPN 把本地请求也拦掉
                AdditionalBrowserArguments = "--no-proxy-server"
            };
            var environment = await CoreWebView2Environment.CreateAsync(
                browserExecutableFolder: null,
                userDataFolder: string.IsNullOrWhiteSpace(_profilePath) ? null : _profilePath,
                options: options
            );
            await _webView.EnsureCoreWebView2Async(environment);
            _webView.CoreWebView2.NewWindowRequested += (_, eventArgs) =>
            {
                // 助手只使用当前本地页面。任何 window.open / target=_blank 都不应
                // 拉起系统浏览器或再生成一个 WebView 窗口。
                eventArgs.Handled = true;
            };
            _webView.CoreWebView2.NavigationStarting += (_, eventArgs) =>
            {
                if (!IsLocalNavigation(eventArgs.Uri))
                {
                    eventArgs.Cancel = true;
                }
            };
            _webView.Source = new Uri(_url);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            MessageBox.Show(
                "缺少 Microsoft Edge WebView2 Runtime，暂时无法显示助手界面。\n\n" +
                "请从微软官方页面安装 Evergreen WebView2 Runtime（Windows x64），再重新打开助手：\n" +
                "https://developer.microsoft.com/microsoft-edge/webview2/\n\n" +
                "助手已自带 Python 和 .NET，不需要另外安装；请保留完整解压目录。",
                Program.WindowTitle,
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            Close();
        }
        catch (Exception exception)
        {
            MessageBox.Show(
                $"无法初始化 HTML 界面。\n\n{exception.Message}",
                "吉星派对 Mod 助手",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            Close();
        }
    }

    private bool IsLocalNavigation(string target)
    {
        if (!Uri.TryCreate(_url, UriKind.Absolute, out var home) ||
            !Uri.TryCreate(target, UriKind.Absolute, out var destination))
        {
            return false;
        }

        return string.Equals(home.Scheme, destination.Scheme, StringComparison.OrdinalIgnoreCase) &&
               string.Equals(home.Host, destination.Host, StringComparison.OrdinalIgnoreCase) &&
               home.Port == destination.Port;
    }
}
