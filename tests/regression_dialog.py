"""pyMD 原生文件对话框回归：Ctrl+O 打开、Ctrl+Shift+S 另存为。

原生对话框在 WebView 之外，CDP 点不到，只能用 SendInput 往对话框的「文件名」框里
打完整路径再回车。校验放在两处：磁盘上真有这个文件 + 应用里的标签状态。
"""
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, wait_targets  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9235
BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, ".artifacts", "regdocs")
OUT = os.path.join(BASE, ".artifacts", "reg")
TARGET = os.path.join(DOCS, "saved-as.md")
OPEN_DOC = os.path.join(DOCS, "dialog-open.md")

ULONG_PTR = ctypes.c_size_t
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


KEYUP, UNICODE = 0x0002, 0x0004
VK_CONTROL, VK_SHIFT, VK_RETURN = 0x11, 0x10, 0x0D
VK_1, VK_A, VK_O, VK_S = 0x31, 0x41, 0x4F, 0x53

results = []


def check(name, value, expect=None):
    ok = True if expect is None else value == expect
    results.append({"step": name, "ok": ok, "got": value, "expect": expect})
    print(("  PASS " if ok else "  FAIL ") + name + " -> " + json.dumps(value, ensure_ascii=False))


def send(events):
    arr = (INPUT * len(events))()
    for i, (vk, flags) in enumerate(events):
        arr[i].type = 1
        arr[i].u.ki.wVk = vk
        arr[i].u.ki.dwFlags = flags
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def chord(mods, vk):
    ev = [(m, 0) for m in mods] + [(vk, 0), (vk, KEYUP)]
    ev += [(m, KEYUP) for m in reversed(mods)]
    send(ev)


def type_text(text):
    for ch in text:
        arr = (INPUT * 2)()
        arr[0].type = 1
        arr[0].u.ki.wScan = ord(ch)
        arr[0].u.ki.dwFlags = UNICODE
        arr[1].type = 1
        arr[1].u.ki.wScan = ord(ch)
        arr[1].u.ki.dwFlags = UNICODE | KEYUP
        user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        time.sleep(0.005)


def find_window(keyword):
    hits = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if keyword.lower() in buf.value.lower():
                hits.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return hits


def find_hwnd_for_pid(pid):
    """按 PID 找主窗口：同名进程可能有多个残留，按标题找会打错窗口。"""
    hits = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
        if owner.value == pid:
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if "pyMD" in buf.value:
                hits.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else None


def windows_snapshot():
    """当前可见窗口 -> (标题, 类名)，用「新出现的窗口」判断对话框是否弹出，比猜标题可靠。"""
    seen = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls, 256)
            seen[h] = (buf.value, cls.value)
        return True

    user32.EnumWindows(cb, 0)
    return seen


def wait_new_window(before, timeout=6.0):
    """等一个「之前不存在」的可见窗口，返回它的 (标题, 类名) 列表。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fresh = {h: v for h, v in windows_snapshot().items() if h not in before}
        if fresh:
            return fresh
        time.sleep(0.25)
    return {}


def open_dialog(hwnd, mods, vk, label, attempts=3):
    """发快捷键开原生对话框，没弹出来就重试（键盘事件偶发丢失）。"""
    for i in range(attempts):
        before = windows_snapshot()
        force_foreground(hwnd)
        chord(mods, vk)
        fresh = wait_new_window(before, 5)
        if fresh:
            return fresh
        print("  %s 第 %d 次没弹出对话框，重试" % (label, i + 1))
    return {}


def force_foreground(h):
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_me = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(tid_me, tid_fg, True)
    user32.ShowWindow(h, 9)
    user32.SetForegroundWindow(h)
    user32.BringWindowToTop(h)
    user32.SetFocus(h)
    user32.AttachThreadInput(tid_me, tid_fg, False)
    time.sleep(0.5)
    return user32.GetForegroundWindow() == h


def tabs(cdp):
    return json.loads(
        cdp.eval(
            """JSON.stringify([...document.querySelectorAll('.doc-tab')].map(t => ({
                 name: t.querySelector('.doc-tab-name').textContent,
                 active: t.getAttribute('aria-selected') === 'true',
               })))"""
        )
    )


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(DOCS, exist_ok=True)
    for path in (TARGET, OPEN_DOC):
        if os.path.exists(path):
            os.remove(path)
    with io.open(OPEN_DOC, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 对话框打开的文件\n\n来自 Ctrl+O。\n")

    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--remote-debugging-port=%d --remote-allow-origins=*" % PORT
    )
    proc = subprocess.Popen(
        [EXE], env=env, creationflags=0x00000200,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
    time.sleep(1.4)

    # 只留一个空白草稿
    cdp.eval(
        """(() => {
             localStorage.setItem('pymd-tabs', JSON.stringify({paths: [], activePath: null}));
             localStorage.setItem('pymd-view', 'edit');
             location.reload();
             return true;
           })()"""
    )
    time.sleep(2.2)
    cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
    time.sleep(1.0)

    hwnd = find_hwnd_for_pid(proc.pid)
    check("找到应用窗口", hwnd is not None, True)
    check("前台化成功", force_foreground(hwnd), True)

    # 输入正文
    chord([VK_CONTROL], VK_1)
    time.sleep(0.7)
    type_text("# 另存为回归\n\nCtrl+Shift+S 写入的内容。\n")
    time.sleep(0.8)
    check("正文已输入", cdp.eval("document.querySelector('.cm-content').textContent.includes('另存为回归')"), True)

    # ---------- 1. 另存为 ----------
    new_wins = open_dialog(hwnd, [VK_CONTROL, VK_SHIFT], VK_S, "另存为")
    check("另存为对话框已弹出", len(new_wins) >= 1, True)
    check("对话框窗口", list(new_wins.values()))
    check("对话框里含 pyMD 主窗口", any("pyMD" in v[0] for v in windows_snapshot().values()), True)
    try:
        from PIL import ImageGrab
        ImageGrab.grab().save(os.path.join(OUT, "20-saveas-dialog.png"))
    except Exception as exc:
        print("  截图跳过:", exc)

    # 文件名框里可能已经预填了内容，先全选再输入，避免拼出一个不存在的路径
    chord([VK_CONTROL], VK_A)
    time.sleep(0.2)
    type_text(TARGET)
    time.sleep(0.7)
    send([(VK_RETURN, 0), (VK_RETURN, KEYUP)])

    # 对话框关闭 + 异步写盘，固定 sleep 会抢跑，改成轮询
    deadline = time.time() + 12
    while time.time() < deadline and not os.path.exists(TARGET):
        time.sleep(0.3)

    check("另存为文件已落盘", os.path.exists(TARGET), True)
    if os.path.exists(TARGET):
        content = io.open(TARGET, encoding="utf-8").read()
        check("落盘内容正确", "另存为回归" in content, True)
        check("落盘字节数", os.path.getsize(TARGET) > 0, True)

    deadline = time.time() + 8
    while time.time() < deadline:
        names = [t["name"] for t in tabs(cdp)]
        if names == ["saved-as.md"]:
            break
        time.sleep(0.3)

    after = tabs(cdp)
    check("标签改名为新文件", [t["name"] for t in after], ["saved-as.md"])
    check("另存为后无脏标记", cdp.eval("document.getElementById('status-dirty').textContent"), "")
    check("状态栏路径指向新文件",
          cdp.eval("document.getElementById('status-path').textContent").replace("/", "\\").lower(),
          TARGET.replace("/", "\\").lower())

    try:
        from PIL import ImageGrab
        ImageGrab.grab().save(os.path.join(OUT, "21-saveas-done.png"))
    except Exception:
        pass

    # ---------- 2. 打开 ----------
    new_wins = open_dialog(hwnd, [VK_CONTROL], VK_O, "打开")
    check("打开对话框已弹出", len(new_wins) >= 1, True)
    chord([VK_CONTROL], VK_A)
    time.sleep(0.2)
    type_text(OPEN_DOC)
    time.sleep(0.7)
    send([(VK_RETURN, 0), (VK_RETURN, KEYUP)])

    deadline = time.time() + 12
    while time.time() < deadline:
        names = [t["name"] for t in tabs(cdp)]
        if "dialog-open.md" in names:
            break
        time.sleep(0.3)

    after = tabs(cdp)
    check("Ctrl+O 打开后新增标签", [t["name"] for t in after], ["saved-as.md", "dialog-open.md"])
    check("新标签成为活动标签", [t["active"] for t in after], [False, True])
    check("打开的内容正确",
          cdp.eval("document.querySelector('.cm-content').textContent.includes('对话框打开的文件')"), True)

    try:
        from PIL import ImageGrab
        ImageGrab.grab().save(os.path.join(OUT, "22-open-done.png"))
    except Exception:
        pass

    failed = [r for r in results if not r["ok"]]
    print("\n汇总: %d 项，通过 %d，失败 %d" % (len(results), len(results) - len(failed), len(failed)))
    for r in failed:
        print("  FAIL:", r["step"], "got=", json.dumps(r["got"], ensure_ascii=False),
              "expect=", json.dumps(r["expect"], ensure_ascii=False))

    with open(os.path.join(OUT, "result_dialog.json"), "w", encoding="utf-8") as fh:
        json.dump({"total": len(results), "failed": len(failed), "results": results},
                  fh, ensure_ascii=False, indent=1)
    try:
        proc.kill()
    except Exception:
        pass
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
