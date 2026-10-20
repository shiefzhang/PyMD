"""验证图标链路：源文件尺寸 / ico 多档 / exe 内嵌图标已换新 / 关于页 logo 正常显示。

关键点：exe 的图标来自 tauri-build 把 icons/icon.ico 编进资源段，
所以「拷到别的电脑」时任务栏、Alt-Tab、文件属性里看到的都是它。
这里用 ExtractIconExW 把新 exe 的图标抠出来，与 icons/ 里的 32px 图比一次，
再抽查右下角色调，确认资源段里嵌的确实是新图标（而不是 build.rs 没重跑、留着旧的）。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cdp  # noqa: E402
from PIL import Image  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
ICONS = os.path.abspath(os.path.join(HERE, "..", "src-tauri", "icons"))
PREV = os.path.join(ICONS, "_prev")
OUT = os.path.join(HERE, ".artifacts", "icons")
PORT = 9357
os.makedirs(OUT, exist_ok=True)

u32 = ctypes.WinDLL("user32", use_last_error=True)
gdi = ctypes.WinDLL("gdi32", use_last_error=True)
shell = ctypes.WinDLL("shell32", use_last_error=True)  # ExtractIconEx 在 shell32，不在 user32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def hicon_to_image(hicon, size):
    """把 HICON 画进 32 位 DIB 再读回来，保留 alpha。"""
    hdc = gdi.CreateCompatibleDC(None)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = size
    bmi.biHeight = -size  # 负数 = 自上而下
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    bits = ctypes.c_void_p()
    hbmp = gdi.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi.SelectObject(hdc, hbmp)
    ctypes.memmove(bits, b"\x00" * (size * size * 4), size * size * 4)
    u32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 3)  # DI_NORMAL
    buf = ctypes.string_at(bits, size * size * 4)
    gdi.SelectObject(hdc, old)
    gdi.DeleteObject(hbmp)
    gdi.DeleteDC(hdc)
    return Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1)


def extract_exe_icons(path):
    """返回 [(size, PIL.Image)]，取 exe 里的 large(32) 与 small(16)。"""
    out = []
    large, small = wintypes.HICON(), wintypes.HICON()
    shell.ExtractIconExW.restype = wintypes.UINT
    n = shell.ExtractIconExW(path, 0, ctypes.byref(large), ctypes.byref(small), 1)
    if not n:
        return out
    for hicon, size in ((large, 32), (small, 16)):
        if hicon.value:
            img = hicon_to_image(hicon, size)
            img.save(os.path.join(OUT, "exe-icon-%d.png" % size))
            out.append((size, img))
            u32.DestroyIcon(hicon)
    return out


def mae(a, b):
    """同一尺寸两图的平均绝对误差（只算任一侧不透明的像素）。"""
    a, b = a.convert("RGBA"), b.convert("RGBA")
    tot = cnt = 0
    pa, pb = a.load(), b.load()
    for y in range(a.height):
        for x in range(a.width):
            ca, cb = pa[x, y], pb[x, y]
            if ca[3] < 40 and cb[3] < 40:
                continue
            tot += abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) + abs(ca[2] - cb[2])
            cnt += 3
    return tot / cnt if cnt else 0.0


results = []


def check(name, got, want=None, ok=None):
    good = ok if ok is not None else (got == want)
    results.append((name, good, got, want))
    print(("  PASS " if good else "  FAIL ") + name + "  -> " + repr(got))
    return good


# 1) 静态图标文件
print("[1] 图标文件")
sizes = {"32x32.png": (32, 32), "64x64.png": (64, 64), "128x128.png": (128, 128),
         "128x128@2x.png": (256, 256), "icon.png": (512, 512)}
for name, want in sizes.items():
    im = Image.open(os.path.join(ICONS, name))
    check("%s 尺寸" % name, im.size, want)
    check("%s 带透明通道" % name, im.mode in ("RGBA", "LA"), True)
check("icon.png 四角透明", Image.open(os.path.join(ICONS, "icon.png")).convert("RGBA").getpixel((2, 2))[3], 0)
ico = Image.open(os.path.join(ICONS, "icon.ico"))
frames = sorted(ico.info.get("sizes", []))
check("ico 含 7 档", len(frames), 7)
check("ico 含 256/32/16", all(s in frames for s in [(256, 256), (32, 32), (16, 16)]), True)

# 2) exe 内嵌图标
print("[2] exe 内嵌图标")
found = dict(extract_exe_icons(EXE))
check("能抠出 32px 图标", 32 in found, True)
if 32 in found:
    new32 = Image.open(os.path.join(ICONS, "32x32.png")).convert("RGBA")
    d_new = mae(found[32], new32)
    print("  与新版 32px 的平均差异：%.1f" % d_new)
    check("exe 图标与新版基本一致", d_new < 22, True)
    # 手绘版是奶油纸底 + 赤陶笔刷，整体暖调：R > G > B 且红通道偏高。
    corner = found[32].getpixel((26, 26))
    print("  右下角像素：", corner)
    check("exe 图标是新版配色（暖纸底）",
          corner[0] > 170 and corner[0] > corner[1] > corner[2], True)
check("小图标为 16px", found.get(16).size if found.get(16) else None, (16, 16))

# 3) 关于页 logo
print("[3] 关于页 logo")
env = dict(os.environ)
env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=%d --remote-allow-origins=*" % PORT
proc = subprocess.Popen([EXE], env=env)
try:
    pages = cdp.wait_targets(PORT, 40)
    c = cdp.CDP(pages[0]["webSocketDebuggerUrl"])
    c.call("Page.enable")
    c.call("Runtime.enable")
    time.sleep(1.2)
    c.eval(
        "(() => { const w=[...document.querySelectorAll('#menubar .dropdown')];"
        " const help=w[w.length-1]; help.querySelector('.dropdown-trigger').click();"
        " [...help.querySelectorAll('.dropdown-item')].find(b=>b.textContent.includes('关于')).click();"
        " return true; })()"
    )
    time.sleep(0.8)
    logo = c.eval(
        "(() => { const i=document.querySelector('.about-logo');"
        " if(!i) return null; const r=i.getBoundingClientRect();"
        " return {src:i.getAttribute('src').slice(0,24), nat:[i.naturalWidth,i.naturalHeight],"
        "   box:[Math.round(r.width),Math.round(r.height)], radius:getComputedStyle(i).borderRadius}; })()"
    )
    print("  raw:", logo)
    check("logo 已插入", bool(logo), True)
    check("logo 加载成功（256px 源）", logo["nat"] if logo else None, [256, 256])
    check("logo 显示尺寸", logo["box"] if logo else None, [46, 46])
    rect = c.eval(
        "(() => { const r=document.getElementById('info-dialog').getBoundingClientRect();"
        " return {x:r.x, y:r.y, width:r.width, height:r.height}; })()"
    )
    c.shot(os.path.join(OUT, "about-logo-light.png"), clip=rect, scale=2)
    c.eval("document.documentElement.dataset.theme='dark'; true")
    time.sleep(0.4)
    c.shot(os.path.join(OUT, "about-logo-dark.png"), clip=rect, scale=2)
    c.eval("document.documentElement.dataset.theme='light'; true")
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
