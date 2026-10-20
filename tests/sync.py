"""分屏滚动联动 + 单换行渲染。

三件事一起验：
1. comrak 输出的块带 data-sourcepos（联动的数据来源）
2. 段内单个换行渲染成 <br>（hardbreaks），不再是空格合并
3. 编辑区滚到第 N 行，预览顶部就是第 N 行那块；反过来也一样

判据用「行号」而不是像素：两侧行高完全不同，只有行号能对齐。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, wait_targets  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9255
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".artifacts", "sync")
DOCS = r"D:\tmp\pymd_syncdoc"

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


def make_doc():
    """60 行正文，每 6 行插一个标题；开头放一对「段内单换行」用来验 hardbreak。"""
    if os.path.isdir(DOCS):
        shutil.rmtree(DOCS, ignore_errors=True)
    os.makedirs(DOCS, exist_ok=True)

    lines = [
        "# 滚动同步验证",
        "",
        "软换行测试甲",
        "软换行测试乙",
        "",
    ]
    for section in range(1, 9):
        lines.append("## 第%d节" % section)
        lines.append("")
        for row in range(1, 7):
            lines.append(
                "第%d节第%d行：这是一段用来验证滚动同步的占位文字，长度足够占据一行以上，"
                "让两侧的行高差显现出来。" % (section, row)
            )
            lines.append("")
    path = os.path.join(DOCS, "sync.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


TOP_BLOCK_JS = """(() => {
  const pane = document.getElementById('preview-pane');
  const paneRect = pane.getBoundingClientRect();
  let top = null;
  for (const el of document.querySelectorAll('#preview [data-sourcepos]')) {
    const r = el.getBoundingClientRect();
    if (r.height <= 0) continue;
    if (r.bottom > paneRect.top + 1) { top = el; break; }
  }
  const m = top ? /^(\\d+):\\d+-(\\d+):\\d+$/.exec(top.dataset.sourcepos) : null;
  const api = window.__pymd;
  return JSON.stringify({
    previewTop: Math.round(pane.scrollTop),
    editorTop: api.editor.getTopLine(),
    blockLine: m ? Number(m[1]) : null,
    blockTag: top ? top.tagName.toLowerCase() : null,
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
                 localStorage.setItem('pymd-view', 'split');
                 return 1;
               })()""" % json.dumps(session),
        )
        js(cdp, "location.reload(); true")
        time.sleep(2.2)
        cdp = CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])
        wait_for(cdp, "document.querySelectorAll('#preview [data-sourcepos]').length > 10", 20, "预览块")
        time.sleep(0.6)

        # ---------- 1. 数据来源 ----------
        print("\n== 1. comrak 输出 data-sourcepos")
        n = js(cdp, "document.querySelectorAll('#preview [data-sourcepos]').length")
        check("预览里有带行号的块", n > 10, True)
        first = js(
            cdp,
            """(() => { const el = document.querySelector('#preview [data-sourcepos]');
                 return el ? el.dataset.sourcepos : null; })()""",
        )
        check("行号格式 start:col-end:col", bool(first and ":" in first and "-" in first), True)

        # ---------- 2. 单换行 ----------
        print("\n== 2. 段内单个换行渲染成 <br>")
        hard = json.loads(
            js(
                cdp,
                """(() => {
                     const p = [...document.querySelectorAll('#preview p')]
                       .find(el => el.textContent.includes('软换行测试甲'));
                     if (!p) return JSON.stringify({found: false});
                     return JSON.stringify({
                       found: true,
                       hasBr: !!p.querySelector('br'),
                       html: p.innerHTML,
                       text: p.textContent,
                     });
                   })()""",
            )
        )
        check("找到软换行段落", hard.get("found"), True)
        check("段内换行是 <br> 而不是空格", hard.get("hasBr"), True)
        # comrak 输出的是 `<br data-sourcepos="…">\n`（连 <br> 自己也带行号），
        # 只要两行之间直接接 <br> 且没被塞空格就算对
        check(
            "两行文本之间没有多余空格",
            re.search(r"软换行测试甲<br[^>]*>\s*软换行测试乙", hard.get("html") or "") is not None,
            True,
        )

        # ---------- 2b. 工具栏勾选框 ----------
        print("\n== 2b. 「硬换行」勾选框能关掉 hardbreaks")
        js_literal = """(() => {
                 const p = [...document.querySelectorAll('#preview p')]
                   .find(el => el.textContent.includes('软换行测试甲'));
                 const box = document.getElementById('chk-hardbreaks');
                 return JSON.stringify({
                   html: p ? p.innerHTML : '',
                   checked: box.checked,
                   stored: localStorage.getItem('pymd-hardbreaks'),
                 });
               })()"""

        js(cdp, "document.getElementById('chk-hardbreaks').click(); 1")
        time.sleep(0.8)
        off = json.loads(js(cdp, js_literal))
        check("取消勾选后状态是未选中", off["checked"], False)
        check("取消勾选后段内换行不再是 <br>", "<br" not in off["html"], True)
        check("取消勾选后两行按 CommonMark 合并（中间只剩空白）",
              re.search(r"软换行测试甲\s+软换行测试乙", off["html"]) is not None, True)
        check("开关写进 localStorage", off["stored"], "0")

        js(cdp, "document.getElementById('chk-hardbreaks').click(); 1")
        time.sleep(0.8)
        on = json.loads(js(cdp, js_literal))
        check("重新勾上又变回 <br>", "<br" in on["html"], True)
        check("开关状态写回 localStorage", on["stored"], "1")

        # ---------- 3. 编辑区滚动 → 预览 ----------
        print("\n== 3. 编辑区滚动，预览跟到同一行")
        js(cdp, "window.__pymd.editor.view.scrollDOM.scrollTop = 1600; true")
        time.sleep(0.5)
        a = json.loads(js(cdp, TOP_BLOCK_JS))
        check("编辑区确实滚下去了", a["editorTop"] > 10, True)
        check("预览也滚了", a["previewTop"] > 0, True)
        check(
            "预览顶部块与编辑区顶部行一致（±3 行）",
            abs((a["blockLine"] or 0) - a["editorTop"]) <= 3,
            True,
        )

        js(cdp, "window.__pymd.editor.view.scrollDOM.scrollTop = 3200; true")
        time.sleep(0.5)
        b = json.loads(js(cdp, TOP_BLOCK_JS))
        check("再往下滚，编辑区行号变大", b["editorTop"] > a["editorTop"], True)
        check("预览仍对齐", abs((b["blockLine"] or 0) - b["editorTop"]) <= 3, True)

        # ---------- 4. 预览滚动 → 编辑区 ----------
        print("\n== 4. 预览滚动，编辑区跟到同一块")
        js(cdp, "document.getElementById('preview-pane').scrollTop = 2400; true")
        time.sleep(0.5)
        c = json.loads(js(cdp, TOP_BLOCK_JS))
        check("预览滚动生效", c["previewTop"] > 0, True)
        check("编辑区跟到同一块（±3 行）", abs((c["blockLine"] or 0) - c["editorTop"]) <= 3, True)

        js(cdp, "document.getElementById('preview-pane').scrollTop = 600; true")
        time.sleep(0.5)
        d = json.loads(js(cdp, TOP_BLOCK_JS))
        check("往回滚，编辑区行号变小", d["editorTop"] < c["editorTop"], True)
        check("往回滚也对齐", abs((d["blockLine"] or 0) - d["editorTop"]) <= 3, True)

        # ---------- 5. 不互相推 ----------
        print("\n== 5. 两边不会互相推（无回环）")
        first_shot = json.loads(js(cdp, TOP_BLOCK_JS))
        time.sleep(0.8)
        second_shot = json.loads(js(cdp, TOP_BLOCK_JS))
        check("停下来之后位置稳定", second_shot, first_shot)

        # ---------- 6. 单栏模式不联动 ----------
        print("\n== 6. 纯编辑模式不该有预览滚动")
        js(cdp, "document.querySelector('#tabbar [data-view=\"edit\"]').click()")
        time.sleep(0.4)
        hidden = js(cdp, "document.getElementById('preview-pane').hidden")
        check("切到纯编辑后预览隐藏", hidden, True)

        cdp.shot(os.path.join(OUT, "after-scroll.png"), scale=1)
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    passed = sum(1 for r in results if r["ok"])
    print("\n汇总: %d 项，通过 %d，失败 %d" % (len(results), passed, len(results) - passed))
    os.makedirs(os.path.join(HERE, ".artifacts", "reg"), exist_ok=True)
    with open(os.path.join(HERE, ".artifacts", "reg", "result_sync.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
