"""pyMD 窗口集成回归：拖动 / 边缘缩放 / 最小化 / 最大化 / 关闭。

这些动作走的是 tao 的原生窗口处理，CDP 只能点到按钮，尺寸变化必须用 Win32 读窗口矩形，
所以这里用 SendInput + GetWindowRect/IsIconic/IsZoomed 校验。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, wait_targets  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9234
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

results = []


def check(name, value, expect=None):
    ok = True if expect is None else value == expect
    results.append({"step": name, "ok": ok, "got": value, "expect": expect})
    print(("  PASS " if ok else "  FAIL ") + name + " -> " + json.dumps(value, ensure_ascii=False))


def find_hwnd(keyword="pyMD"):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if keyword in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def focus(hwnd):
    user32.ShowWindow(hwnd, 9)
    time.sleep(0.2)
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_me = kernel32.GetCurrentThreadId()
    attached = user32.AttachThreadInput(tid_me, tid_fg, True)
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    time.sleep(0.4)
    return user32.GetForegroundWindow() == hwnd


def rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def size(hwnd):
    l, t, r, b = rect(hwnd)
    return (r - l, b - t)


def drag(x0, y0, dx, dy, steps=12):
    user32.SetCursorPos(x0, y0)
    time.sleep(0.15)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.12)
    for i in range(1, steps + 1):
        user32.SetCursorPos(x0 + dx * i // steps, y0 + dy * i // steps)
        time.sleep(0.03)
    time.sleep(0.1)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.5)


def title_of(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def main():
    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--remote-debugging-port=%d --remote-allow-origins=*" % PORT
    )
    proc = subprocess.Popen(
        [EXE],
        env=env,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
    time.sleep(1.4)

    # 清掉会话，保证只有一个空白草稿（关闭时不会弹确认）
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

    hwnd = find_hwnd()
    check("找到窗口", hwnd is not None, True)
    check("前台化成功", focus(hwnd), True)
    check("窗口标题与界面一致", title_of(hwnd), "未命名 - pyMD")
    check("窗口按钮 aria-label", json.loads(
        cdp.eval("JSON.stringify(['win-min','win-max','win-close'].map(i => document.getElementById(i).getAttribute('aria-label')))")
    ), ["最小化", "最大化", "关闭"])

    # ---------- 1. 标题栏拖动 ----------
    before = rect(hwnd)
    drag(before[0] + 700, before[1] + 16, 120, 72)
    after = rect(hwnd)
    moved = (after[0] - before[0], after[1] - before[1])
    check("标题栏拖动位移", moved, (120, 72))
    user32.SetWindowPos(hwnd, 0, before[0], before[1], 0, 0, 0x0001 | 0x0004)
    time.sleep(0.4)

    # ---------- 2. 右边缘缩放 ----------
    w0, h0 = size(hwnd)
    l, t, r, b = rect(hwnd)
    drag(r - 2, t + h0 // 2, 160, 0)
    w1, h1 = size(hwnd)
    check("右边缘加宽", w1 - w0 >= 140, True)
    check("右边缘加宽数值", w1 - w0)
    check("高度不变", h1, h0)

    # ---------- 3. 下边缘缩放 ----------
    l, t, r, b = rect(hwnd)
    drag(l + w1 // 2, b - 2, 0, 90)
    w2, h2 = size(hwnd)
    check("下边缘加高", h2 - h1 >= 70, True)
    check("下边缘加高数值", h2 - h1)
    check("宽度不变", w2, w1)

    # 缩回原尺寸，方便后续判定最大化
    user32.SetWindowPos(hwnd, 0, l, t, w0, h0, 0x0002 | 0x0004)
    time.sleep(0.5)

    # ---------- 4. 最小化 / 还原 ----------
    cdp.eval("document.getElementById('win-min').click(); true")
    time.sleep(0.9)
    check("点击最小化后窗口最小化", bool(user32.IsIconic(hwnd)), True)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(0.8)
    check("还原后不再最小化", bool(user32.IsIconic(hwnd)), False)

    # ---------- 5. 最大化 / 还原 ----------
    focus(hwnd)
    cdp.eval("document.getElementById('win-max').click(); true")
    time.sleep(1.0)
    check("点击最大化后窗口最大化", bool(user32.IsZoomed(hwnd)), True)
    check("按钮图标切到还原", cdp.eval("document.getElementById('win-max').getAttribute('aria-label')"), "还原")
    scr_w = user32.GetSystemMetrics(0)
    l, t, r, b = rect(hwnd)
    check("最大化后覆盖屏幕宽度", (r - l) >= scr_w - 4, True)
    check("最大化尺寸", [r - l, b - t])

    cdp.eval("document.getElementById('win-max').click(); true")
    time.sleep(1.0)
    check("再点一次还原", bool(user32.IsZoomed(hwnd)), False)
    check("按钮图标切回最大化", cdp.eval("document.getElementById('win-max').getAttribute('aria-label')"), "最大化")

    # ---------- 6. 关闭按钮 ----------
    focus(hwnd)
    cdp.eval("document.getElementById('win-close').click(); true")
    time.sleep(2.0)
    check("关闭按钮退出进程", proc.poll() is not None, True)

    failed = [r for r in results if not r["ok"]]
    print("\n汇总: %d 项，通过 %d，失败 %d" % (len(results), len(results) - len(failed), len(failed)))
    for r in failed:
        print("  FAIL:", r["step"], "got=", json.dumps(r["got"], ensure_ascii=False),
              "expect=", json.dumps(r["expect"], ensure_ascii=False))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".artifacts", "reg")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result_win.json"), "w", encoding="utf-8") as fh:
        json.dump({"total": len(results), "failed": len(failed), "results": results},
                  fh, ensure_ascii=False, indent=1)
    try:
        proc.kill()
    except Exception:
        pass
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
