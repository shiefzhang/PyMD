"""pyMD 完整功能回归测试（主链路）。

方式：带 --remote-debugging-port 启动真实 exe，用最小 CDP 客户端直接驱动 DOM +
读 EditorView 状态（window.__pymd，仅在 localStorage pymd-debug=1 时挂出）。

覆盖：
  启动状态 / Markdown 渲染 / 三视图 / 侧边栏 / 多标签隔离 / 查找替换 /
  编辑命令与剪贴板 / 菜单栏与右键菜单 / 字号主题 / 文档目录 / 文件监听 /
  保存落盘 / 会话恢复

原生文件对话框与窗口交互（拖动/缩放/最大化/关闭）不在这里，见 regression_win.py
和 regression_dialog.py。
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
PORT = 9233
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, ".artifacts", "reg")
DOCS = os.path.join(BASE, ".artifacts", "regdocs")

# 版本号从 package.json 取，别在测试里再写死一份（以前写死过一次，升版必漏）
with open(os.path.join(BASE, "..", "package.json"), encoding="utf-8") as _fh:
    VERSION = json.load(_fh)["version"]

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# ---------------------------------------------------------------- 测试资料

ALPHA = """# alpha 文档

段落里有 **粗体**、*斜体* 和 `行内代码`，还有 [链接](https://example.com)。

## 二级标题

- 列表一
- 列表二

| 列 A | 列 B |
| --- | --- |
| 1 | 2 |

- [x] 已完成
- [ ] 未完成

> [!NOTE]
> 提示块内容。

```python
# 代码里的井号不是标题
print("hi")
```

### 三级标题

行内公式 $E=mc^2$，化学式 $\\ce{H2SO4}$。

$$
\\int_0^1 x^2 dx
$$

> # 引用里的标题

#### 四级标题

##### 五级标题

###### 六级标题

结尾。
"""

ALPHA_TITLES = ["alpha 文档", "二级标题", "三级标题", "引用里的标题", "四级标题", "五级标题", "六级标题"]

FILLER = "这是一段用来把预览撑高的填充文字，方便验证目录点击后预览是否滚动。\n\n"
_beta = ["# beta 长文\n\n"]
for _i in range(1, 6):
    _beta.append("## 第 %d 章\n\n" % _i)
    for _j in range(10):
        _beta.append("第 %d-%d 段：%s" % (_i, _j, FILLER))
BETA = "".join(_beta)
BETA_TITLES = ["beta 长文"] + ["第 %d 章" % i for i in range(1, 6)]

GAMMA = "# gamma\n\n纯文本文件也能打开。\n"

results = []


def check(name, value, expect=None):
    ok = True if expect is None else value == expect
    results.append({"step": name, "ok": ok, "got": value, "expect": expect})
    print(("  PASS " if ok else "  FAIL ") + name + " -> " + json.dumps(value, ensure_ascii=False))
    return ok


def section(title):
    print("\n== " + title)


def js(cdp, code, await_promise=False):
    return cdp.eval(code, await_promise=await_promise)


def wait_for(cdp, expr, timeout=15, label=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if js(cdp, expr):
                return True
        except Exception as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError("等待超时: %s <- %s (%r)" % (label, expr, last))


def connect():
    page = wait_targets(PORT)[0]
    return CDP(page["webSocketDebuggerUrl"])


def reload_app(cdp, wait_expr="document.querySelectorAll('.doc-tab').length >= 1", timeout=25):
    js(cdp, "location.reload(); true")
    time.sleep(1.6)
    new = connect()
    wait_for(new, wait_expr, timeout, "reload")
    time.sleep(0.8)
    return new


def state(cdp):
    raw = js(
        cdp,
        """JSON.stringify({
             appTitle: document.getElementById('app-title').textContent,
             tabs: [...document.querySelectorAll('.doc-tab')].map(t => ({
               name: t.querySelector('.doc-tab-name').textContent,
               active: t.getAttribute('aria-selected') === 'true',
               dirty: !t.querySelector('.doc-tab-dirty').hidden,
             })),
             view: {
               editorHidden: document.getElementById('editor-pane').hidden,
               previewHidden: document.getElementById('preview-pane').hidden,
               dividerHidden: document.getElementById('divider').hidden,
             },
             dir: [...document.querySelectorAll('#dir-list .file-item')].map(b => b.querySelector('.file-name').textContent),
             dirCount: document.getElementById('dir-count').textContent,
             dirActive: [...document.querySelectorAll('#dir-list .file-item.active')].map(b => b.querySelector('.file-name').textContent),
             recent: [...document.querySelectorAll('#recent-list .file-item')].map(b => b.querySelector('.file-name').textContent),
             recentCount: document.getElementById('recent-count').textContent,
             outline: [...document.querySelectorAll('.outline-item')].map(b => b.textContent),
             outlineCount: document.getElementById('outline-count').textContent,
             outlineHidden: document.getElementById('outline-pane').hidden,
             sidebarHidden: document.getElementById('sidebar').hidden,
             statusPath: document.getElementById('status-path').textContent,
             statusDirty: document.getElementById('status-dirty').textContent,
             statusStat: document.getElementById('status-stat').textContent,
             findOpen: !document.getElementById('findbar').hidden,
             replaceHidden: document.getElementById('replace-field').hidden,
             findStatus: document.getElementById('find-status').textContent,
             theme: document.documentElement.dataset.theme,
             fs: getComputedStyle(document.documentElement).getPropertyValue('--fs').trim(),
             menus: [...document.querySelectorAll('.dropdown-trigger')].map(b => b.textContent),
             stored: {
               view: localStorage.getItem('pymd-view'),
               sidebar: localStorage.getItem('pymd-sidebar'),
               outline: localStorage.getItem('pymd-outline'),
               theme: localStorage.getItem('pymd-theme'),
               font: localStorage.getItem('pymd-font'),
               tabs: localStorage.getItem('pymd-tabs'),
             },
           })""",
    )
    return json.loads(raw)


def doc(cdp):
    return js(cdp, "window.__pymd ? window.__pymd.editor.getDoc() : null")


def set_doc(cdp, text):
    js(cdp, "window.__pymd.editor.setDoc(%s); true" % json.dumps(text))


def key(cdp, k, ctrl=True, shift=False):
    return js(
        cdp,
        """(() => {
             const t = document.querySelector('.cm-content') || document.body;
             t.dispatchEvent(new KeyboardEvent('keydown', {
               key: %s, ctrlKey: %s, shiftKey: %s, bubbles: true, cancelable: true
             }));
             return true;
           })()"""
        % (json.dumps(k), "true" if ctrl else "false", "true" if shift else "false"),
    )


def click(cdp, selector):
    return js(
        cdp,
        """(() => {
             const n = document.querySelector(%s);
             if (!n) return false;
             n.click();
             return true;
           })()""" % json.dumps(selector),
    )


def click_dir_file(cdp, name):
    return js(
        cdp,
        """(() => {
             const row = [...document.querySelectorAll('#dir-list .file-item')]
               .find(b => b.querySelector('.file-name').textContent === %s);
             if (!row) return false;
             row.click();
             return true;
           })()""" % json.dumps(name),
    )


def click_tab(cdp, index):
    return js(
        cdp,
        """(() => {
             const t = document.querySelectorAll('.doc-tab')[%d];
             if (!t) return false;
             t.dispatchEvent(new MouseEvent('mousedown', {button: 0, bubbles: true}));
             return true;
           })()""" % index,
    )


def click_outline(cdp, index):
    return js(
        cdp,
        "(() => { const n = document.querySelectorAll('.outline-item')[%d]; if (!n) return false; n.click(); return true; })()"
        % index,
    )


def open_menu(cdp, label):
    return js(
        cdp,
        """(() => {
             const t = [...document.querySelectorAll('.dropdown-trigger')]
               .find(b => b.textContent === %s);
             if (!t) return false;
             t.click();
             return true;
           })()""" % json.dumps(label),
    )


def menu_snapshot(cdp):
    return json.loads(
        js(
            cdp,
            """(() => {
                 const open = document.querySelector('.dropdown.open');
                 if (!open) return JSON.stringify(null);
                 const list = open.querySelector('.dropdown-list');
                 return JSON.stringify({
                   trigger: open.querySelector('.dropdown-trigger').textContent,
                   items: [...list.querySelectorAll('.dropdown-item')].map(b => ({
                     label: b.querySelector('span').textContent,
                     shortcut: (b.querySelector('.dropdown-shortcut') || {}).textContent || null,
                     disabled: b.disabled,
                   })),
                   seps: list.querySelectorAll('.dropdown-sep').length,
                   visible: !list.hidden,
                 });
               })()""",
        )
    )


def menu_click(cdp, menu_label, item_label):
    ok = js(
        cdp,
        """(() => {
             const t = [...document.querySelectorAll('.dropdown-trigger')]
               .find(b => b.textContent === %s);
             if (!t) return false;
             t.click();
             const open = document.querySelector('.dropdown.open');
             const item = [...open.querySelectorAll('.dropdown-item')]
               .find(b => b.querySelector('span').textContent === %s);
             if (!item) return false;
             item.click();
             return true;
           })()""" % (json.dumps(menu_label), json.dumps(item_label)),
    )
    return ok


def shot(cdp, name, clip=None, scale=1):
    path = os.path.join(OUT, name)
    cdp.shot(path, clip=clip, scale=scale)
    return path


def launch():
    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--remote-debugging-port=%d --remote-allow-origins=*" % PORT
    )
    return subprocess.Popen(
        [EXE],
        env=env,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    os.makedirs(OUT, exist_ok=True)
    if os.path.isdir(DOCS):
        shutil.rmtree(DOCS, ignore_errors=True)
    os.makedirs(os.path.join(DOCS, "sub"), exist_ok=True)

    path_a = os.path.join(DOCS, "alpha.md")
    path_b = os.path.join(DOCS, "beta.md")
    path_g = os.path.join(DOCS, "gamma.txt")
    for path, text in ((path_a, ALPHA), (path_b, BETA), (path_g, GAMMA)):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    with open(os.path.join(DOCS, "ignore.xyz"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("不该出现在侧边栏\n")
    with open(os.path.join(DOCS, "sub", "delta.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 子目录里的文件\n")

    proc = launch()
    cdp = connect()
    time.sleep(1.2)

    # 备份用户原有的 localStorage，测完恢复
    snapshot = js(cdp, "JSON.stringify(Object.assign({}, localStorage))")

    # 统一初始状态：清干净 -> 只留 alpha 一个标签 -> reload
    session = json.dumps({"paths": [path_a], "activePath": path_a}, ensure_ascii=False)
    js(
        cdp,
        """(() => {
             localStorage.clear();
             localStorage.setItem('pymd-debug', '1');
             localStorage.setItem('pymd-tabs', %s);
             localStorage.setItem('pymd-view', 'edit');
             localStorage.setItem('pymd-theme', 'light');
             localStorage.setItem('pymd-sidebar', '1');
             localStorage.setItem('pymd-outline', '1');
             localStorage.setItem('pymd-font', '14');
             localStorage.setItem('pymd-recent', '[]');
             return true;
           })()""" % json.dumps(session),
    )
    cdp = reload_app(cdp, "document.querySelectorAll('.doc-tab').length === 1")
    wait_for(cdp, "window.__pymd !== undefined", 10, "调试句柄")

    # ------------------------------------------------------------------
    section("1. 启动状态")
    s = state(cdp)
    check("窗口标题", s["appTitle"], "alpha.md - pyMD")
    check("菜单栏分组", s["menus"], ["文件", "编辑", "查看", "帮助"])
    check("标签数量", len(s["tabs"]), 1)
    check("标签名", [t["name"] for t in s["tabs"]], ["alpha.md"])
    check("状态栏路径", s["statusPath"].replace("/", "\\").lower(), path_a.replace("/", "\\").lower())
    check("主题", s["theme"], "light")
    check("字号变量", s["fs"], "14px")
    check("侧边栏默认展开", s["sidebarHidden"], False)
    check("目录默认展开", s["outlineHidden"], False)
    check("编辑器已装载内容", doc(cdp).splitlines()[0], "# alpha 文档")
    shot(cdp, "01-start.png")

    # ------------------------------------------------------------------
    section("1b. 界面布局（标题栏顺序 / 工具栏分组 / 侧边栏溢出）")
    lay = json.loads(
        js(
            cdp,
            """(() => {
                 const box = x => x.getBoundingClientRect();
                 const gap = (a, b) => Math.round(box(document.getElementById(b)).left
                                                    - box(document.getElementById(a)).right);
                 const mb = box(document.getElementById('menubar'));
                 const ic = box(document.getElementById('titlebar-icon'));
                 const ttl = box(document.getElementById('app-title'));
                 const sb = document.getElementById('sidebar');
                 const sec = document.querySelector('.side-section');
                 return JSON.stringify({
                   order: [...document.getElementById('titlebar').children].map(e => e.id || e.className),
                   iconRight: Math.round(ic.right),
                   menuLeft: Math.round(mb.left),
                   iconSize: [Math.round(ic.width), Math.round(ic.height)],
                   titleLeft: Math.round(ttl.left),
                   menuRight: Math.round(mb.right),
                   titleBorder: getComputedStyle(document.getElementById('app-title')).borderLeftWidth,
                   gapOutlineNew: gap('btn-outline', 'btn-new'),
                   gapSaveasFind: gap('btn-save-as', 'btn-find'),
                   gapInGroup: gap('btn-new', 'btn-open'),
                   groups: document.querySelectorAll('#toolbar .group').length,
                   navInToolbar: document.getElementById('tabbar').parentElement
                                   === document.getElementById('toolbar'),
                   navRightOfReplace: Math.round(document.getElementById('tabbar').getBoundingClientRect().left
                                   - document.getElementById('btn-replace').getBoundingClientRect().right) >= 0,
                   navIsCapsule: document.getElementById('tabbar').classList.contains('group'),
                   groupBg: getComputedStyle(document.querySelector('#toolbar .group')).backgroundColor,
                   groupRadius: getComputedStyle(document.querySelector('#toolbar .group')).borderRadius,
                   barBg: getComputedStyle(document.getElementById('toolbar')).backgroundColor,
                   sideSectionShrink: getComputedStyle(sec).flexShrink,
                   sidebarOverflowX: getComputedStyle(sb).overflowX,
                   fileNameMin: getComputedStyle(document.querySelector('.file-name')).minWidth,
                 });
               })()""",
        )
    )
    check("标题栏顺序：图标/菜单在左、文件名在右", lay["order"], ["titlebar-icon", "menubar", "app-title", "window-actions"])
    check("app 图标在菜单左边", lay["iconRight"] <= lay["menuLeft"], True)
    check("app 图标 16x16", lay["iconSize"], [16, 16])
    check("菜单在图标之后（不再贴窗口最左）", lay["menuLeft"] >= 25, True)
    check("文件名起点在菜单右侧", lay["titleLeft"] >= lay["menuRight"], True)
    check("文件名与菜单有竖线分隔", lay["titleBorder"], "1px")
    check("工具栏分七组（视图切换、硬换行、公式转图各占一块）", lay["groups"], 7)
    check("视图切换并进工具栏", lay["navInToolbar"], True)
    check("视图切换在「替换」右边", lay["navRightOfReplace"], True)
    check("视图切换自己是一块胶囊", lay["navIsCapsule"], True)
    check("目录 → 新建 间距（2px 胶囊内边 + 10px 组间）", lay["gapOutlineNew"], 14)
    check("另存为 → 查找 间距", lay["gapSaveasFind"], 14)
    check("组内仍然紧凑", lay["gapInGroup"], 4)
    check("每组有独立底色", lay["groupBg"] != lay["barBg"], True)
    check("分组圆角", lay["groupRadius"], "8px")
    check("侧边栏分组不被压缩（长列表不会压到下一组）", lay["sideSectionShrink"], "0")
    check("侧边栏裁掉横向溢出", lay["sidebarOverflowX"], "hidden")
    check("文件名可被压缩出省略号", lay["fileNameMin"], "0px")

    # ------------------------------------------------------------------
    section("2. Markdown 渲染与统计")
    wait_for(cdp, "document.querySelectorAll('#preview h2').length === 1", 10, "预览渲染")
    render = json.loads(
        js(
            cdp,
            """JSON.stringify({
                 h1: document.querySelector('#preview h1')?.textContent || '',
                 levels: ['h1','h2','h3','h4','h5','h6'].map(t => document.querySelectorAll('#preview ' + t).length),
                 tables: document.querySelectorAll('#preview table').length,
                 rows: document.querySelectorAll('#preview table tbody tr').length,
                 tasks: document.querySelectorAll('#preview input[type=checkbox]').length,
                 checked: document.querySelectorAll('#preview input[type=checkbox]:checked').length,
                 code: (document.querySelector('#preview pre code')?.textContent || ''),
                 katex: document.querySelectorAll('#preview .katex').length,
                 katexError: document.querySelectorAll('#preview .katex-error').length,
                 link: document.querySelector('#preview a')?.getAttribute('href') || '',
                 alertBlock: document.querySelectorAll('#preview .alert, #preview blockquote').length,
               })""",
        )
    )
    check("h1 文本", render["h1"], "alpha 文档")
    check("各级标题数量", render["levels"], [2, 1, 1, 1, 1, 1])
    check("表格行数", [render["tables"], render["rows"]], [1, 1])
    check("任务列表勾选状态", [render["tasks"], render["checked"]], [2, 1])
    check("代码块内容", 'print("hi")' in render["code"], True)
    check("公式渲染节点数", render["katex"] >= 3, True)
    check("公式无报错", render["katexError"], 0)
    check("链接 href", render["link"], "https://example.com")
    check("引用/提示块存在", render["alertBlock"] >= 1, True)

    wait_for(cdp, "/\\d+ 字 · \\d+ 行 · 7 个标题/.test(document.getElementById('status-stat').textContent)", 10, "统计文案")
    check("状态栏统计", re.sub(r"^\d+ 字 · \d+ 行", "N 字 · N 行", state(cdp)["statusStat"]), "N 字 · N 行 · 7 个标题")
    shot(cdp, "02-preview.png")

    # ------------------------------------------------------------------
    section("3. 三视图切换")
    key(cdp, "2")
    time.sleep(0.6)
    s = state(cdp)
    check("分屏：两块都显示", [s["view"]["editorHidden"], s["view"]["previewHidden"]], [False, False])
    check("分屏：分隔条显示", s["view"]["dividerHidden"], False)
    widths = json.loads(
        js(
            cdp,
            """JSON.stringify({
                 editor: document.getElementById('editor-pane').offsetWidth,
                 preview: document.getElementById('preview-pane').offsetWidth,
                 outline: document.getElementById('outline-pane').offsetWidth,
               })""",
        )
    )
    check("分屏宽度大致 1:1", abs(widths["editor"] - widths["preview"]) <= 8, True)
    check("分屏宽度明细", widths)
    shot(cdp, "03-split.png")

    key(cdp, "3")
    time.sleep(0.5)
    s = state(cdp)
    check("预览：编辑器隐藏", s["view"]["editorHidden"], True)
    check("预览：分隔条隐藏", s["view"]["dividerHidden"], True)

    key(cdp, "1")
    time.sleep(0.5)
    s = state(cdp)
    check("文本：预览隐藏", [s["view"]["editorHidden"], s["view"]["previewHidden"]], [False, True])

    key(cdp, "e")  # edit -> split
    time.sleep(0.4)
    check("Ctrl+E 循环到分屏", state(cdp)["view"], {"editorHidden": False, "previewHidden": False, "dividerHidden": False})
    key(cdp, "e")
    time.sleep(0.4)
    check("Ctrl+E 循环到预览", state(cdp)["view"]["editorHidden"], True)
    key(cdp, "e")
    time.sleep(0.4)
    check("Ctrl+E 循环回文本", state(cdp)["view"]["previewHidden"], True)
    check("视图写入 localStorage", state(cdp)["stored"]["view"], "edit")

    # 点击标签栏按钮
    click(cdp, "#tabbar [data-view='split']")
    time.sleep(0.5)
    check("点标签栏进分屏", state(cdp)["view"]["previewHidden"], False)
    click(cdp, "#tabbar [data-view='edit']")
    time.sleep(0.4)

    # ------------------------------------------------------------------
    section("4. 侧边栏")
    s = state(cdp)
    check("目录文件列表", sorted(s["dir"]), ["alpha.md", "beta.md", "gamma.txt"])
    check("目录计数", s["dirCount"], "3")
    check("非 md/txt 被过滤", "ignore.xyz" in s["dir"], False)
    check("子目录不进列表", "delta.md" in s["dir"], False)
    check("当前文件高亮", s["dirActive"], ["alpha.md"])

    click_dir_file(cdp, "beta.md")
    time.sleep(1.2)
    s = state(cdp)
    check("点目录打开新标签", [t["name"] for t in s["tabs"]], ["alpha.md", "beta.md"])
    check("新标签成为活动标签", [t["active"] for t in s["tabs"]], [False, True])
    check("内容切换到 beta", doc(cdp).splitlines()[0], "# beta 长文")
    check("最近打开含 beta", s["recent"][0], "beta.md")
    check("最近打开只记通过打开动作载入的文件", s["recent"], ["beta.md"])
    check("目录高亮跟随", s["dirActive"], ["beta.md"])

    key(cdp, "b")
    time.sleep(0.4)
    s = state(cdp)
    check("Ctrl+B 收起侧边栏", s["sidebarHidden"], True)
    check("侧边栏状态落盘", s["stored"]["sidebar"], "0")
    key(cdp, "b")
    time.sleep(0.4)
    check("Ctrl+B 再展开", state(cdp)["sidebarHidden"], False)

    click(cdp, "#dir-title")
    time.sleep(0.3)
    collapsed = js(cdp, "document.getElementById('dir-list').hidden")
    click(cdp, "#dir-title")
    time.sleep(0.3)
    expanded = js(cdp, "document.getElementById('dir-list').hidden")
    check("分组可折叠/展开", [collapsed, expanded], [True, False])

    # ------------------------------------------------------------------
    section("5. 多标签隔离")
    # 在 beta 里插入标记
    js(cdp, "window.__pymd.editor.gotoLine(1); true")
    cdp.call("Input.insertText", text="BETA-EDIT ")
    time.sleep(0.6)
    s = state(cdp)
    check("beta 出现未保存圆点", [t["dirty"] for t in s["tabs"]], [False, True])
    check("标题带星号", s["appTitle"], "beta.md * - pyMD")
    check("状态栏未保存提示", s["statusDirty"], "● 未保存")
    shot(cdp, "04-dirty.png")

    click_tab(cdp, 0)
    time.sleep(0.9)
    s = state(cdp)
    check("切回 alpha 内容不受影响", doc(cdp).splitlines()[0], "# alpha 文档")
    check("脏标记只属于 beta", [t["dirty"] for t in s["tabs"]], [False, True])
    check("窗口标题跟随活动标签", s["appTitle"], "alpha.md - pyMD")

    click_tab(cdp, 1)
    time.sleep(0.9)
    check("beta 改动仍在", doc(cdp).startswith("BETA-EDIT"), True)
    key(cdp, "z")
    time.sleep(0.5)
    check("beta 独立撤销栈生效", doc(cdp).startswith("# beta 长文"), True)

    # 滚动位置独立
    js(cdp, "document.querySelector('.cm-editor .cm-scroller').scrollTop = 260; true")
    time.sleep(0.3)
    click_tab(cdp, 0)
    time.sleep(0.7)
    click_tab(cdp, 1)
    time.sleep(0.9)
    scroll = js(cdp, "Math.round(document.querySelector('.cm-editor .cm-scroller').scrollTop)")
    check("切回后滚动位置保留", scroll >= 200, True)
    check("滚动值", scroll)

    # ------------------------------------------------------------------
    section("6. 保存落盘")
    key(cdp, "s")
    time.sleep(1.5)
    s = state(cdp)
    check("保存后圆点消失", [t["dirty"] for t in s["tabs"]], [False, False])
    check("保存提示", s["statusStat"].startswith("已写入 "), True)
    time.sleep(4.2)  # 等 flash 过期，回到统计
    check("保存后恢复统计文案", "字 · " in state(cdp)["statusStat"], True)

    # ------------------------------------------------------------------
    section("7. 查找与替换（在草稿标签里做，不污染磁盘）")
    key(cdp, "n")
    time.sleep(0.8)
    s = state(cdp)
    check("Ctrl+N 新建标签", [t["name"] for t in s["tabs"]], ["alpha.md", "beta.md", "未命名"])
    set_doc(cdp, "重复词 一\n中间行\n重复词 二\n再来一行\n重复词 三\n")
    time.sleep(0.6)

    key(cdp, "f")
    time.sleep(0.4)
    s = state(cdp)
    check("Ctrl+F 打开查找栏", s["findOpen"], True)
    check("查找模式隐藏替换行", s["replaceHidden"], True)
    check("焦点在查找框", js(cdp, "document.activeElement.id"), "find-input")

    js(
        cdp,
        """(() => {
             const i = document.getElementById('find-input');
             i.value = '重复词';
             i.dispatchEvent(new Event('input', {bubbles: true}));
             document.getElementById('find-next').click();
             return true;
           })()""",
    )
    time.sleep(0.5)
    s = state(cdp)
    check("查找计数", s["findStatus"], "已定位，共 3 处匹配")
    marks = json.loads(
        js(
            cdp,
            """JSON.stringify({
                 all: document.querySelectorAll('.cm-pyFind').length,
                 active: document.querySelectorAll('.cm-pyFind-active').length,
               })""",
        )
    )
    check("命中全部高亮", [marks["all"], marks["active"]], [3, 1])
    line1 = js(cdp, "window.__pymd.editor.getCursorLine()")
    check("首次定位到第 1 处", line1, 1)
    shot(cdp, "05-find.png")

    js(cdp, "document.getElementById('find-next').click(); true")
    time.sleep(0.3)
    check("第二次定位到第 3 行", js(cdp, "window.__pymd.editor.getCursorLine()"), 3)
    js(cdp, "document.getElementById('find-next').click(); true")
    time.sleep(0.3)
    js(cdp, "document.getElementById('find-next').click(); true")
    time.sleep(0.3)
    check("第四次定位回绕到第 1 行", js(cdp, "window.__pymd.editor.getCursorLine()"), 1)

    # 无匹配
    js(cdp, "document.getElementById('find-input').value = '不存在的词'; true")
    js(cdp, "document.getElementById('find-next').click(); true")
    time.sleep(0.3)
    check("无匹配提示", state(cdp)["findStatus"], "没有找到匹配内容")
    # 空查询
    js(cdp, "document.getElementById('find-input').value = ''; true")
    js(cdp, "document.getElementById('find-next').click(); true")
    time.sleep(0.3)
    check("空查询提示", state(cdp)["findStatus"], "请输入要查找的内容")

    # 替换
    key(cdp, "h")
    time.sleep(0.4)
    s = state(cdp)
    check("Ctrl+H 显示替换行", s["replaceHidden"], False)
    js(
        cdp,
        """(() => {
             document.getElementById('find-input').value = '重复词';
             document.getElementById('replace-input').value = 'REP';
             document.getElementById('find-next').click();
             return true;
           })()""",
    )
    time.sleep(0.4)
    js(cdp, "document.getElementById('replace-one').click(); true")
    time.sleep(0.6)
    s = state(cdp)
    check("单次替换计数", s["findStatus"], "已替换当前匹配，全文原有 3 处")
    check("单次替换结果", doc(cdp).count("REP"), 1)
    check("剩余匹配 2 处", doc(cdp).count("重复词"), 2)

    js(cdp, "document.getElementById('replace-all').click(); true")
    time.sleep(0.7)
    s = state(cdp)
    check("全部替换计数", s["findStatus"], "已全部替换，共处理 2 处匹配")
    check("全部替换结果", [doc(cdp).count("REP"), doc(cdp).count("重复词")], [3, 0])

    key(cdp, "Escape", ctrl=False)
    time.sleep(0.4)
    s = state(cdp)
    check("Esc 关闭查找栏", s["findOpen"], False)
    check("Esc 清除高亮", js(cdp, "document.querySelectorAll('.cm-pyFind').length"), 0)
    shot(cdp, "06-replace-done.png")

    # ------------------------------------------------------------------
    section("8. 编辑命令与剪贴板")
    set_doc(cdp, "第一行\n第二行\n第三行\n")
    time.sleep(0.5)
    key(cdp, "a")
    time.sleep(0.3)
    FULL = "第一行\n第二行\n第三行\n"
    check("Ctrl+A 全选", js(cdp, "window.__pymd.editor.getSelectedText()"), FULL)
    check("右键菜单有选区时不禁用", menu_has_disabled(cdp), [])

    copied = menu_click(cdp, "编辑", "复制")
    time.sleep(0.8)
    check("菜单复制可用", copied, True)
    check("复制提示", state(cdp)["statusStat"], "已复制 %d 个字符" % len(FULL))

    key(cdp, "n")  # 新草稿
    time.sleep(0.6)
    menu_click(cdp, "编辑", "粘贴")
    time.sleep(0.8)
    check("菜单粘贴送达内容", doc(cdp), FULL)

    menu_click(cdp, "编辑", "撤销")
    time.sleep(0.5)
    check("菜单撤销清空粘贴", doc(cdp), "")
    menu_click(cdp, "编辑", "重做")
    time.sleep(0.5)
    check("菜单重做恢复粘贴", doc(cdp), FULL)

    check("菜单全选", menu_click(cdp, "编辑", "全选"), True)
    time.sleep(0.3)
    menu_click(cdp, "编辑", "剪切")
    time.sleep(0.8)
    check("菜单剪切清空文档", doc(cdp), "")

    # 菜单栏的剪切/复制不像右键菜单那样置灰（下拉是一次性构建的，状态会过期），
    # 但点下去会给出提示，不会静默失败。
    check("菜单栏剪切/复制不置灰（已知差异）", menu_has_disabled(cdp), [])
    check("无选区点复制给提示", (menu_click(cdp, "编辑", "复制"), time.sleep(0.4), state(cdp)["statusStat"])[2], "请先选中要复制的内容")

    # ------------------------------------------------------------------
    section("9. 菜单栏与弹窗")
    open_menu(cdp, "文件")
    snap = menu_snapshot(cdp)
    check("文件菜单展开", snap["visible"], True)
    check("文件菜单项", [i["label"] for i in snap["items"]],
          ["新建", "打开", "保存", "另存为", "导出为纯文本 (.txt)", "关闭标签", "关闭窗口"])
    check("文件菜单分隔线", snap["seps"], 2)
    check("快捷键标注", next(i["shortcut"] for i in snap["items"] if i["label"] == "另存为"), "Ctrl+Shift+S")
    shot(cdp, "07-menu-file.png")

    open_menu(cdp, "编辑")
    snap = menu_snapshot(cdp)
    check("编辑菜单项", [i["label"] for i in snap["items"]],
          ["撤销", "重做", "剪切", "复制", "复制为纯文本", "粘贴", "全选", "查找", "替换"])
    open_menu(cdp, "查看")
    snap = menu_snapshot(cdp)
    check("查看菜单项", [i["label"] for i in snap["items"]],
          ["文本模式", "左右模式", "预览模式", "显示/隐藏侧边栏", "显示/隐藏目录",
           "放大字体", "缩小字体", "重置字体大小"])
    open_menu(cdp, "帮助")
    snap = menu_snapshot(cdp)
    check("帮助菜单项", [i["label"] for i in snap["items"]], ["关于 pyMD", "快捷键说明"])

    key(cdp, "Escape", ctrl=False)
    time.sleep(0.3)
    check("Esc 关闭菜单", menu_snapshot(cdp), None)

    # 菜单动作：查看 -> 预览模式
    menu_click(cdp, "查看", "预览模式")
    time.sleep(0.6)
    check("菜单切预览模式", state(cdp)["view"]["editorHidden"], True)
    menu_click(cdp, "查看", "文本模式")
    time.sleep(0.5)

    # 关于 / 快捷键弹窗
    menu_click(cdp, "帮助", "关于 pyMD")
    time.sleep(0.5)
    about = json.loads(
        js(
            cdp,
            """JSON.stringify({
                 open: document.getElementById('info-dialog').open,
                 title: document.getElementById('info-title').textContent,
                 body: document.getElementById('info-body').textContent,
               })""",
        )
    )
    check("关于弹窗打开", about["open"], True)
    check("关于标题", about["title"], "关于")
    check("关于含版本号", ("v" + VERSION) in about["body"], True)
    check("关于含版权行", "保留所有权利" in about["body"], True)
    shot(cdp, "08-about.png")
    js(cdp, "document.getElementById('info-dialog').close(); true")
    time.sleep(0.3)

    menu_click(cdp, "帮助", "快捷键说明")
    time.sleep(0.5)
    shortcuts = js(cdp, "document.getElementById('info-body').textContent")
    check("快捷键弹窗内容", "Ctrl+Shift+O   显示/隐藏目录" in shortcuts, True)
    js(cdp, "document.getElementById('info-dialog').close(); true")
    time.sleep(0.3)

    # ------------------------------------------------------------------
    section("10. 字号与主题")
    key(cdp, "=")
    time.sleep(0.3)
    check("Ctrl+= 放大一号", [state(cdp)["fs"], state(cdp)["statusStat"]], ["15px", "字号 15px"])
    key(cdp, "-")
    time.sleep(0.2)
    key(cdp, "-")
    time.sleep(0.3)
    check("Ctrl+- 缩小两号", state(cdp)["fs"], "13px")
    key(cdp, "0")
    time.sleep(0.3)
    check("Ctrl+0 重置", [state(cdp)["fs"], state(cdp)["stored"]["font"]], ["14px", "14"])
    # 上下限
    for _ in range(12):
        key(cdp, "=")
        time.sleep(0.05)
    check("字号上限 22px", state(cdp)["fs"], "22px")
    for _ in range(14):
        key(cdp, "-")
        time.sleep(0.05)
    check("字号下限 12px", state(cdp)["fs"], "12px")
    key(cdp, "0")
    time.sleep(0.2)

    click(cdp, "#btn-theme")
    time.sleep(0.4)
    dark = state(cdp)
    check("主题切到 dark", dark["theme"], "dark")
    check("主题落盘", dark["stored"]["theme"], "dark")
    shot(cdp, "09-dark.png")
    click(cdp, "#btn-theme")
    time.sleep(0.4)
    check("主题切回 light", state(cdp)["theme"], "light")

    # ------------------------------------------------------------------
    section("11. 文档目录")
    click_tab(cdp, 0)  # alpha
    time.sleep(1.0)
    s = state(cdp)
    check("目录标题列表", s["outline"], ALPHA_TITLES)
    check("目录计数", s["outlineCount"], "7")
    expect_line = ALPHA.splitlines().index("> # 引用里的标题") + 1
    click_outline(cdp, 3)
    time.sleep(0.6)
    check("目录跳转行号", js(cdp, "window.__pymd.editor.getCursorLine()"), expect_line)
    check("目录高亮跟随", js(cdp, "document.querySelectorAll('.outline-item')[3].classList.contains('active')"), True)

    js(cdp, "window.__pymd.editor.gotoLine(1); true")
    time.sleep(0.5)
    check("光标回顶时高亮第一条", js(cdp, "document.querySelectorAll('.outline-item')[0].classList.contains('active')"), True)

    key(cdp, "o", shift=True)
    time.sleep(0.4)
    check("Ctrl+Shift+O 收起目录", state(cdp)["outlineHidden"], True)
    key(cdp, "o", shift=True)
    time.sleep(0.4)
    check("Ctrl+Shift+O 展开目录", [state(cdp)["outlineHidden"], state(cdp)["stored"]["outline"]], [False, "1"])
    shot(cdp, "10-outline.png")

    # 预览滚动联动
    key(cdp, "2")
    time.sleep(0.7)
    js(cdp, "document.getElementById('preview-pane').scrollTop = 0; true")
    click_outline(cdp, 6)
    time.sleep(0.8)
    scroll_info = json.loads(
        js(
            cdp,
            """JSON.stringify({
                 scrollTop: Math.round(document.getElementById('preview-pane').scrollTop),
                 visible: (() => {
                   const node = document.querySelector('[data-hi="6"]');
                   if (!node) return false;
                   const pane = document.getElementById('preview-pane').getBoundingClientRect();
                   const r = node.getBoundingClientRect();
                   return r.top >= pane.top - 4 && r.top <= pane.bottom;
                 })(),
               })""",
        )
    )
    check("预览滚动到目标标题", scroll_info["visible"], True)
    check("预览滚动值", scroll_info["scrollTop"])
    key(cdp, "1")
    time.sleep(0.4)

    # ------------------------------------------------------------------
    section("12. 右键菜单")
    js(cdp, "window.__pymd.editor.gotoLine(1); document.querySelector('.cm-content').focus(); true")
    time.sleep(0.3)
    ctx = json.loads(
        js(
            cdp,
            """(() => {
                 document.querySelector('.cm-content').dispatchEvent(new MouseEvent('contextmenu', {
                   bubbles: true, cancelable: true, clientX: 320, clientY: 260,
                 }));
                 const m = document.getElementById('ctxmenu');
                 const r = m.getBoundingClientRect();
                 return JSON.stringify({
                   visible: !m.hidden,
                   items: [...m.querySelectorAll('.dropdown-item')].map(b => b.querySelector('span').textContent),
                   disabled: [...m.querySelectorAll('.dropdown-item:disabled')].map(b => b.querySelector('span').textContent),
                   seps: m.querySelectorAll('.dropdown-sep').length,
                   inside: r.right <= window.innerWidth && r.bottom <= window.innerHeight,
                 });
               })()""",
        )
    )
    check("右键菜单出现", ctx["visible"], True)
    check("右键菜单项", ctx["items"],
          ["撤销", "重做", "剪切", "复制", "复制为纯文本", "粘贴", "全选", "查找", "替换"])
    check("无选区时剪切/复制置灰", ctx["disabled"], ["剪切", "复制"])
    check("右键菜单不越界", ctx["inside"], True)
    shot(cdp, "11-contextmenu.png")
    js(cdp, "document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); true")
    time.sleep(0.2)
    check("点别处关闭右键菜单", js(cdp, "document.getElementById('ctxmenu').hidden"), True)

    # ------------------------------------------------------------------
    section("13. 文件监听")
    # 当前标签是 alpha 且干净
    click_tab(cdp, 0)
    time.sleep(0.8)
    check("监听对象是 alpha", doc(cdp).splitlines()[0], "# alpha 文档")
    with open(path_a, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n外部追加的一段。\n")
    wait_for(cdp, "window.__pymd.editor.getDoc().includes('外部追加的一段')", 12, "外部修改自动重载")
    check("外部修改后自动重载", "外部追加的一段" in doc(cdp), True)
    check("重载提示", state(cdp)["statusStat"], "已从磁盘重新载入")

    # 有未保存改动时不覆盖
    js(cdp, "window.__pymd.editor.gotoLine(1); window.__pymd.editor.focus(); true")
    cdp.call("Input.insertText", text="LOCAL ")
    time.sleep(0.5)
    with open(path_a, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n第二次外部写入。\n")
    time.sleep(1.8)
    got = doc(cdp)
    check("脏状态下不被外部覆盖", got.startswith("LOCAL "), True)
    check("未覆盖时给出提示", "已被外部修改" in state(cdp)["statusStat"], True)
    check("外部内容没有混进本地", "第二次外部写入" in got, False)

    # ------------------------------------------------------------------
    section("14. 关闭标签与会话恢复")
    # 先把 alpha 的本地改动丢掉（重新读盘）：关掉它再重开
    js(cdp, "window.__pymd.editor.setDoc(%s); true" % json.dumps(open(path_a, encoding="utf-8").read()))
    time.sleep(0.4)
    key(cdp, "s")
    time.sleep(1.2)
    check("覆盖保存成功", state(cdp)["tabs"][0]["dirty"], False)

    key(cdp, "w")
    time.sleep(1.2)
    s = state(cdp)
    check("Ctrl+W 关闭当前标签", "alpha.md" not in [t["name"] for t in s["tabs"]], True)
    check("剩余标签（两个草稿各占一个）", [t["name"] for t in s["tabs"]], ["beta.md", "未命名", "未命名"])

    # Ctrl+Tab 循环
    before_active = [t["name"] for t in s["tabs"] if t["active"]][0]
    key(cdp, "Tab")
    time.sleep(0.6)
    after_active = [t["name"] for t in state(cdp)["tabs"] if t["active"]][0]
    check("Ctrl+Tab 切换标签", after_active != before_active, True)

    # 会话里只应记录有路径的
    stored = json.loads(state(cdp)["stored"]["tabs"])
    check("会话只记有路径的标签", stored["paths"], [path_b])

    cdp = reload_app(cdp, "document.querySelectorAll('.doc-tab').length === 1")
    s = state(cdp)
    check("重启恢复已保存标签", [t["name"] for t in s["tabs"]], ["beta.md"])
    check("恢复内容来自磁盘", doc(cdp).splitlines()[0], "# beta 长文")
    check("恢复后无脏标记", s["tabs"][0]["dirty"], False)
    shot(cdp, "12-restored.png")

    # ------------------------------------------------------------------
    # 汇总
    failed = [r for r in results if not r["ok"]]
    print("\n汇总: %d 项，通过 %d，失败 %d" % (len(results), len(results) - len(failed), len(failed)))
    for r in failed:
        print("  FAIL:", r["step"], "got=", json.dumps(r["got"], ensure_ascii=False),
              "expect=", json.dumps(r["expect"], ensure_ascii=False))

    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"total": len(results), "failed": len(failed), "results": results},
                  fh, ensure_ascii=False, indent=1)

    # 收尾：恢复用户原有 localStorage，杀掉进程
    try:
        js(
            cdp,
            """(() => {
                 localStorage.clear();
                 const prev = %s;
                 for (const k of Object.keys(prev)) localStorage.setItem(k, prev[k]);
                 return true;
               })()""" % snapshot,
        )
    except Exception as exc:
        print("恢复 localStorage 失败:", exc)

    time.sleep(0.3)
    try:
        proc.kill()
    except Exception:
        pass
    return 0 if not failed else 1


def menu_has_disabled(cdp):
    """展开编辑菜单，返回禁用项标签，然后关掉菜单。"""
    out = js(
        cdp,
        """(() => {
             const t = [...document.querySelectorAll('.dropdown-trigger')]
               .find(b => b.textContent === '编辑');
             t.click();
             const open = document.querySelector('.dropdown.open');
             const items = [...open.querySelectorAll('.dropdown-item:disabled')]
               .map(b => b.querySelector('span').textContent);
             t.click();
             return JSON.stringify(items);
           })()""",
    )
    return json.loads(out)


if __name__ == "__main__":
    sys.exit(main())
