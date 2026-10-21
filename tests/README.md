# pyMD 回归测试

八个脚本，直接驱动真实 exe。不依赖 pytest / selenium / playwright，只用 Python 标准库。

## 前置

- 已构建好 `pymd.exe`（`build.bat`，带 `--custom-protocol`）。
- 脚本顶部的 `EXE` 常量改成实际产物路径，默认 `D:\mdpad-target\release\pymd.exe`。
- Python 3.8+。`regression_dialog.py` 会尝试用 Pillow 截图，没装就跳过截图，不影响断言。
- **跑之前先确认没有别的 pyMD 实例在跑**：WebView2 同一份用户数据目录只能有一个宿主持有一致的启动参数，
  第二个实例带不同参数启动会报 `0x8007139F` 并直接退出（表现是「等不到调试端口」）。

## 运行

```bash
cd tests
python regression.py         # 主功能 + 布局，158 项
python regression_win.py     # 窗口交互，20 项
python regression_dialog.py  # 原生对话框，16 项
python verify_about.py       # 关于/版权对话框，23 项
python layout.py             # 界面布局专项，41 项
python verify_icon.py        # 图标链路（含从 exe 里抠图标），20 项
python sync.py               # 分屏滚动联动 + 单换行渲染，22 项
python plaintext.py          # 纯文本导出（复制 / 另存 .txt），61 项
```

`layout.py` 会真的在 `D:\tmp\pymd_layoutdir` 造 40 多个文件，把侧边栏列表撑长，
专门盯「长列表压到『最近打开』文字上」这类溢出问题；主回归里的 1b 段只做轻量几何断言。

`sync.py` 在 `D:\tmp\pymd_syncdoc` 造一份 117 行的文档，靠 `data-sourcepos` 的行号
判断两侧是否对齐（**不要改成比像素**：源码一行经常渲染成一屏，像素永远对不上）。

每个脚本都会打印 `PASS / FAIL` 明细、末尾汇总，并把结果写到 `.artifacts/reg/result*.json`。
退出码 0 = 全过。

## 原理

| 环节 | 做法 |
| --- | --- |
| 驱动页面 | 用 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=923x` 启动，`cdp.py` 手写 WebSocket + JSON-RPC，走 `Runtime.evaluate` / `Page.captureScreenshot` |
| 读编辑器状态 | `window.__pymd`（只在 `localStorage.pymd-debug === "1"` 时挂出），能拿到完整 `EditorState`，包括没渲染到 DOM 的长文档 |
| 原生对话框 | 对话框在 WebView 之外，只能用 `SendInput` 往文件名框打完整路径再回车 |
| 窗口行为 | `GetWindowRect` / `IsIconic` / `IsZoomed` 读真实窗口状态；拖动和缩放用 `SendInput` 模拟鼠标 |
| 剪贴板断言 | `verify_about.py` / `plaintext.py` 用 `OpenClipboard` / `GetClipboardData` 直接读系统剪贴板，验证内容真的落到了剪贴板（`GetClipboardData` 的 `restype` 必须声明成 `c_void_p`，否则 64 位句柄被截断，读出来永远是空） |
| exe 图标断言 | `verify_icon.py` 用 `shell32.ExtractIconExW` 把 exe 资源段里的图标抠出来（注意：这个 API 在 shell32，不在 user32），再用 `DrawIconEx` 画进 DIB 读回 RGBA，和 `icons/32x32.png` 比对 |

为什么不用模拟鼠标点界面：无边框窗口的坐标随 DPI 和窗口位置变，且 `SetForegroundWindow` 会静默失败，
按键可能打到别的窗口。CDP 直接派发 DOM 事件更稳定，窗口类操作才用 Win32。

## 会改动什么

- 清空并重建 `localStorage`，测完把进入前的快照写回。
- 在 `.artifacts/regdocs/` 生成测试文档（alpha.md / beta.md / gamma.txt / saved-as.md 等）。
- 结束时 `kill()` 自己启动的进程，不影响你手动打开的实例。

`.artifacts/` 已进 `.gitignore`，随时可删。

## 已知的脚本约定

- 主回归不带 `Ctrl+S` 的弹窗路径（会阻塞），保存只测「已命名文件直接写盘」；另存为在 `regression_dialog.py` 里。
- 对话框脚本对键盘事件做了「没弹出就重试」的兜底，因为 `SendInput` 偶发丢失首个组合键。
- 认保存对话框不能靠「出现了新窗口」：提示框、阴影窗口也会算新窗口，`SendInput` 就打空了。
  要按「新出现 + 类名 `#32770` + 标题含另存为」三重条件筛。
- 两处「预期值」是照产品当前行为写的，不是理想行为：菜单栏的剪切/复制不置灰、两个草稿标签各占一格。
