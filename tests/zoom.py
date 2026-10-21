"""页面缩放：整页（工具栏 / 侧边栏 / 状态栏）一起放大缩小。

和「字号缩放」是两套，这个脚本专门分开验：
  - 字体缩放只改 `--fs`，页面倍率不动
  - 页面缩放走 WebView 渲染层（`set_webview_zoom`），DOM 的 px 不变、视口 CSS px 变少

**断言不能只看状态栏文字**：状态栏是前端自己写的，就算 Rust 那边 invoke 失败它也会
显示「110%」。真正的证据是 `window.innerWidth` —— 100% 时是 W，110% 时应该约 W/1.1。
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, wait_targets  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9267
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".artifacts", "zoom")
DOCS = r"D:\tmp\pymd_zoomdoc"

results = []


def check(name, value, expect=None):
    ok = True if expect is None else value == expect
    results.append({"step": name, "ok": ok, "got": value, "expect": expect})
    print(("  PASS " if ok else "  FAIL ") + name + " -> " + json.dumps(value, ensure_ascii=False))
    return ok


def js(cdp, code, wait=False):
    return cdp.eval(code, await_promise=wait)


def wait_for(cdp, expr, timeout=20, label=""):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            if js(cdp, expr):
                return True
        except Exception as err:
            last = err
        time.sleep(0.2)
    raise RuntimeError("等待超时: %s <- %s (%s)" % (label, expr, last))


def click_menu_item(cdp, menu_label, item_label):
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


def menu_items(cdp, label):
    raw = js(
        cdp,
        """(() => {
             const wrap = [...document.querySelectorAll('#menubar .dropdown')]
               .find(d => d.querySelector('.dropdown-trigger').textContent === %s);
             if (!wrap) return '[]';
             return JSON.stringify([...wrap.querySelectorAll('.dropdown-item')].map(b => b.children[0].textContent));
           })()""" % json.dumps(label),
    )
    return json.loads(raw)


def chip_text(cdp):
    return js(cdp, "document.getElementById('status-zoom').textContent")


def wait_chip(cdp, expect, timeout=12):
    """等状态栏倍率变成期望值 —— invoke 是异步的，点完不能立刻断言。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if chip_text(cdp) == expect:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def inner_width(cdp):
    return js(cdp, "window.innerWidth")


def make_doc():
    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "zoom.md")
    body = "# 页面缩放验证\n\n正文用来确认放大之后排版仍然正常。\n\n| 项 | 值 |\n| --- | --- |\n| 倍率 | 110% |\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return path


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
        time.sleep(2.5)
        cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
        wait_for(cdp, "document.querySelectorAll('#preview .katex, #preview p').length >= 1", 25, "预览就位")
        time.sleep(0.6)

        # ---------- 1. 默认 100% ----------
        print("\n== 1. 默认 100%，状态栏不显示倍率")
        base_w = inner_width(cdp)
        check("初始状态栏倍率是 100%", chip_text(cdp), "100%")
        check("100% 时状态栏不露出倍率", js(cdp, "document.getElementById('status-zoom').hidden"), True)
        check("视口宽度可用作基准", base_w > 400, True)

        items = menu_items(cdp, "查看")
        check("查看菜单新增放大页面", "放大页面" in items, True)
        check("查看菜单新增缩小页面", "缩小页面" in items, True)
        check("查看菜单新增重置页面缩放", "重置页面缩放" in items, True)
        check("字体缩放的菜单项没被动过", "放大字体" in items and "重置字体大小" in items, True)

        # ---------- 2. 菜单放大 ----------
        print("\n== 2. 菜单「放大页面」：视口 CSS px 真的变少")
        check("点菜单项", click_menu_item(cdp, "查看", "放大页面"), "ok")
        check("状态栏显示 110%", wait_chip(cdp, "110%"), True)
        time.sleep(0.4)
        w110 = inner_width(cdp)
        print("  innerWidth: %d -> %d（期望约 %.0f）" % (base_w, w110, base_w / 1.1))
        check("视口宽度按 1.1 倍收缩", abs(w110 - base_w / 1.1) <= 4, True)
        check("倍率写进 localStorage", js(cdp, "localStorage.getItem('pymd-zoom')"), "1.1")
        check("状态栏倍率不再隐藏", js(cdp, "document.getElementById('status-zoom').hidden"), False)

        # DOM 里的 px 不该变 —— 变的是渲染比例，不是布局宽度
        toolbar_h = js(cdp, "document.getElementById('toolbar').getBoundingClientRect().height")
        check("工具栏 CSS 高度仍是 40（缩放的是渲染层）", round(toolbar_h), 40)

        # ---------- 3. 快捷键 ----------
        print("\n== 3. Ctrl+Shift+= 再放大一档")
        js(
            cdp,
            """window.dispatchEvent(new KeyboardEvent('keydown', {
                 key: '+', code: 'Equal', ctrlKey: true, shiftKey: true,
                 bubbles: true, cancelable: true
               })); true""",
        )
        check("倍率到 120%", wait_chip(cdp, "120%"), True)

        print("\n== 4. Ctrl+滚轮（向上）")
        js(
            cdp,
            """window.dispatchEvent(new WheelEvent('wheel', {
                 deltaY: -100, ctrlKey: true, bubbles: true, cancelable: true
               })); true""",
        )
        check("倍率到 130%", wait_chip(cdp, "130%"), True)

        # ---------- 5. 两套缩放互不干扰 ----------
        print("\n== 5. Ctrl+= 只放大字体，页面倍率不动")
        fs_before = js(cdp, "getComputedStyle(document.documentElement).getPropertyValue('--fs').trim()")
        zoom_before = chip_text(cdp)
        js(
            cdp,
            """window.dispatchEvent(new KeyboardEvent('keydown', {
                 key: '=', code: 'Equal', ctrlKey: true,
                 bubbles: true, cancelable: true
               })); true""",
        )
        time.sleep(0.6)
        fs_after = js(cdp, "getComputedStyle(document.documentElement).getPropertyValue('--fs').trim()")
        check("字号 +1px", (fs_before, fs_after), ("14px", "15px"))
        check("页面倍率没变", chip_text(cdp), zoom_before)

        # ---------- 6. 上下限 ----------
        print("\n== 6. 夹紧到 50%–300%")
        for _ in range(18):
            click_menu_item(cdp, "查看", "放大页面")
            time.sleep(0.08)
        check("顶到 300%", wait_chip(cdp, "300%"), True)
        click_menu_item(cdp, "查看", "放大页面")
        time.sleep(0.5)
        check("300% 之后再点还是 300%", chip_text(cdp), "300%")

        for _ in range(28):
            click_menu_item(cdp, "查看", "缩小页面")
            time.sleep(0.08)
        check("缩到 50%", wait_chip(cdp, "50%"), True)
        click_menu_item(cdp, "查看", "缩小页面")
        time.sleep(0.5)
        check("50% 之后再点还是 50%", chip_text(cdp), "50%")

        # ---------- 7. 状态栏一点就回 100% ----------
        print("\n== 7. 点状态栏倍率回 100%")
        js(cdp, "document.getElementById('status-zoom').click(); true")
        check("回到 100%", wait_chip(cdp, "100%"), True)
        time.sleep(0.5)
        check("视口宽度复原", abs(inner_width(cdp) - base_w) <= 2, True)
        check("倍率从状态栏收起", js(cdp, "document.getElementById('status-zoom').hidden"), True)
        check("localStorage 记 1", js(cdp, "localStorage.getItem('pymd-zoom')"), "1")

        # ---------- 8. 重启保留 ----------
        print("\n== 8. 重启后倍率还在")
        js(cdp, "localStorage.setItem('pymd-zoom', '1.3'); location.reload(); true")
        time.sleep(2.5)
        cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
        wait_for(cdp, "document.getElementById('status-zoom') && true", 20, "界面就绪")
        check("重载后显示 130%", wait_chip(cdp, "130%"), True)
        time.sleep(0.6)
        print("  重载后 innerWidth: %d（100%% 时 %d）" % (inner_width(cdp), base_w))
        check("重载后视口确实是缩放态", abs(inner_width(cdp) - base_w / 1.3) <= 4, True)

        # 截图留证（缩放态下整页都变大）
        try:
            cdp.call("Page.enable")
            cdp.shot(os.path.join(OUT, "zoom-130.png"))
        except Exception as err:
            print("  截图跳过:", err)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        time.sleep(0.5)
        subprocess.run(["taskkill", "/IM", "pymd.exe", "/F"], capture_output=True)

    passed = sum(1 for r in results if r["ok"])
    print("\n汇总: %d 项，通过 %d，失败 %d" % (len(results), passed, len(results) - passed))
    for r in results:
        if not r["ok"]:
            print("  FAIL: %s got=%r expect=%r" % (r["step"], r["got"], r["expect"]))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
