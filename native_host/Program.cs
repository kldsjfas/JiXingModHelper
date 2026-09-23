using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace JiXingModHelperHost;

internal static class Program
{
    internal const string WindowTitle = "吉星派对 Mod 助手 · 1.2.0";

    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        // 不同版本可以并排运行，重复启动当前版本时激活已有窗口。
        using var instanceMutex = new Mutex(true, @"Local\JiXingModHelper.v1.2.0.SingleInstance", out var isFirstInstance);
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
        // 默认接近用户截图外框 ~1866×1182（含标题栏），客户区略大便于浏览
        ClientSize = new Size(1840, 1120);
        MinimumSize = new Size(1280, 800);
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
