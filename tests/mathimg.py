"""公式转图：勾选后预览里的公式变成 PNG 图片。

为什么需要这功能：KaTeX 排出来的是「HTML + 自带字体」，黏到 Word / 微信里字体一丢就散架。
这里验的不是「好看」，是**粘出去还能看**：
  - 预览里真的换成了 <img src="data:image/png;base64,…">
  - 图不是空白：不透明像素占比够，且墨色是深的（粘白纸文档能看清）
  - 位图是 3 倍像素，CSS 尺寸按原尺寸给
  - 关掉能退回矢量渲染，勾选状态记在 localStorage、重启还在
  - 选中预览内容复制，剪贴板 HTML 里带的是 data URI 图片（这才是「粘到别处」的实锤）
"""
import ctypes
import io
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, wait_targets  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9266
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".artifacts", "mathimg")
DOCS = r"D:\tmp\pymd_mathdoc"

CF_UNICODETEXT = 13

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 这几个 restype/argtypes 不声明就会踩坑：64 位句柄默认被当成 int32 截断，读出来全是空
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t

results = []


def check(name, value, expect=None):
    ok = True if expect is None else value == expect
    results.append({"step": name, "ok": ok, "got": value, "expect": expect})
    print(("  PASS " if ok else "  FAIL ") + name + " -> " + json.dumps(value, ensure_ascii=False))
    return ok


def js(cdp, code, wait=False):
    return cdp.eval(code, await_promise=wait)


def wait_for(cdp, expr, timeout=25, label=""):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            if js(cdp, expr):
                return True
        except Exception as err:  # 页面正在重载时会拿不到上下文
            last = err
        time.sleep(0.25)
    raise RuntimeError("等待超时: %s <- %s (%s)" % (label, expr, last))


def clipboard_text():
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_html():
    """读 CF_HTML（Windows 的「HTML 格式」剪贴板数据，带一段文本头）。"""
    fmt = user32.RegisterClipboardFormatW("HTML Format")
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            raw = ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)
        # 头是 ASCII 的 Version/StartHTML/…，正文是 UTF-8；统一按 UTF-8 解，替换掉非法字节
        return raw.decode("utf-8", "replace")
    finally:
        user32.CloseClipboard()


def make_doc():
    if os.path.isdir(DOCS):
        for name in os.listdir(DOCS):
            try:
                os.remove(os.path.join(DOCS, name))
            except OSError:
                pass
    os.makedirs(DOCS, exist_ok=True)

    body = r"""# 公式转图验证

行内公式 $a^2 + b^2 = c^2$ 夹在文字中间，用来验基线对齐。

$$
\int_0^1 x^2 \, dx = \frac{1}{3}
$$

化学式 $\ce{H2SO4}$ 走 mhchem，和 KaTeX 那边保持一致。

```math
\sum_{i=1}^{n} i = \frac{n(n+1)}{2}
```

行内第二个公式 $\alpha + \beta$ 用来验多张图的替换顺序。
"""
    path = os.path.join(DOCS, "math.md")
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return path


SAMPLE_JS = """(async () => {
  const img = document.querySelector('#preview img.math-img');
  if (!img) return JSON.stringify({found: false});
  if (!img.complete) { try { await img.decode(); } catch (e) {} }
  const c = document.createElement('canvas');
  c.width = img.naturalWidth;
  c.height = img.naturalHeight;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const data = ctx.getImageData(0, 0, c.width, c.height).data;
  let opaque = 0, lum = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] > 40) {
      opaque++;
      lum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
    }
  }
  const total = data.length / 4;
  return JSON.stringify({
    found: true,
    naturalW: img.naturalWidth, naturalH: img.naturalHeight,
    cssW: img.width, cssH: img.height,
    opaqueRatio: opaque / total,
    meanLum: opaque ? lum / opaque : null,
    srcHead: img.src.slice(0, 22),
  });
})()"""


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
                 localStorage.setItem('pymd-view', 'preview');
                 return 1;
               })()""" % json.dumps(session),
        )
        js(cdp, "location.reload(); true")
        time.sleep(2.5)
        cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
        wait_for(cdp, "document.querySelectorAll('#preview .katex').length >= 3", 25, "KaTeX 就位")
        time.sleep(0.5)

        # ---------- 1. 默认是矢量 ----------
        print("\n== 1. 默认关：KaTeX 矢量渲染，没有图片")
        katex_n = js(cdp, "document.querySelectorAll('#preview .katex').length")
        img_n = js(cdp, "document.querySelectorAll('#preview img.math-img').length")
        check("公式条数 ≥ 3", katex_n >= 3, True)
        check("默认没有公式图片", img_n, 0)
        check("勾选框默认不勾", js(cdp, "document.getElementById('chk-mathimg').checked"), False)
        where = json.loads(
            js(
                cdp,
                """(() => {
                     const box = document.getElementById('chk-mathimg');
                     const hard = document.getElementById('chk-hardbreaks');
                     const nav = document.getElementById('tabbar');
                     const rb = box.getBoundingClientRect(), rh = hard.getBoundingClientRect();
                     const rn = nav.getBoundingClientRect();
                     return JSON.stringify({
                       sameRow: Math.abs((rb.top + rb.bottom) / 2 - (rn.top + rn.bottom) / 2) < 6,
                       rightOfHard: rb.left > rh.right,
                       label: box.closest('label').textContent.trim(),
                     });
                   })()""",
            )
        )
        check("勾选框在工具栏那一行", where["sameRow"], True)
        check("勾选框在硬换行右边", where["rightOfHard"], True)
        check("勾选框文案", where["label"], "公式转图")

        # ---------- 2. 打开转图 ----------
        print("\n== 2. 勾上「公式转图」")
        js(cdp, "document.getElementById('chk-mathimg').click(); true")
        wait_for(cdp, "document.querySelectorAll('#preview img.math-img').length >= 5", 30, "公式变图片")
        time.sleep(0.4)

        img_n = js(cdp, "document.querySelectorAll('#preview img.math-img').length")
        katex_left = js(
            cdp,
            "document.querySelectorAll('#preview .math-inline .katex, #preview .math-display .katex').length",
        )
        check("5 个公式全变成图片（行内 2 + 独立 1 + 化学 1 + ```math 1）", img_n, 5)
        check("原来包着 KaTeX 的容器已被替换", katex_left, 0)
        check("勾选框状态同步", js(cdp, "document.getElementById('chk-mathimg').checked"), True)
        check("localStorage 记下了", js(cdp, "localStorage.getItem('pymd-mathimg')"), "1")
        check(
            "每张都是 PNG data URI",
            js(
                cdp,
                """[...document.querySelectorAll('#preview img.math-img')]
                     .every(img => img.src.startsWith('data:image/png;base64,'))""",
            ),
            True,
        )
        check(
            "alt 保留 TeX（无障碍 / 复制纯文本时还能认出来）",
            js(
                cdp,
                """[...document.querySelectorAll('#preview img.math-img')]
                     .every(img => img.alt && img.alt.length > 0 && !img.alt.startsWith('data:'))""",
            ),
            True,
        )

        # ---------- 3. 图片内容 ----------
        print("\n== 3. 图片不是空白、墨色够深")
        sample = json.loads(js(cdp, SAMPLE_JS, wait=True))
        print("  sample:", json.dumps(sample, ensure_ascii=False))
        check("第一张图能解码", sample.get("found"), True)
        check("位图有像素", (sample.get("naturalW") or 0) > 10 and (sample.get("naturalH") or 0) > 10, True)
        # 3 倍像素：位图宽度应是 CSS 宽度的 2.5~3.5 倍（向上取整会带来零头）
        ratio = (sample.get("naturalW") or 0) / max(1, sample.get("cssW") or 1)
        check("位图是 3 倍像素", 2.5 <= ratio <= 3.5, True)
        check("不透明像素占比 > 3%（不是空白图）", (sample.get("opaqueRatio") or 0) > 0.03, True)
        check("墨色是深色（粘白纸能看清）", (sample.get("meanLum") or 255) < 110, True)
        check("透明底（四角 alpha 为 0）", js(
            cdp,
            """(async () => {
                 const img = document.querySelector('#preview img.math-img');
                 if (!img.complete) { try { await img.decode(); } catch (e) {} }
                 const c = document.createElement('canvas');
                 c.width = img.naturalWidth; c.height = img.naturalHeight;
                 const ctx = c.getContext('2d');
                 ctx.drawImage(img, 0, 0);
                 const d = ctx.getImageData(0, 0, c.width, c.height).data;
                 const at = (x, y) => d[(y * c.width + x) * 4 + 3];
                 return at(0, 0) === 0 && at(c.width - 1, c.height - 1) === 0;
               })()""",
            wait=True,
        ), True)

        # ---------- 4. 排版 ----------
        print("\n== 4. 行内 / 块级排版")
        check(
            "行内图片是 inline-block",
            js(cdp, "getComputedStyle(document.querySelector('#preview img.math-inline')).display"),
            "inline-block",
        )
        check(
            "行内图片按基线校准（vertical-align 有值）",
            bool(js(cdp, "document.querySelector('#preview img.math-inline').style.verticalAlign")),
            True,
        )
        layout = json.loads(
            js(
                cdp,
                """(() => {
                     const img = document.querySelector('#preview img.math-display');
                     const s = getComputedStyle(img);
                     const box = img.parentElement.getBoundingClientRect();
                     const r = img.getBoundingClientRect();
                     const leftGap = r.left - box.left, rightGap = box.right - r.right;
                     return JSON.stringify({
                       display: s.display,
                       centered: Math.abs(leftGap - rightGap) < 6,
                     });
                   })()""",
            )
        )
        check("块级图片是 block", layout["display"], "block")
        check("块级图片居中", layout["centered"], True)

        # ---------- 5. 深色主题垫白 ----------
        print("\n== 5. 深色主题下图片垫白（深字落在深底上看不见）")
        js(cdp, "document.documentElement.dataset.theme = 'dark'; true")
        time.sleep(0.4)
        shot = json.loads(
            js(
                cdp,
                """(() => {
                     const img = document.querySelector('#preview img.math-img');
                     const s = getComputedStyle(img);
                     return JSON.stringify({shadow: s.boxShadow, bg: s.backgroundColor});
                   })()""",
            )
        )
        print("  dark:", json.dumps(shot, ensure_ascii=False))
        check("深色主题给图片加白底", shot["bg"].startswith("rgb(255, 255, 255)"), True)
        check("白底带外扩描边", shot["shadow"] not in ("none", ""), True)
        js(cdp, "document.documentElement.dataset.theme = 'light'; true")
        time.sleep(0.3)

        # ---------- 6. 复制出去带的是图 ----------
        print("\n== 6. 选中预览内容复制，剪贴板 HTML 里是 data URI 图片")
        js(
            cdp,
            """(() => {
                 const range = document.createRange();
                 range.selectNodeContents(document.getElementById('preview'));
                 const sel = getSelection();
                 sel.removeAllRanges();
                 sel.addRange(range);
                 return document.execCommand('copy');
               })()""",
        )
        time.sleep(0.8)
        html = clipboard_html()
        text = clipboard_text()
        check("剪贴板拿到 HTML 格式", bool(html), True)
        if html:
            check("HTML 里带 PNG data URI", "data:image/png;base64," in html, True)
            check("不是 KaTeX 的 DOM（没有 class=\"katex\"）", 'class="katex"' in html, False)
        check("纯文本回退里公式是 alt 文字（不是一串 base64）", bool(text) and "base64" not in (text or ""), True)

        # ---------- 7. 关掉退回矢量 ----------
        print("\n== 7. 取消勾选退回矢量渲染")
        js(cdp, "document.getElementById('chk-mathimg').click(); true")
        wait_for(cdp, "document.querySelectorAll('#preview img.math-img').length === 0", 20, "图片清空")
        time.sleep(0.5)
        check("图片全部撤掉", js(cdp, "document.querySelectorAll('#preview img.math-img').length"), 0)
        check("KaTeX 回到原位", js(cdp, "document.querySelectorAll('#preview .katex').length") >= 3, True)
        check("localStorage 记为关", js(cdp, "localStorage.getItem('pymd-mathimg')"), "0")

        # ---------- 8. 重启后记得勾选 ----------
        print("\n== 8. 重启后勾选状态还在")
        js(cdp, "localStorage.setItem('pymd-mathimg', '1'); location.reload(); true")
        time.sleep(2.5)
        cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
        wait_for(cdp, "document.querySelectorAll('#preview img.math-img').length >= 5", 30, "重载后自动转图")
        check("重载后直接就是图片", js(cdp, "document.querySelectorAll('#preview img.math-img').length"), 5)
        check("勾选框勾上", js(cdp, "document.getElementById('chk-mathimg').checked"), True)

        try:
            cdp.call("Page.enable")
            cdp.shot(os.path.join(OUT, "mathimg-light.png"))
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
