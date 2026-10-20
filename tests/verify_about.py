"""验证「关于」对话框：内容、版本联动、复制邮箱、明暗两套样式、快捷键对话框回归。"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cdp  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9355
OUT = os.path.join(HERE, ".artifacts", "about")
os.makedirs(OUT, exist_ok=True)

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.OpenClipboard.restype = wintypes.BOOL
u32.GetClipboardData.argtypes = [wintypes.UINT]
u32.GetClipboardData.restype = wintypes.HANDLE
u32.CloseClipboard.restype = wintypes.BOOL
k32.GlobalLock.argtypes = [wintypes.HANDLE]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [wintypes.HANDLE]


def read_clip():
    if not u32.OpenClipboard(None):
        return None
    try:
        h = u32.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return None
        p = k32.GlobalLock(h)
        try:
            return ctypes.c_wchar_p(p).value
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def exe_copyright(path):
    """读 exe 的 LegalCopyright —— 右键属性里「版权」那一行，来自 tauri.conf.json 的 bundle.copyright"""
    v = ctypes.WinDLL("version", use_last_error=True)
    size = v.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not v.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr, ln = ctypes.c_void_p(), wintypes.UINT()
    if not v.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(ln)):
        return None
    lang = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD))[0]
    if not v.VerQueryValueW(buf, "\\StringFileInfo\\%04x04b0\\LegalCopyright" % lang,
                            ctypes.byref(ptr), ctypes.byref(ln)) or not ln.value:
        return None
    return ctypes.wstring_at(ptr.value, ln.value).rstrip("\x00")


env = dict(os.environ)
env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
    "--remote-debugging-port=%d --remote-allow-origins=*" % PORT
)
proc = subprocess.Popen([EXE], env=env)
results = []


def check(name, got, want=None, ok=None):
    good = ok if ok is not None else (got == want)
    results.append((name, good, got, want))
    print(("  PASS " if good else "  FAIL ") + name + "  -> " + repr(got))
    return good


try:
    pages = cdp.wait_targets(PORT, 40)
    c = cdp.CDP(pages[0]["webSocketDebuggerUrl"])
    c.call("Page.enable")
    c.call("Runtime.enable")
    time.sleep(1.0)

    # 1) 帮助菜单项
    items = c.eval(
        "(() => { const w=[...document.querySelectorAll('#menubar .dropdown')];"
        " const help=w[w.length-1]; help.querySelector('.dropdown-trigger').click();"
        " return [...help.querySelectorAll('.dropdown-item')].map(b=>b.textContent.trim()); })()"
    )
    check("帮助菜单项", items, ["关于 pyMD", "快捷键说明"])

    # 2) 点「关于」
    c.eval(
        "(() => { const w=[...document.querySelectorAll('#menubar .dropdown')];"
        " const help=w[w.length-1];"
        " [...help.querySelectorAll('.dropdown-item')].find(b=>b.textContent.includes('关于')).click();"
        " return true; })()"
    )
    time.sleep(0.6)

    info = c.eval(
        "(() => { const d=document.getElementById('info-dialog'); const b=document.getElementById('info-body');"
        " const q=s=>{const n=b.querySelector(s);return n?n.textContent.trim():null};"
        " return {open:d.open, title:document.getElementById('info-title').textContent,"
        "   name:q('.about-name'), ver:q('.about-ver'),"
        "   author:[...b.querySelectorAll('.about-meta dt')].map(n=>n.textContent),"
        "   authorVal:b.querySelector('.about-meta dd').textContent.trim(),"
        "   mail:q('.about-mail'),"
        "   copyright:q('.about-copyright'), dim:q('.about-dim'),"
        "   paras:b.querySelectorAll('.about-text').length,"
        "   copyBtn:q('.about-copy'),"
        "   ws:getComputedStyle(b).whiteSpace}; })()"
    )
    print("  raw:", json.dumps(info, ensure_ascii=False))
    check("对话框已打开", info["open"], True)
    check("标题", info["title"], "关于")
    check("应用名", info["name"], "pyMD")
    check("版本徽标", info["ver"], "v0.3.0")
    check("作者行标签", info["author"], ["作者", "邮箱"])
    check("作者值", info["authorVal"], "Pyrrhus")
    check("邮箱值", info["mail"], "zhangxuefeng@batonsoft.com")
    check("版权行", info["copyright"], "© 2026 Pyrrhus · 保留所有权利")
    check("出处说明", info["dim"], "界面布局与三模式切换参考了开源项目 MDPad（MIT）。")
    check("正文段落数", info["paras"], 3)
    check("复制按钮", info["copyBtn"], "复制")
    check("about 版式非 pre-wrap", info["ws"], "normal")

    # 3) 截图（明亮）
    rect = c.eval(
        "(() => { const r=document.getElementById('info-dialog').getBoundingClientRect();"
        " return {x:r.x, y:r.y, width:r.width, height:r.height}; })()"
    )
    print("  dialog rect:", rect)
    c.shot(os.path.join(OUT, "about-light.png"), clip=rect, scale=2)

    # 4) 复制邮箱
    c.eval("document.querySelector('.about-copy').click(); true")
    time.sleep(0.5)
    check("复制后按钮文案", c.eval("document.querySelector('.about-copy').textContent.trim()"), "已复制")
    clip_text = read_clip()
    check("剪贴板内容", clip_text, "zhangxuefeng@batonsoft.com")

    # 5) 暗色主题
    c.eval("document.documentElement.dataset.theme='dark'; true")
    time.sleep(0.4)
    bg = c.eval("getComputedStyle(document.getElementById('info-dialog')).backgroundColor")
    check("暗色下对话框底色", bg, "rgb(28, 27, 26)")
    c.shot(os.path.join(OUT, "about-dark.png"), clip=rect, scale=2)
    c.eval("document.documentElement.dataset.theme='light'; true")

    # 6) 快捷键对话框回归（showInfo 通路要恢复 pre-wrap）
    c.eval("document.getElementById('info-dialog').close(); true")
    time.sleep(0.2)
    c.eval(
        "(() => { const w=[...document.querySelectorAll('#menubar .dropdown')];"
        " const help=w[w.length-1];"
        " [...help.querySelectorAll('.dropdown-item')].find(b=>b.textContent.includes('快捷键')).click();"
        " return true; })()"
    )
    time.sleep(0.4)
    sc = c.eval(
        "(() => { const b=document.getElementById('info-body');"
        " return {title:document.getElementById('info-title').textContent,"
        "   ws:getComputedStyle(b).whiteSpace, text:b.textContent.split('\\n').length,"
        "   about:b.querySelectorAll('.about').length}; })()"
    )
    print("  raw:", sc)
    check("快捷键标题", sc["title"], "快捷键说明")
    check("快捷键正文恢复 pre-wrap", sc["ws"], "pre-wrap")
    check("没有残留 about 节点", sc["about"], 0)
    check("快捷键行数", sc["text"], 18)
    c.shot(os.path.join(OUT, "shortcuts.png"), scale=1)

    # 7) 菜单里不该出现单独的「版权信息」项
    dup = c.eval(
        "(() => { const w=[...document.querySelectorAll('#menubar .dropdown')];"
        " const help=w[w.length-1]; help.querySelector('.dropdown-trigger').click();"
        " return [...help.querySelectorAll('.dropdown-item')].map(b=>b.textContent.trim()); })()"
    )
    check("未新增冗余菜单项", dup, ["关于 pyMD", "快捷键说明"])

    # 8) exe 文件属性里的版权（右键 → 属性 → 详细信息）
    cr = exe_copyright(EXE)
    print("  LegalCopyright:", cr)
    check("exe 带版权字段", bool(cr) and "Pyrrhus" in cr and "2026" in cr, True)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()

passed = sum(1 for r in results if r[1])
print("\n== %d/%d 通过 ==" % (passed, len(results)))
for name, good, got, want in results:
    if not good:
        print("  !! %s  got=%r want=%r" % (name, got, want))
sys.exit(0 if passed == len(results) else 1)
