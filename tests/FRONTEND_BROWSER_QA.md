真实浏览器回归使用生产 HTML / CSS / JS，仅拦截 HTTP API。测试目录、作品集和 WAV 均为内存 fixture，不调用 Python 游戏后端。

在项目根目录启动只读静态服务：

```powershell
& '.\.venv\Scripts\python.exe' -m http.server 8797 --bind 127.0.0.1 --directory '.\astral_party_auto\webui'
```

另开终端，在项目根目录用 Node.js 的 `npx.cmd` 调用 Playwright CLI：

```powershell
New-Item -ItemType Directory -Force -Path '.\output\playwright\bug-audit-oct10' | Out-Null
npx.cmd --yes --package @playwright/cli playwright-cli -s=jixing-bugs-oct10 open about:blank
npx.cmd --yes --package @playwright/cli playwright-cli -s=jixing-bugs-oct10 run-code --filename '.\tests\frontend_browser_qa_setup.js' --raw
npx.cmd --yes --package @playwright/cli playwright-cli -s=jixing-bugs-oct10 snapshot
npx.cmd --yes --package @playwright/cli playwright-cli -s=jixing-bugs-oct10 run-code --filename '.\tests\frontend_browser_qa_checks.js' --raw
npx.cmd --yes --package @playwright/cli playwright-cli -s=jixing-bugs-oct10 close
```

结束后用 Ctrl+C 停止本次静态服务。截图保存于 `output/playwright/bug-audit-oct10`，应保留为验证证据。主逻辑无需测试专用钩子；延迟请求、WAV 播放和错误响应都由 route mock 提供。

该驱动验证三项用户操作：旧贴图分类迟到不覆盖文本分类；作品集详情失败后操作按钮禁用；音频已有可保存候选时，清空操作同步收回候选与播放器。音频编解码兼容性和游戏效果需另行测试，不能由这些 fixture 证明。
