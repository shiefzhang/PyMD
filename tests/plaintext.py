"""纯文本导出：「复制为纯文本」+「导出为纯文本 (.txt)」。

验四层：
1. 菜单结构（两个入口在菜单里的位置）
2. 「复制为纯文本」写进系统剪贴板的内容 —— 逐项验标记确实被去掉、该留的都留着
3. 选区复制只转选区（不是整篇）
4. 「导出为纯文本」落盘的 .txt 与剪贴板内容逐字节一致，且**不改绑标签**

第 4 条最关键：导出只是扔一份副本，源文档还得是原来的 .md，
否则用户接着按 Ctrl+S 会把 Markdown 源码以纯文本写回，标记全丢。

剪贴板用 ctypes 直接读（应用是用 Win32 写的，Python 这边也直连），
原生保存对话框用 SendInput 打字 —— 那段管道复用 regression_dialog.py。
"""
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, wait_targets  # noqa: E402

# SendInput / 窗口枚举那套只在原生对话框用到，直接复用，不抄第二份
import regression_dialog as RD  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9265
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".artifacts", "plain")
DOCS = os.path.join(HERE, ".artifacts", "plaindocs")
SRC = os.path.join(DOCS, "sample.md")
TXT = os.path.join(DOCS, "sample.txt")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13

# 这几个函数的返回值是 64 位句柄，不声明 restype 会被 ctypes 截成 int32，
# 拿到的是个废指针 —— 表现出来就是「剪贴板明明是空的」。
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.CloseClipboard.restype = wt.BOOL
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL

for _dll in (user32, RD.user32):
    _dll.GetForegroundWindow.restype = wt.HWND

results = []


def check(name, value, expect=None):
    ok = True if expect is None else value == expect
    results.append({"step": name, "ok": ok, "got": value, "expect": expect})
    print(("  PASS " if ok else "  FAIL ") + name + " -> " + json.dumps(value, ensure_ascii=False))
    return ok


def js(cdp, code):
    return cdp.eval(code)


def wait_for(cdp, expr, timeout=15, label=""):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if js(cdp, expr):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("等待超时: %s <- %s" % (label, expr))


def read_clipboard():
    """读系统剪贴板的 CF_UNICODETEXT。"""
    if not user32.OpenClipboard(None):
        raise RuntimeError("打不开剪贴板")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(ptr)
    finally:
        user32.CloseClipboard()


def force_foreground_window(h, attempts=3):
    """把窗口推到前台并**确认真的到位**（SetForegroundWindow 会被前台锁静默拒掉）。"""
    if not h:
        return False
    for _ in range(attempts):
        if RD.force_foreground(h) and user32.GetForegroundWindow() == h:
            return True
        time.sleep(0.4)
    return False


def wait_save_dialog(before, timeout=10.0):
    """等「新出现的、标题含另存为的 #32770」窗口。

    不用 `wait_new_window`：它遇到任何一个新窗口（工具提示、阴影窗口）就返回，
    拿到的句柄不是对话框，后面 SendInput 全打空。
    """
    end = time.time() + timeout
    while time.time() < end:
        for handle, (title, cls) in RD.windows_snapshot().items():
            if handle in before:
                continue
            if cls == "#32770" and ("另存为" in title or "Save" in title):
                return handle
        time.sleep(0.25)
    return None


SAMPLE = """# 纯文本导出验证

这是一段**粗体**、*斜体* 和 `行内代码` 混排的正文，还有一个[链接文字](https://example.com/a?b=1)和一个行内公式 $a^2+b^2=c^2$。

软换行第一行
软换行第二行

## 列表

- 第一项
- 第二项 **加粗**
  - 嵌套项甲
  - 嵌套项乙

1. 有序一
2. 有序二

- [x] 已完成
- [ ] 未完成

- 松散项第一段

  松散项第二段

- 松散项二的正文

## 表格

| 名称 | 数量 |
| --- | --- |
| 苹果 | 3 |
| 香蕉 | 5 |

## 代码

```rust
fn main() {
    // 这一行里的 # 不是标题
    let s = 1 + 1;
}
```

> 引用里的一段话

<div class="raw">原始 HTML 该被丢掉</div>

脚注引用[^1]

[^1]: 这是脚注内容。
"""


def make_doc():
    if os.path.isdir(DOCS):
        shutil.rmtree(DOCS, ignore_errors=True)
    os.makedirs(DOCS, exist_ok=True)
    with io.open(SRC, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SAMPLE)
    if os.path.exists(TXT):
        os.remove(TXT)
    return SRC


def click_menu_item(cdp, menu_label, item_label):
    """展开某个菜单并点其中一项（菜单项在 DOM 里，隐藏也能 .click()）。"""
    return js(
        cdp,
        """(() => {
             const wrap = [...document.querySelectorAll('#menubar .dropdown')]
               .find(d => d.querySelector('.dropdown-trigger').textContent === %s);
             if (!wrap) return 'no-menu';
             wrap.querySelector('.dropdown-trigger').click();
             const btn = [...wrap.querySelectorAll('.dropdown-item')]
               .find(b => b.children[0].textContent === %s);
             if (!btn) return 'no-item';
             btn.click();
             return 'ok';
           })()""" % (json.dumps(menu_label), json.dumps(item_label)),
    )


def menu_snapshot(cdp):
    raw = js(
        cdp,
        """JSON.stringify([...document.querySelectorAll('#menubar .dropdown')].map(d => ({
             label: d.querySelector('.dropdown-trigger').textContent,
             items: [...d.querySelectorAll('.dropdown-item')].map(b => b.children[0].textContent),
           })))""",
    )
    return json.loads(raw)


def main():
    subprocess.run(["taskkill", "/IM", "pymd.exe", "/F"], capture_output=True)
    time.sleep(1)
    os.makedirs(OUT, exist_ok=True)
    doc = make_doc()

    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--remote-debugging-port=%d --remote-allow-origins=*" % PORT
    )
    proc = subprocess.Popen(
        [EXE],
        env=env,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
        time.sleep(1.2)

        session = json.dumps({"paths": [doc], "activePath": doc}, ensure_ascii=False)
        js(
            cdp,
            """(() => {
                 localStorage.clear();
                 localStorage.setItem('pymd-debug', '1');
                 localStorage.setItem('pymd-tabs', %s);
                 localStorage.setItem('pymd-view', 'split');
                 return 1;
               })()""" % json.dumps(session),
        )
        js(cdp, "location.reload(); true")
        time.sleep(2.2)
        cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
        wait_for(cdp, "window.__pymd && window.__pymd.editor.getDoc().length > 100", 20, "文档载入")
        time.sleep(0.6)

        # ---------- 1. 菜单结构 ----------
        print("\n== 1. 两个入口在菜单里的位置")
        menus = {m["label"]: m["items"] for m in menu_snapshot(cdp)}
        file_items = menus.get("文件", [])
        edit_items = menus.get("编辑", [])
        check("文件菜单有「导出为纯文本 (.txt)」", "导出为纯文本 (.txt)" in file_items, True)
        check(
            "它紧跟「另存为」",
            file_items.index("导出为纯文本 (.txt)") - file_items.index("另存为") == 1,
            True,
        )
        check("编辑菜单有「复制为纯文本」", "复制为纯文本" in edit_items, True)
        check(
            "它紧跟「复制」",
            edit_items.index("复制为纯文本") - edit_items.index("复制") == 1,
            True,
        )

        # ---------- 2. 复制为纯文本（全文） ----------
        print("\n== 2. 复制为纯文本：标记被去掉，文字都在")
        check("点菜单项", click_menu_item(cdp, "编辑", "复制为纯文本"), "ok")
        time.sleep(1.2)
        text = read_clipboard()
        check("剪贴板有内容", len(text) > 200, True)
        check("标题的 # 没了", "# 纯文本导出验证" in text, False)
        check("标题文字还在", "纯文本导出验证" in text, True)
        check("粗体标记没了", "**粗体**" in text, False)
        check("粗体文字还在", "这是一段粗体、斜体 和 行内代码 混排的正文" in text, True)
        check("行内代码反引号没了", "`行内代码`" in text, False)
        check("链接只剩文字", "[链接文字](https://example.com" in text, False)
        check("链接文字还在", "链接文字" in text, True)
        check("链接地址没混进去", "example.com" in text, False)
        check("公式留 TeX 源码", "a^2+b^2=c^2" in text, True)
        check("公式的 $ 没了", "$$" in text, False)
        check("段内换行保留成换行", "软换行第一行\n软换行第二行" in text, True)
        check("无序列表保留 -", "- 第一项" in text, True)
        check("列表项里的粗体也摊平", "- 第二项 加粗" in text, True)
        check("嵌套列表紧贴父项", "- 第二项 加粗\n  - 嵌套项甲\n  - 嵌套项乙" in text, True)
        check("嵌套列表没有多缩一层", "\n    - 嵌套项" in text, False)
        check("有序列表保留 1.", "1. 有序一\n2. 有序二" in text, True)
        check("任务列表保留 [x]", "- [x] 已完成" in text, True)
        check("任务列表保留 [ ]", "- [ ] 未完成" in text, True)
        check("任务列表两项紧挨着（没被插空行）", "- [x] 已完成\n- [ ] 未完成" in text, True)
        # loose 列表（项内写了空行）保留段间空行，并缩进两格
        check("loose 列表项内保留空行", "- 松散项第一段\n\n  松散项第二段" in text, True)
        check("列表项之间不空行", "  松散项第二段\n- 松散项二的正文" in text, True)
        check("表格单元格用 | 分隔", "名称 | 数量" in text, True)
        check("表格数据行", "苹果 | 3" in text, True)
        check("表格分隔行没了", "| --- |" in text, False)
        check("代码块围栏没了", "```" in text, False)
        check("代码块原文保留", "fn main() {" in text, True)
        check("代码块里的注释没被当标题", "// 这一行里的 # 不是标题" in text, True)
        check("引用符号 > 去掉", "> 引用里的一段话" in text, False)
        check("引用文字还在", "引用里的一段话" in text, True)
        check("原始 HTML 整块丢掉", "原始 HTML 该被丢掉" in text, False)
        check("HTML 标签没了", "<div" in text, False)
        check("行内脚注引用留编号", "脚注引用[1]" in text, True)
        check("脚注定义留条文", "[1] 这是脚注内容。" in text, True)
        check("输出结尾是换行", text.endswith("\n"), True)
        check("正文里没有连续三个换行", "\n\n\n" in text, False)

        with io.open(os.path.join(OUT, "clip-full.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        full_clip = text

        # ---------- 3. 选区复制 ----------
        print("\n== 3. 有选区时只转选区")
        js(
            cdp,
            """(() => {
                 const view = window.__pymd.editor.view;
                 const doc = view.state.doc;
                 // 按内容找行，别写死行号：样例文档改一行就会错位
                 let target = 0;
                 for (let i = 1; i <= doc.lines; i++) {
                   if (doc.line(i).text === '- 第一项') { target = i; break; }
                 }
                 const line = doc.line(target);
                 view.dispatch({selection: {anchor: line.from, head: line.to}});
                 return target;
               })()""",
        )
        time.sleep(0.3)
        selected = js(
            cdp,
            """window.__pymd.editor.view.state.sliceDoc(
                 window.__pymd.editor.view.state.selection.main.from,
                 window.__pymd.editor.view.state.selection.main.to)""",
        )
        check("选中了列表项原文", selected, "- 第一项")
        check("点菜单项", click_menu_item(cdp, "编辑", "复制为纯文本"), "ok")
        time.sleep(1.2)
        part = read_clipboard()
        check("选区复制结果是单项", part.strip(), "- 第一项")
        check("没有把整篇带进来", "纯文本导出验证" in part, False)
        status = js(cdp, "document.getElementById('status-stat').textContent")
        check("状态栏说明是选区", "选区" in status, True)

        # 清掉选区，恢复全文
        js(
            cdp,
            """(() => {
                 const view = window.__pymd.editor.view;
                 view.dispatch({selection: {anchor: 0}});
                 return 1;
               })()""",
        )

        # ---------- 4. 导出为 .txt ----------
        print("\n== 4. 导出为纯文本：落盘 + 不改绑标签")
        hwnd = RD.find_hwnd_for_pid(proc.pid)
        check("找到应用窗口", hwnd is not None, True)
        check("前台化成功", RD.force_foreground(hwnd), True)

        before = RD.windows_snapshot()
        check("点菜单项", click_menu_item(cdp, "文件", "导出为纯文本 (.txt)"), "ok")

        # 认真正的保存对话框：类名 #32770 是 Windows 标准对话框，
        # 标题里带「另存为」。其余新窗口（工具提示等）一律不算。
        dialog = wait_save_dialog(before, 10)
        check("找到保存对话框窗口", dialog is not None, True)
        if dialog is not None:
            check("对话框标题是「另存为」",
                  RD.windows_snapshot().get(dialog, ("", ""))[0], "另存为")

        # 对话框虽然弹出来了，但 Windows 的前台锁不一定把焦点给它 ——
        # 这时 SendInput 打出去的键全落到主窗口，文件名框一个字都不会变。
        # 所以先把对话框推到前台并确认，再开始打字。
        check("对话框拿到前台", force_foreground_window(dialog), True)

        # 文件名框里预填的就是 defaultPath。**什么都不改，直接回车** ——
        # 落盘若正好是本目录下的 sample.txt，就同时证明了「扩展名换成 .txt」
        # 和「沿用原标题」这两点，比去对话框里抠控件文本稳得多。
        # SendInput 偶发丢首个按键，所以回车也要重试。
        for attempt in range(3):
            RD.send([(RD.VK_RETURN, 0), (RD.VK_RETURN, RD.KEYUP)])
            deadline = time.time() + 6
            while time.time() < deadline and not os.path.exists(TXT):
                time.sleep(0.25)
            if os.path.exists(TXT):
                break
            print("  第 %d 次回车没落盘，重试" % (attempt + 1))
            force_foreground_window(dialog)
        check("回车即按默认名存成 sample.txt", os.path.exists(TXT), True)

        # 万一默认目录不在本目录（系统「另存为」记着上次的目录），
        # 再走一遍「全选 + 打完整路径」，保证后面的内容断言有文件可读
        if not os.path.exists(TXT):
            for attempt in range(3):
                RD.chord([RD.VK_CONTROL], RD.VK_A)
                time.sleep(0.2)
                RD.type_text(TXT)
                time.sleep(0.6)
                RD.send([(RD.VK_RETURN, 0), (RD.VK_RETURN, RD.KEYUP)])

                deadline = time.time() + 8
                while time.time() < deadline and not os.path.exists(TXT):
                    time.sleep(0.25)
                if os.path.exists(TXT):
                    break
                print("  第 %d 次没落盘，重试" % (attempt + 1))
                force_foreground_window(dialog)

        check("txt 已落盘", os.path.exists(TXT), True)

        # 写盘是异步的：文件刚出现时可能还被写句柄占着（Windows 共享冲突），
        # 一边等可读一边等文件大小稳定
        body = None
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with io.open(TXT, "rb") as fh:
                    raw = fh.read()
                if raw:
                    body = raw
                    break
            except PermissionError:
                pass
            time.sleep(0.25)

        if body is None:
            check("能读到 txt 内容", False, True)
        else:
            check("没有 BOM", body[:3] == b"\xef\xbb\xbf", False)
            text_on_disk = body.decode("utf-8")
            check("落盘内容与剪贴板逐字节一致", text_on_disk == full_clip, True)
            check("落盘内容里没有 # 标题", "# 纯文本导出验证" in text_on_disk, False)
            with io.open(os.path.join(OUT, "on-disk.txt"), "w", encoding="utf-8",
                         newline="\n") as fh:
                fh.write(text_on_disk)

        time.sleep(1.0)
        tab = json.loads(
            js(
                cdp,
                """JSON.stringify({
                     names: [...document.querySelectorAll('.doc-tab-name')].map(n => n.textContent),
                     dirty: document.getElementById('status-dirty').textContent,
                     path: document.getElementById('status-path').textContent,
                     doc: window.__pymd.editor.getDoc(),
                   })""",
            )
        )
        check("标签名没改成 .txt", tab["names"], ["sample.md"])
        check("标签没变脏", tab["dirty"], "")
        check("状态栏路径仍是源文件", tab["path"].replace("/", "\\").lower(), SRC.lower())
        check("编辑器里还是 Markdown 源码", tab["doc"].startswith("# 纯文本导出验证"), True)

        try:
            from PIL import ImageGrab

            ImageGrab.grab().save(os.path.join(OUT, "after-export.png"))
        except Exception as exc:
            print("  截图跳过:", exc)
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    passed = sum(1 for r in results if r["ok"])
    print("\n汇总: %d 项，通过 %d，失败 %d" % (len(results), passed, len(results) - passed))
    for r in results:
        if not r["ok"]:
            print("  FAIL:", r["step"], "got=", json.dumps(r["got"], ensure_ascii=False),
                  "expect=", json.dumps(r["expect"], ensure_ascii=False))
    os.makedirs(os.path.join(HERE, ".artifacts", "reg"), exist_ok=True)
    with io.open(os.path.join(HERE, ".artifacts", "reg", "result_plain.json"), "w",
                 encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
