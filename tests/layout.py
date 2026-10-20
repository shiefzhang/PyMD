"""界面布局回归：标题栏顺序、工具栏分组间距、侧边栏长列表不溢出。

前两项靠 CDP 读几何位置，第三项要真的造一个「当前目录里有 40 多个文件」的现场：
列表长起来时曾在旧版本里溢出色块、压到「最近打开」的文字上（.side-section 被 flex 压缩）。

运行前先确认没有残留的 pymd.exe —— 同一份 WebView2 用户数据目录被两个实例挂载会 panic。
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, wait_targets  # noqa: E402

EXE = r"D:\mdpad-target\release\pymd.exe"
PORT = 9247
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".artifacts", "layout")
DOCS = r"D:\tmp\pymd_layoutdir"
LONG = "这是一个特别特别长的中文文件名用来测试侧边栏的省略号效果到底有没有生效对吧.md"

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


def connect():
    return CDP(wait_targets(PORT)[0]["webSocketDebuggerUrl"])


def clip_of(cdp, selector):
    return json.loads(
        js(
            cdp,
            """(() => { const r = document.querySelector(%s).getBoundingClientRect();
                 return JSON.stringify({x: Math.round(r.x), y: Math.round(r.y),
                                        width: Math.round(r.width), height: Math.round(r.height)}); })()"""
            % json.dumps(selector),
        )
    )


def make_docs():
    if os.path.isdir(DOCS):
        shutil.rmtree(DOCS, ignore_errors=True)
    os.makedirs(DOCS, exist_ok=True)
    names = ["%02d-第%d篇技术笔记与排期记录.md" % (i, i) for i in range(1, 41)]
    names.append(LONG)
    for n in names:
        with open(os.path.join(DOCS, n), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 标题\n\n正文一行。\n")
    return names


def main():
    subprocess.run(["taskkill", "/IM", "pymd.exe", "/F"], capture_output=True)
    time.sleep(1)
    os.makedirs(OUT, exist_ok=True)
    names = make_docs()
    first = os.path.join(DOCS, names[0])

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
        cdp = connect()
        time.sleep(1.2)
        snapshot = js(cdp, "JSON.stringify(Object.assign({}, localStorage))")
        session = json.dumps({"paths": [first], "activePath": first}, ensure_ascii=False)
        recent = json.dumps(
            [{"path": first, "name": names[0], "at": 1},
             {"path": os.path.join(DOCS, LONG), "name": LONG, "at": 2}],
            ensure_ascii=False,
        )
        js(
            cdp,
            """(() => {
                 localStorage.clear();
                 localStorage.setItem('pymd-debug', '1');
                 localStorage.setItem('pymd-tabs', %s);
                 localStorage.setItem('pymd-view', 'split');
                 localStorage.setItem('pymd-theme', 'light');
                 localStorage.setItem('pymd-sidebar', '1');
                 localStorage.setItem('pymd-outline', '1');
                 localStorage.setItem('pymd-recent', %s);
                 return true;
               })()""" % (json.dumps(session), json.dumps(recent)),
        )
        js(cdp, "location.reload(); true")
        time.sleep(2.2)
        cdp = connect()
        wait_for(cdp, "document.querySelectorAll('#dir-list .file-item').length >= 41", 20, "目录列表")

        # ---------- 1. 标题栏 ----------
        print("\n== 1. 标题栏：图标 + 菜单顶格，文件名在菜单右边")
        bar = json.loads(
            js(
                cdp,
                """(() => {
                     const b = x => { const r = x.getBoundingClientRect();
                       return {l: Math.round(r.left), r: Math.round(r.right)}; };
                     const mb = document.getElementById('menubar');
                     const t = document.getElementById('app-title');
                     const ic = document.getElementById('titlebar-icon');
                     return JSON.stringify({
                       order: [...document.getElementById('titlebar').children].map(e => e.id || e.className),
                       mb: b(mb), title: b(t), icon: b(ic),
                       iconW: Math.round(ic.getBoundingClientRect().width),
                       iconH: Math.round(ic.getBoundingClientRect().height),
                       iconLoaded: ic.complete && ic.naturalWidth > 0,
                       iconRing: getComputedStyle(ic).boxShadow,
                       titleText: t.textContent,
                       border: getComputedStyle(t).borderLeftWidth,
                     });
                   })()""",
            )
        )
        check("标题栏顺序", bar["order"], ["titlebar-icon", "menubar", "app-title", "window-actions"])
        check("图标在菜单左侧", bar["icon"]["r"] <= bar["mb"]["l"], True)
        check("图标左边缘留 9px", bar["icon"]["l"], 9)
        check("图标 16x16", [bar["iconW"], bar["iconH"]], [16, 16])
        check("图标图片已加载", bar["iconLoaded"], True)
        # 图标自带米色底和边缘，再加 outline/shadow 会显出「双框」，断言没被后来者加上
        check("图标不额外套环", bar["iconRing"], "none")
        check("菜单紧跟图标（不贴窗口左边）", bar["mb"]["l"] >= 9 + 16, True)
        check("文件名在菜单右侧", bar["title"]["l"] >= bar["mb"]["r"], True)
        check("文件名与菜单之间有竖线", bar["border"], "1px")
        check("标题内容仍是「文件 - pyMD」", bar["titleText"], names[0] + " - pyMD")

        # ---------- 2. 工具栏 ----------
        print("\n== 2. 工具栏分组：胶囊底色 + 间距 + 视图切换")
        tb = json.loads(
            js(
                cdp,
                """(() => {
                     const box = id => document.getElementById(id).getBoundingClientRect();
                     const gap = (a, b) => Math.round(box(b).left - box(a).right);
                     const g = document.querySelector('#toolbar .group');
                     const tb = document.getElementById('toolbar');
                     const nav = document.getElementById('tabbar');
                     const rep = document.getElementById('btn-replace');
                     const sel = nav.querySelector('.tab[aria-selected="true"]');
                     const nb = nav.getBoundingClientRect();
                     const chk = document.getElementById('chk-hardbreaks');
                     const chkLabel = chk && chk.closest('label');
                     return JSON.stringify({
                       groups: document.querySelectorAll('#toolbar .group').length,
                       outlineNew: gap('btn-outline', 'btn-new'),
                       saveasFind: gap('btn-save-as', 'btn-find'),
                       sidebarOutline: gap('btn-sidebar', 'btn-outline'),
                       newOpen: gap('btn-new', 'btn-open'),
                       findReplace: gap('btn-find', 'btn-replace'),
                       groupBg: getComputedStyle(g).backgroundColor,
                       groupRing: getComputedStyle(g).boxShadow,
                       barBg: getComputedStyle(tb).backgroundColor,
                       barH: Math.round(box('toolbar').height),
                       navInToolbar: nav.parentElement === tb,
                       navClass: nav.className,
                       navLeft: Math.round(nb.left), navRight: Math.round(nb.right),
                       navTop: Math.round(nb.top), navBottom: Math.round(nb.bottom),
                       barTop: Math.round(tb.getBoundingClientRect().top),
                       barBottom: Math.round(tb.getBoundingClientRect().bottom),
                       repRight: Math.round(rep.getBoundingClientRect().right),
                       tabs: nav.querySelectorAll('.tab').length,
                       selText: sel ? sel.textContent : null,
                       selBg: sel ? getComputedStyle(sel).backgroundColor : null,
                       selRadius: sel ? getComputedStyle(sel).borderRadius : null,
                       navChildTop: [...nav.children].filter(b => getComputedStyle(b).display !== 'none')
                                      .map(b => Math.round(b.getBoundingClientRect().top)),
                       docbarTop: Math.round(box('docbar').top),
                       chkExists: !!chk,
                       chkInToolbar: !!chk && !!chk.closest('#toolbar'),
                       chkLabel: chkLabel ? chkLabel.textContent.trim() : null,
                       chkChecked: chk ? chk.checked : null,
                       chkBox: chk ? getComputedStyle(chk).width : null,
                       chkRight: chkLabel ? Math.round(chkLabel.getBoundingClientRect().right) : null,
                       themeLeft: Math.round(document.getElementById('btn-theme').getBoundingClientRect().left),
                       chkTop: chkLabel ? Math.round(chkLabel.getBoundingClientRect().top) : null,
                       chkBottom: chkLabel ? Math.round(chkLabel.getBoundingClientRect().bottom) : null,
                     });
                   })()""",
            )
        )
        check("工具栏分六组（视图切换 + 硬换行开关）", tb["groups"], 6)
        check("目录 → 新建 拉开距离", tb["outlineNew"], 14)
        check("另存为 → 查找 拉开距离", tb["saveasFind"], 14)
        check("组内间距保持紧凑", [tb["sidebarOutline"], tb["newOpen"], tb["findReplace"]], [4, 4, 4])
        check("分组底色不同于工具栏底色", tb["groupBg"] != tb["barBg"], True)
        check("分组有描边环（inset）", "inset" in tb["groupRing"], True)
        check("工具栏总高未变（胶囊多出的 2px 由 padding 让出）", tb["barH"], 40)
        check("视图切换已并进工具栏", tb["navInToolbar"], True)
        check("视图切换自身也是一块胶囊", tb["navClass"], "group")
        check("视图切换在「替换」右边", tb["navLeft"] >= tb["repRight"], True)
        check("视图切换仍在一行之内（不越出工具栏上下沿）",
              [tb["navTop"] >= tb["barTop"], tb["navBottom"] <= tb["barBottom"]], [True, True])
        check("视图切换三个按钮都在", tb["tabs"], 3)
        check("三个按钮垂直对齐", len(set(tb["navChildTop"])), 1)
        check("当前模式有淡底标出（不是下划线）", tb["selBg"] != "rgba(0, 0, 0, 0)", True)
        check("当前模式按钮有圆角", tb["selRadius"], "6px")
        check("原来的独立 Tab 行已消失（文件标签条直接接在工具栏下）", tb["docbarTop"], tb["barBottom"])

        # ---------- 2b. 硬换行开关 ----------
        print("\n== 2b. 工具栏的「硬换行」勾选框")
        check("勾选框存在", tb["chkExists"], True)
        check("勾选框在工具栏里", tb["chkInToolbar"], True)
        check("标签文字是「硬换行」", tb["chkLabel"], "硬换行")
        check("默认勾上（与 Rust 默认一致）", tb["chkChecked"], True)
        check("勾选框 14px", tb["chkBox"], "14px")
        check("排在「主题」左边（中间隔着组间距）", tb["themeLeft"] - tb["chkRight"] >= 10, True)
        check("和工具栏其它控件同一行（高度落在工具栏内）",
              [tb["chkTop"] >= tb["barTop"], tb["chkBottom"] <= tb["barBottom"]], [True, True])

        # ---------- 3. 侧边栏 ----------
        print("\n== 3. 侧边栏：长列表不压住「最近打开」")
        sb = json.loads(
            js(
                cdp,
                """(() => {
                     const b = x => x.getBoundingClientRect();
                     const side = document.getElementById('sidebar');
                     const secs = document.querySelectorAll('.side-section');
                     const dirList = document.getElementById('dir-list');
                     const longName = [...document.querySelectorAll('#dir-list .file-name')]
                       .find(n => n.textContent.length > 20);
                     const ln = b(longName);
                     const row = b(longName.parentElement);
                     return JSON.stringify({
                       dirSecBottom: Math.round(b(secs[0]).bottom),
                       listBottom: Math.round(b(dirList).bottom),
                       recentTop: Math.round(b(secs[1]).top),
                       recentTitleTop: Math.round(b(document.getElementById('recent-title')).top),
                       scrollH: side.scrollHeight, clientH: side.clientHeight,
                       overflowX: getComputedStyle(side).overflowX,
                       shrink: getComputedStyle(secs[0]).flexShrink,
                       nameRight: Math.round(ln.right), rowRight: Math.round(row.right),
                       clipped: longName.scrollWidth > longName.clientWidth,
                       ellipsis: getComputedStyle(longName).textOverflow,
                     });
                   })()""",
            )
        )
        check("列表底边不越出所属分组", sb["listBottom"] <= sb["dirSecBottom"] + 1, True)
        check("当前目录与最近打开不重叠", sb["dirSecBottom"] <= sb["recentTop"], True)
        check("最近打开标题仍在自己分组里", sb["recentTitleTop"] >= sb["dirSecBottom"], True)
        check("列表长时改为整栏滚动", sb["scrollH"] > sb["clientH"], True)
        check("分组不被压缩", sb["shrink"], "0")
        check("侧边栏裁掉横向溢出", sb["overflowX"], "hidden")
        check("长文件名收进行内", sb["nameRight"] <= sb["rowRight"], True)
        check("长文件名出省略号", [sb["clipped"], sb["ellipsis"]], [True, "ellipsis"])

        cdp.shot(os.path.join(OUT, "layout-full.png"))
        cdp.shot(os.path.join(OUT, "layout-sidebar.png"), clip=clip_of(cdp, "#sidebar"), scale=2)
        cdp.shot(
            os.path.join(OUT, "layout-bars.png"),
            clip=json.loads(
                js(
                    cdp,
                    """(() => { const a = document.getElementById('titlebar').getBoundingClientRect();
                         const b = document.getElementById('toolbar').getBoundingClientRect();
                         return JSON.stringify({x: 0, y: 0, width: Math.round(a.width),
                                                height: Math.round(b.bottom + 1)}); })()""",
                )
            ),
            scale=2,
        )
    finally:
        try:
            js(cdp, """(() => { localStorage.clear();
                     const prev = %s;
                     for (const k of Object.keys(prev)) localStorage.setItem(k, prev[k]);
                     return true; })()""" % snapshot)
        except Exception as exc:
            print("恢复 localStorage 失败:", exc)
        proc.kill()
        time.sleep(0.8)
        subprocess.run(["taskkill", "/IM", "pymd.exe", "/F"], capture_output=True)

    failed = [r for r in results if not r["ok"]]
    print("\n汇总: %d 项，通过 %d，失败 %d" % (len(results), len(results) - len(failed), len(failed)))
    for r in failed:
        print("  FAIL:", r["step"], "got=", json.dumps(r["got"], ensure_ascii=False),
              "expect=", json.dumps(r["expect"], ensure_ascii=False))
    with open(os.path.join(OUT, "result_layout.json"), "w", encoding="utf-8") as fh:
        json.dump({"total": len(results), "failed": len(failed), "results": results},
                  fh, ensure_ascii=False, indent=1)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
