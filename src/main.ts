/**
 * 应用装配层：把编辑器、预览区、侧边栏、菜单、Rust 命令和界面事件连起来。
 *
 * 视图有三种模式（对齐参考 MDPad 的 edit / split / preview）：
 *   edit    纯源码
 *   split   左编辑 + 右预览，分隔条可拖
 *   preview 纯预览
 *
 * 多文件用顶部标签页：每个标签持有**独立的 EditorState**，切换时整份 state 换进
 * view.setState()，因此缓冲、撤销栈、选区天然互不干扰；滚动位置单独记录。
 *
 * 菜单栏是自绘的（tauri.conf.json 里 decorations 为 false），
 * 所以「文件 / 编辑 / 查看 / 帮助」和右上角的最小化/最大化/关闭都在前端实现，
 * 拖动窗口靠 window.startDragging()。
 */
import type { EditorState } from "@codemirror/state";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { confirm as confirmDialog, open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";

import * as api from "./api";
import appIconUrl from "./assets/app-icon.png";
import { createEditor } from "./editor";
import { renderMath } from "./math";

/* ------------------------------ DOM 引用 ------------------------------ */
function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`缺少 DOM 节点：#${id}`);
  return node as T;
}

const titlebarEl = el<HTMLElement>("titlebar");
const titlebarIconEl = el<HTMLImageElement>("titlebar-icon");
const appTitleEl = el<HTMLElement>("app-title");
const menubarEl = el<HTMLElement>("menubar");
const editorPaneEl = el<HTMLElement>("editor-pane");
const previewPaneEl = el<HTMLElement>("preview-pane");
const panesEl = el<HTMLElement>("panes");
const docTabsEl = el<HTMLElement>("doctabs");
const outlinePaneEl = el<HTMLElement>("outline-pane");
const outlineListEl = el<HTMLElement>("outline-list");
const outlineCountEl = el<HTMLElement>("outline-count");
const dividerEl = el<HTMLElement>("divider");
const sidebarEl = el<HTMLElement>("sidebar");
const dirListEl = el<HTMLElement>("dir-list");
const recentListEl = el<HTMLElement>("recent-list");
const dirCountEl = el<HTMLElement>("dir-count");
const recentCountEl = el<HTMLElement>("recent-count");
const previewEl = el<HTMLElement>("preview");
const tabButtons = Array.from(document.querySelectorAll<HTMLButtonElement>("#tabbar .tab"));
const statusPathEl = el<HTMLElement>("status-path");
const statusStatEl = el<HTMLElement>("status-stat");
const statusDirtyEl = el<HTMLElement>("status-dirty");
const findbarEl = el<HTMLElement>("findbar");
const findInputEl = el<HTMLInputElement>("find-input");
const replaceInputEl = el<HTMLInputElement>("replace-input");
const replaceFieldEl = el<HTMLElement>("replace-field");
const findStatusEl = el<HTMLElement>("find-status");
const ctxMenuEl = el<HTMLElement>("ctxmenu");
const infoDialogEl = el<HTMLDialogElement>("info-dialog");
const infoTitleEl = el<HTMLElement>("info-title");
const infoBodyEl = el<HTMLElement>("info-body");
const maxButtonEl = el<HTMLButtonElement>("win-max");

/* -------------------------------- 状态 -------------------------------- */
type ViewMode = "edit" | "split" | "preview";

const LS = {
  view: "pymd-view",
  theme: "pymd-theme",
  sidebar: "pymd-sidebar",
  recent: "pymd-recent",
  split: "pymd-split",
  font: "pymd-font",
  tabs: "pymd-tabs",
  outline: "pymd-outline",
  hardbreaks: "pymd-hardbreaks",
  mathimg: "pymd-mathimg",
};

const MAX_RECENT = 10;
const MD_EXT = /\.(md|markdown|mdx|txt)$/i;
const FONT_MIN = 12;
const FONT_MAX = 22;
const FONT_DEFAULT = 14;

interface RecentFile {
  path: string;
  name: string;
  at: number;
}

/**
 * 一个打开的文件（标签）。
 *
 * 关键点：`state` 是整份 CodeMirror 状态。活动标签的 state 由 EditorView 持有
 * （字段为 null），切走时捞回来，切回来时塞进去 —— 撤销历史因此是每个标签独立的。
 */
interface Tab {
  id: string;
  /** null 表示还没保存过的新文档 */
  path: string | null;
  name: string;
  dirty: boolean;
  state: EditorState | null;
  scrollTop: number;
  headings: api.Heading[];
}

let tabs: Tab[] = [];
let activeId = "";
let tabSeq = 0;
/** 程序化改文档（载入 / 外部重载）期间抑制「变脏」+ 重复渲染 */
let suppressChange = false;

/** 侧边栏「当前目录」用的最近一个目录：新建空白标签时不清空列表。 */
let lastDir: string | null = null;

let renderTimer: ReturnType<typeof setTimeout> | undefined;
let renderSeq = 0;
let cursorLine = 1;
/** 目录内容的指纹，避免每次预览渲染都重建成一样的 DOM */
let outlineSignature = "\u0000";
let viewMode: ViewMode = "edit";
let splitRatio = Number(localStorage.getItem(LS.split) ?? "0.5");
if (!Number.isFinite(splitRatio) || splitRatio < 0.15 || splitRatio > 0.85) splitRatio = 0.5;

let fontSize = Number(localStorage.getItem(LS.font) ?? String(FONT_DEFAULT));
if (!Number.isFinite(fontSize)) fontSize = FONT_DEFAULT;

/** 段内单个换行是否硬换行（comrak render.hardbreaks）。默认开。 */
let hardbreaks = localStorage.getItem(LS.hardbreaks) !== "0";

/**
 * 公式是否转成图片。默认关——关着才是矢量渲染，清晰度最好；
 * 只有要把内容粘到 Word / 微信这类不认 KaTeX 的软件时才需要打开。
 */
let mathAsImage = localStorage.getItem(LS.mathimg) === "1";

const MD_FILTER = [{ name: "Markdown", extensions: ["md", "markdown", "mdx", "txt"] }];

/** 「另存为纯文本」用的过滤器。单独一份，避免用户手误把源文档存成 .md 覆盖掉。 */
const TXT_FILTER = [{ name: "纯文本", extensions: ["txt"] }];

/** Tauri 窗口对象。纯浏览器里调试时拿不到，置 null 后各处都会跳过窗口操作。 */
function currentWindow(): ReturnType<typeof getCurrentWindow> | null {
  try {
    return getCurrentWindow();
  } catch {
    return null;
  }
}
const win = currentWindow();

const editor = createEditor({
  parent: editorPaneEl,
  onChange: () => {
    if (suppressChange) return;
    const tab = activeTab();
    if (tab && !tab.dirty) {
      tab.dirty = true;
      updateStatus();
      renderTabstrip();
    }
    scheduleRender();
  },
  onCursor: (line) => {
    cursorLine = line;
    highlightOutline();
  },
  onContextMenu: (x, y, hasSelection) => showContextMenu(x, y, hasSelection),
});

/* ------------------------------ 标签模型 ------------------------------ */
function activeTab(): Tab | null {
  return tabs.find((t) => t.id === activeId) ?? null;
}

/** 新建标签（不激活）。content 会预先建成一份独立的 EditorState。 */
function newTab(path: string | null, name: string, content: string): Tab {
  const tab: Tab = {
    id: `t${++tabSeq}`,
    path,
    name,
    dirty: false,
    state: editor.createState(content),
    scrollTop: 0,
    headings: [],
  };
  tabs.push(tab);
  return tab;
}

/** 把标签的 state 装进 view。setState 不触发 updateListener，外部要自己刷新界面。 */
function mountTab(tab: Tab): void {
  const state = tab.state ?? editor.createState("");
  tab.state = null;
  suppressChange = true;
  editor.setState(state, tab.scrollTop);
  suppressChange = false;
  cursorLine = editor.getCursorLine();
}

function activateTab(id: string, focus = true): void {
  if (!tabs.some((t) => t.id === id)) return;

  if (id === activeId) {
    if (focus && viewMode !== "preview" && findbarEl.hidden) editor.focus();
    return;
  }

  const prev = activeTab();
  if (prev) {
    prev.state = editor.getState();
    prev.scrollTop = editor.getScrollTop();
  }

  activeId = id;
  const tab = activeTab();
  if (!tab) return;
  mountTab(tab);

  renderTabstrip();
  updateStatus();
  renderOutline();
  void refreshDirList();
  saveTabSession();

  void (async () => {
    if (tab.path) {
      try {
        await api.watchDocument(tab.path);
      } catch {
        /* 文件可能已被删除，忽略 */
      }
    }
    await flushRender();
    if (focus && viewMode !== "preview" && findbarEl.hidden) editor.focus();
  })();
}

async function closeTab(id: string): Promise<void> {
  const tab = tabs.find((t) => t.id === id);
  if (!tab) return;

  if (tab.dirty) {
    const ok = await confirmDialog(`${tab.name} 尚未保存，关闭后内容将丢失。`, {
      title: "关闭标签",
      kind: "warning",
    });
    if (!ok) return;
  }

  const index = tabs.indexOf(tab);
  tabs.splice(index, 1);

  // 关掉最后一个：留一个空白标签，免得界面没有任何可编辑区域
  if (tabs.length === 0) {
    const fresh = newTab(null, "未命名", "");
    activeId = "";
    activateTab(fresh.id);
    return;
  }

  if (activeId === id) {
    activeId = "";
    const next = tabs[Math.min(index, tabs.length - 1)];
    activateTab(next.id);
  } else {
    renderTabstrip();
    saveTabSession();
  }
}

function cycleTab(step: number): void {
  if (tabs.length < 2) return;
  const index = tabs.findIndex((t) => t.id === activeId);
  const next = (index + step + tabs.length) % tabs.length;
  activateTab(tabs[next].id);
}

function renderTabstrip(): void {
  docTabsEl.replaceChildren(
    ...tabs.map((tab) => {
      const node = document.createElement("div");
      node.className = "doc-tab";
      node.setAttribute("role", "tab");
      node.setAttribute("aria-selected", String(tab.id === activeId));
      node.dataset.id = tab.id;
      node.title = tab.path ?? `${tab.name}（未保存）`;

      const name = document.createElement("span");
      name.className = "doc-tab-name";
      name.textContent = tab.name;

      const dot = document.createElement("span");
      dot.className = "doc-tab-dirty";
      dot.hidden = !tab.dirty;
      dot.title = "尚未保存";

      const close = document.createElement("button");
      close.type = "button";
      close.className = "doc-tab-close";
      close.textContent = "×";
      close.title = "关闭标签 (Ctrl+W)";
      close.setAttribute("aria-label", `关闭 ${tab.name}`);
      close.addEventListener("mousedown", (event) => event.stopPropagation());
      close.addEventListener("click", (event) => {
        event.stopPropagation();
        void closeTab(tab.id);
      });

      node.append(name, dot, close);
      node.addEventListener("mousedown", (event) => {
        // 中键关闭，和浏览器一致
        if (event.button === 1) {
          event.preventDefault();
          void closeTab(tab.id);
        } else if (event.button === 0 && tab.id !== activeId) {
          activateTab(tab.id);
        }
      });

      if (tab.id === activeId) {
        // 标签多了以后，保证当前标签在可视区内
        requestAnimationFrame(() => {
          const left = node.offsetLeft;
          if (left < docTabsEl.scrollLeft) docTabsEl.scrollLeft = left;
          else if (left + node.offsetWidth > docTabsEl.scrollLeft + docTabsEl.clientWidth) {
            docTabsEl.scrollLeft = left + node.offsetWidth - docTabsEl.clientWidth;
          }
        });
      }
      return node;
    }),
  );
}

/** 记住「开了哪些文件、最后停在哪个」，下次启动恢复（只恢复已保存过的文件）。 */
function saveTabSession(): void {
  const paths = tabs.map((t) => t.path).filter((p): p is string => p !== null);
  const activePath = activeTab()?.path ?? null;
  localStorage.setItem(LS.tabs, JSON.stringify({ paths, activePath }));
}

async function restoreTabs(): Promise<void> {
  let restored = 0;
  let activePath: string | null = null;

  try {
    const raw = JSON.parse(localStorage.getItem(LS.tabs) ?? "null") as
      | { paths?: unknown; activePath?: unknown }
      | null;
    if (raw && Array.isArray(raw.paths)) {
      activePath = typeof raw.activePath === "string" ? raw.activePath : null;
      for (const path of raw.paths) {
        if (typeof path !== "string") continue;
        try {
          const doc = await api.readDocument(path);
          newTab(doc.path, baseName(doc.path), doc.content);
          restored += 1;
        } catch {
          /* 文件被删或没权限：跳过 */
        }
      }
    }
  } catch {
    /* 数据坏了当作没有 */
  }

  if (restored === 0) {
    newTab(null, "未命名", "");
  }

  const preferred = activePath
    ? tabs.find((t) => t.path && samePath(t.path, activePath))
    : undefined;
  activeId = (preferred ?? tabs[0]).id;

  const tab = activeTab();
  if (tab) mountTab(tab);

  renderTabstrip();
  updateStatus();
  await flushRender();
  void refreshDirList();

  if (tab?.path) {
    try {
      await api.watchDocument(tab.path);
    } catch {
      /* 忽略 */
    }
  }
}

/* ------------------------------ 渲染与状态 ------------------------------ */
function scheduleRender(): void {
  if (renderTimer !== undefined) clearTimeout(renderTimer);
  renderTimer = setTimeout(() => {
    renderTimer = undefined;
    void doRender();
  }, 120);
}

async function doRender(): Promise<void> {
  const seq = ++renderSeq;
  const tab = activeTab();
  const source = editor.getDoc();
  try {
    const rendered = await api.renderMarkdown(source, hardbreaks);
    // 渲染是异步的，期间可能又改了文档或切了标签：只认最后一次
    if (seq !== renderSeq) return;

    previewEl.innerHTML = rendered.html;

    // 给预览里的标题按顺序打标，目录点击时据此滚动。
    // 顺序与 Rust 返回的 headings 严格一致（两边都是 comrak 解析出来的）。
    previewEl.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6").forEach((node, index) => {
      node.dataset.hi = String(index);
    });

    // comrak 只标出了公式边界，实际排版在这里做。
    // 勾了「公式转图」时会异步把排好的公式换成位图，所以要等它回来再量高度。
    await renderMath(previewEl, { asImage: mathAsImage, fontPx: fontSize });

    // 渲染期间又改了文档 / 切了标签：这次的结果已经过期，别去改锚点和状态栏
    if (seq !== renderSeq) return;

    // 公式排版（以及转图后的替换）会改高度，锚点必须在它之后重建
    rebuildSyncAnchors();

    const current = activeTab() ?? tab;
    if (current) current.headings = rendered.headings;
    renderOutline();

    lastStat = `${rendered.words} 字 · ${rendered.lines} 行 · ${rendered.headings.length} 个标题`;
    if (flashTimer === undefined) statusStatEl.textContent = lastStat;

    // 内容重排后把预览拉回编辑区的阅读位置（只在分屏时有意义）
    if (viewMode === "split") syncPreviewToEditor();
  } catch (err) {
    if (seq !== renderSeq) return;
    previewEl.innerHTML = `<pre>渲染失败：${escapeHtml(String(err))}</pre>`;
  }
}

/** 立刻渲染（丢掉防抖等待），切到预览相关视图时用。 */
async function flushRender(): Promise<void> {
  if (renderTimer !== undefined) {
    clearTimeout(renderTimer);
    renderTimer = undefined;
  }
  await doRender();
}

/* ------------------------- 分屏滚动联动 -------------------------
 * 不用「滚动百分比」对齐：源码一行可能渲染成一屏（图片、表格、长列表），
 * 按比例同步在长文档里会越滚越偏。这里直接用 comrak 输出的 data-sourcepos
 * —— 每个块自带它在源码里的起止行 —— 在两侧之间按行号换算。
 */
interface SyncAnchor {
  line: number;
  endLine: number;
  /** 相对预览内容原点的像素位置 */
  top: number;
  height: number;
}

let syncAnchors: SyncAnchor[] = [];
/** 谁正在驱动这次滚动：用来吞掉程序性滚动引发的回声，避免两边互相推。 */
let syncDriver: "editor" | "preview" | null = null;

/** "12:1-15:3" → { line: 12, endLine: 15 } */
function parseSourcepos(raw: string | undefined): { line: number; endLine: number } | null {
  if (!raw) return null;
  const m = /^(\d+):\d+-(\d+):\d+$/.exec(raw);
  return m ? { line: Number(m[1]), endLine: Number(m[2]) } : null;
}

function rebuildSyncAnchors(): void {
  const list: SyncAnchor[] = [];
  const seen = new Set<number>();
  // 预览内容原点 = 容器顶 − 当前滚动量
  const originTop = previewPaneEl.getBoundingClientRect().top - previewPaneEl.scrollTop;

  for (const node of previewEl.querySelectorAll<HTMLElement>("[data-sourcepos]")) {
    const range = parseSourcepos(node.dataset.sourcepos);
    if (!range) continue;
    const rect = node.getBoundingClientRect();
    if (rect.height <= 0) continue;
    const top = rect.top - originTop;
    const key = Math.round(top);
    // 父子块的顶端重合（父块第一个子块贴着父块顶），留外层那份就够
    if (seen.has(key)) continue;
    seen.add(key);
    list.push({ line: range.line, endLine: range.endLine, top, height: rect.height });
  }

  list.sort((a, b) => a.top - b.top);
  syncAnchors = list;
}

/** 源码第 line 行大约落在预览的哪个纵向位置。 */
function previewOffsetForLine(line: number): number {
  const list = syncAnchors;
  if (!list.length) return 0;

  // 二分：最后一个起始行 ≤ line 的块
  let lo = 0;
  let hi = list.length - 1;
  let idx = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].line <= line) {
      idx = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }

  const anchor = list[idx];
  const span = Math.max(1, anchor.endLine - anchor.line);
  // 目标行落在块中间就按行数比例插值；超出块尾则停在块底
  const ratio = Math.min(1, Math.max(0, (line - anchor.line) / span));
  return anchor.top + ratio * anchor.height;
}

/** 预览纵向位置 top 对应源码的哪一行（可能是小数，调用方取整）。 */
function lineForPreviewOffset(top: number): number {
  const list = syncAnchors;
  if (!list.length) return 1;

  let lo = 0;
  let hi = list.length - 1;
  let idx = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].top <= top) {
      idx = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }

  const anchor = list[idx];
  const span = Math.max(0, anchor.endLine - anchor.line);
  if (anchor.height <= 0 || span === 0) return anchor.line;
  const ratio = Math.min(1, Math.max(0, (top - anchor.top) / anchor.height));
  return anchor.line + ratio * span;
}

function releaseSyncDriver(): void {
  // 程序性滚动会再冒一次 scroll 事件，等两帧让回声过去再解锁
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      syncDriver = null;
    });
  });
}

/** 编辑区滚动 → 预览跟随。 */
function syncPreviewToEditor(): void {
  if (viewMode !== "split" || previewPaneEl.hidden || !syncAnchors.length) return;
  if (syncDriver === "preview") return;
  syncDriver = "editor";
  previewPaneEl.scrollTop = previewOffsetForLine(editor.getTopLine());
  releaseSyncDriver();
}

/** 预览滚动 → 编辑区跟随。 */
function syncEditorToPreview(): void {
  if (viewMode !== "split" || !syncAnchors.length) return;
  if (syncDriver === "editor") return;
  syncDriver = "preview";
  editor.scrollToLine(lineForPreviewOffset(previewPaneEl.scrollTop));
  releaseSyncDriver();
}

function setupScrollSync(): void {
  editor.view.scrollDOM.addEventListener("scroll", syncPreviewToEditor, { passive: true });
  previewPaneEl.addEventListener("scroll", syncEditorToPreview, { passive: true });
}

function escapeHtml(input: string): string {
  const table: Record<string, string> = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };
  return input.replace(/[&<>"']/g, (ch) => table[ch] ?? ch);
}

let lastTitle = "";

function updateStatus(): void {
  const tab = activeTab();
  statusPathEl.textContent = tab ? (tab.path ?? `${tab.name}（未保存）`) : "未打开文件";
  statusDirtyEl.textContent = tab?.dirty ? "● 未保存" : "";

  const name = tab?.name ?? "未命名";
  const title = `${name}${tab?.dirty ? " *" : ""} - pyMD`;
  appTitleEl.textContent = title;
  // 任务栏 / Alt-Tab 里显示的标题
  if (title !== lastTitle && win) {
    lastTitle = title;
    void win.setTitle(title).catch(() => undefined);
  }
}

/** 在状态栏右侧闪一条提示，4 秒后恢复成字数统计。 */
let flashTimer: ReturnType<typeof setTimeout> | undefined;
let lastStat = "";

function flashStatus(text: string): void {
  statusStatEl.textContent = text;
  if (flashTimer !== undefined) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    flashTimer = undefined;
    statusStatEl.textContent = lastStat;
  }, 4000);
}

/* ------------------------------ 文件操作 ------------------------------ */
function markSaved(tab: Tab): void {
  tab.dirty = false;
  renderTabstrip();
  updateStatus();
}

async function loadFile(path: string): Promise<void> {
  // 已经开着的文件：直接切过去，不重新读盘
  const existing = tabs.find((t) => t.path && samePath(t.path, path));
  if (existing) {
    activateTab(existing.id);
    return;
  }

  try {
    const doc = await api.readDocument(path);
    const tab = newTab(doc.path, baseName(doc.path), doc.content);
    activateTab(tab.id);
    pushRecent(doc.path);
    renderRecent();
    flashStatus(`已打开 ${baseName(doc.path)} · ${doc.encoding} · ${doc.size} B`);
  } catch (err) {
    window.alert(`打开失败：${String(err)}`);
  }
}

async function openFile(): Promise<void> {
  // 现在有标签页了，一次可以选多个（按住 Ctrl / Shift 多选）
  const picked = await openDialog({ multiple: true, directory: false, filters: MD_FILTER });
  if (!picked) return;
  const list = Array.isArray(picked) ? picked : [picked];
  for (const path of list) await loadFile(path);
}

async function saveFile(): Promise<void> {
  const tab = activeTab();
  if (!tab) return;
  if (!tab.path) {
    await saveFileAs();
    return;
  }
  try {
    const bytes = await api.writeDocument(tab.path, editor.getDoc());
    markSaved(tab);
    flashStatus(`已写入 ${bytes} 字节`);
  } catch (err) {
    window.alert(`保存失败：${String(err)}`);
  }
}

async function saveFileAs(): Promise<void> {
  const tab = activeTab();
  if (!tab) return;

  const picked = await saveDialog({
    defaultPath: tab.path ?? `${tab.name}.md`,
    filters: MD_FILTER,
  });
  if (typeof picked !== "string") return;

  try {
    const bytes = await api.writeDocument(picked, editor.getDoc());

    // 目标路径可能已经被别的标签占着：把那个关掉，避免两个标签指向同一文件
    const duplicate = tabs.find((t) => t !== tab && t.path && samePath(t.path, picked));
    if (duplicate) tabs.splice(tabs.indexOf(duplicate), 1);

    tab.path = picked;
    tab.name = baseName(picked);
    markSaved(tab);
    flashStatus(`已另存为 ${picked}（${bytes} 字节）`);
    await api.watchDocument(picked);
    pushRecent(picked);
    renderRecent();
    void refreshDirList();
    saveTabSession();
  } catch (err) {
    window.alert(`另存为失败：${String(err)}`);
  }
}

function newFile(): void {
  const tab = newTab(null, "未命名", "");
  activateTab(tab.id);
}

/* ---------------------------- 导出纯文本 ---------------------------- */

/** 把 Markdown 源码转成纯文本（去掉所有标记）。转换在 Rust 侧做，前端只负责搬运。 */
async function toPlainText(markdown: string): Promise<string> {
  return api.plainText(markdown);
}

/**
 * 另存为纯文本（.txt）。
 *
 * 注意这里**不复用 `saveFileAs`**：那个会把标签的 `path` / `name` 改成新文件，
 * 而「导出纯文本」只是往外扔一份副本，真正的源文档还是原来那个 .md。
 * 如果连标签一起改绑，用户接着按 Ctrl+S 就会把 Markdown 源码以纯文本写回，
 * 标记全丢 —— 这是最容易埋的坑，所以这里只 `writeDocument`，不碰 tab。
 */
async function savePlainTextAs(): Promise<void> {
  const tab = activeTab();
  if (!tab) return;

  // 默认文件名：沿用原标题，只把扩展名换成 .txt
  const stem = (tab.path ?? tab.name).replace(/\.(md|markdown|mdx|txt)$/i, "");
  const picked = await saveDialog({ defaultPath: `${stem}.txt`, filters: TXT_FILTER });
  if (typeof picked !== "string") return;

  try {
    const text = await toPlainText(editor.getDoc());
    const bytes = await api.writeDocument(picked, text);
    flashStatus(`已导出纯文本 ${baseName(picked)}（${bytes} 字节）`);
  } catch (err) {
    window.alert(`导出纯文本失败：${String(err)}`);
  }
}

/**
 * 复制为纯文本：有选区就只转选区，没选区转整篇。
 *
 * 和「复制」的区别：那个原样复制 Markdown 源码，这个先把 `#`、`**`、`|`
 * 这些标记去掉再进剪贴板，粘到聊天窗口 / 普通编辑器里不会是一堆符号。
 */
async function copyPlainText(): Promise<void> {
  const selection = editor.getSelectedText();
  const scoped = selection.length > 0;
  try {
    const text = await toPlainText(scoped ? selection : editor.getDoc());
    if (!text.trim()) {
      flashStatus("没有可复制的文字");
      return;
    }
    await api.clipboardWrite(text);
    flashStatus(`已复制纯文本 ${text.trim().length} 个字符${scoped ? "（选区）" : "（全文）"}`);
  } catch (err) {
    flashStatus(`复制纯文本失败：${String(err)}`);
  }
}

/* ------------------------------- 主题 ------------------------------- */
function applyTheme(theme: "light" | "dark"): void {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(LS.theme, theme);
}

function setupTheme(): void {
  const stored = localStorage.getItem(LS.theme);
  const initial: "light" | "dark" =
    stored === "light" || stored === "dark"
      ? stored
      : window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
  applyTheme(initial);
  el("btn-theme").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
}

/* ------------------------------ 硬换行开关 ------------------------------ */
/** 工具栏的「硬换行」勾选框：段内单个换行是否渲染成 `<br>`（comrak render.hardbreaks）。 */
function setupHardbreaks(): void {
  const box = el<HTMLInputElement>("chk-hardbreaks");
  box.checked = hardbreaks;
  box.addEventListener("change", () => {
    hardbreaks = box.checked;
    localStorage.setItem(LS.hardbreaks, hardbreaks ? "1" : "0");
    flashStatus(hardbreaks ? "单换行按硬换行渲染" : "单换行按 CommonMark 合并成空格");
    void flushRender();
  });
}

/** 「公式转图」勾选框：开了就重排一遍，把公式换成位图。 */
function setupMathImage(): void {
  const box = el<HTMLInputElement>("chk-mathimg");
  box.checked = mathAsImage;
  box.addEventListener("change", () => {
    mathAsImage = box.checked;
    localStorage.setItem(LS.mathimg, mathAsImage ? "1" : "0");
    flashStatus(
      mathAsImage
        ? "公式已转为图片：可直接粘到 Word / 微信 / 邮件"
        : "公式恢复矢量渲染（最清晰）",
    );
    void flushRender();
  });
}

/* ------------------------------ 字号缩放 ------------------------------ */
function applyFontSize(next: number): void {
  fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(next)));
  document.documentElement.style.setProperty("--fs", `${fontSize}px`);
  localStorage.setItem(LS.font, String(fontSize));
  // 字号变了行高跟着变，CodeMirror 需要重新测量
  editor.requestMeasure();
  // 图片是按字号烘死的像素，字号一变就得重转，否则和正文对不上
  if (mathAsImage) void flushRender();
}

function zoomIn(): void {
  applyFontSize(fontSize + 1);
  flashStatus(`字号 ${fontSize}px`);
}

function zoomOut(): void {
  applyFontSize(fontSize - 1);
  flashStatus(`字号 ${fontSize}px`);
}

function zoomReset(): void {
  applyFontSize(FONT_DEFAULT);
  flashStatus(`字号 ${fontSize}px（默认）`);
}

/* ----------------------------- 视图切换 ----------------------------- */
/**
 * 分屏比例。
 *
 * 注意：flex-basis 的百分比是相对 #panes 的宽度算的，而 #panes 里还塞着右侧目录面板，
 * 直接写 50% 会让预览少掉「目录面板」那么多宽度。所以这里换算成像素。
 */
function applySplit(ratio: number): void {
  const outlineWidth = outlinePaneEl.hidden ? 0 : outlinePaneEl.offsetWidth;
  const dividerWidth = dividerEl.hidden ? 0 : dividerEl.offsetWidth;
  const available = panesEl.clientWidth - outlineWidth - dividerWidth;
  if (available <= 0) return;
  editorPaneEl.style.flex = `0 0 ${Math.round(available * ratio)}px`;
}

function setView(mode: ViewMode): void {
  viewMode = mode;

  const showEditor = mode !== "preview";
  const showPreview = mode !== "edit";

  editorPaneEl.hidden = !showEditor;
  previewPaneEl.hidden = !showPreview;
  dividerEl.hidden = mode !== "split";

  // 只有分屏模式按比例分宽度，其余模式各自占满
  if (mode === "split") applySplit(splitRatio);
  else editorPaneEl.style.flex = "1 1 auto";
  previewPaneEl.style.flex = "1 1 auto";

  for (const btn of tabButtons) {
    const active = btn.dataset.view === mode;
    btn.setAttribute("aria-selected", String(active));
    btn.tabIndex = active ? 0 : -1;
  }

  localStorage.setItem(LS.view, mode);

  if (showEditor) {
    // 从隐藏状态显示出来时 CodeMirror 需要重新测量，否则光标/滚动位置会错
    editor.requestMeasure();
    if (mode === "edit" && findbarEl.hidden) editor.focus();
  }
  if (showPreview) void flushRender();
}

function cycleView(): void {
  const order: ViewMode[] = ["edit", "split", "preview"];
  const next = order[(order.indexOf(viewMode) + 1) % order.length];
  setView(next);
}

function setupTabs(): void {
  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.view;
      if (mode === "edit" || mode === "split" || mode === "preview") setView(mode);
    });
  }
  const stored = localStorage.getItem(LS.view);
  setView(stored === "split" || stored === "preview" ? stored : "edit");
}

/* ----------------------------- 分隔条拖拽 ----------------------------- */
function setupDivider(): void {
  let dragging = false;

  dividerEl.addEventListener("mousedown", (event) => {
    if (viewMode !== "split") return;
    event.preventDefault();
    dragging = true;
    document.body.style.cursor = "col-resize";
  });

  window.addEventListener("mousemove", (event) => {
    if (!dragging || viewMode !== "split") return;
    // 比例只在「编辑 + 预览」两栏之间算，右侧的目录面板不参与
    const left = editorPaneEl.getBoundingClientRect().left;
    const total = editorPaneEl.offsetWidth + previewPaneEl.offsetWidth;
    if (total <= 0) return;
    const ratio = Math.min(0.85, Math.max(0.15, (event.clientX - left) / total));
    splitRatio = ratio;
    applySplit(ratio);
  });

  // 窗口变宽变窄时，分屏的像素宽度要跟着重算
  window.addEventListener("resize", () => {
    if (viewMode === "split") applySplit(splitRatio);
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    localStorage.setItem(LS.split, String(splitRatio));
    editor.requestMeasure();
  });
}

/* ------------------------------- 侧边栏 ------------------------------- */
function samePath(a: string, b: string): boolean {
  return a.replace(/\//g, "\\").toLowerCase() === b.replace(/\//g, "\\").toLowerCase();
}

function baseName(path: string): string {
  return path.replace(/\\/g, "/").split("/").pop() ?? path;
}

function currentDir(): string | null {
  const tab = activeTab();
  if (tab?.path) {
    const dir = tab.path.replace(/[\\/][^\\/]*$/, "");
    if (dir && dir !== tab.path) lastDir = dir;
  }
  return lastDir;
}

function hintNode(text: string): HTMLElement {
  const div = document.createElement("div");
  div.className = "side-hint";
  div.textContent = text;
  return div;
}

function fileItem(path: string, name: string): HTMLElement {
  const row = document.createElement("button");
  row.type = "button";
  const tab = activeTab();
  row.className = "file-item" + (tab?.path && samePath(path, tab.path) ? " active" : "");
  row.title = path;

  const icon = document.createElement("span");
  icon.className = "file-icon";
  icon.textContent = (name.split(".").pop() ?? "").toUpperCase().slice(0, 4) || "MD";

  const label = document.createElement("span");
  label.className = "file-name";
  label.textContent = name;

  row.append(icon, label);
  row.addEventListener("click", () => void loadFile(path));
  return row;
}

function loadRecent(): RecentFile[] {
  try {
    const raw = JSON.parse(localStorage.getItem(LS.recent) ?? "[]") as unknown;
    if (!Array.isArray(raw)) return [];
    return raw
      .filter(
        (x): x is RecentFile =>
          typeof x === "object" && x !== null && typeof (x as RecentFile).path === "string",
      )
      .slice(0, MAX_RECENT);
  } catch {
    return [];
  }
}

function pushRecent(path: string): void {
  const list = loadRecent().filter((f) => !samePath(f.path, path));
  list.unshift({ path, name: baseName(path), at: Date.now() });
  localStorage.setItem(LS.recent, JSON.stringify(list.slice(0, MAX_RECENT)));
}

function renderRecent(): void {
  const list = loadRecent();
  recentCountEl.textContent = String(list.length);
  if (list.length === 0) {
    recentListEl.replaceChildren(hintNode("暂无最近打开的文件"));
    return;
  }
  recentListEl.replaceChildren(...list.map((f) => fileItem(f.path, f.name)));
}

async function refreshDirList(): Promise<void> {
  const dir = currentDir();
  if (!dir) {
    dirCountEl.textContent = "0";
    dirListEl.replaceChildren(hintNode("打开文件后显示目录列表"));
    return;
  }

  try {
    const entries = await api.listDir(dir);
    const files = entries
      .filter((e) => !e.isDir && MD_EXT.test(e.name))
      .sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"));
    dirCountEl.textContent = String(files.length);
    dirListEl.replaceChildren(
      ...(files.length === 0
        ? [hintNode("该目录下没有 Markdown 文件")]
        : files.map((f) => fileItem(f.path, f.name))),
    );
  } catch (err) {
    dirCountEl.textContent = "0";
    dirListEl.replaceChildren(hintNode(`目录读取失败：${String(err)}`));
  }
}

function setSidebar(visible: boolean): void {
  sidebarEl.hidden = !visible;
  el("btn-sidebar").setAttribute("aria-pressed", String(visible));
  localStorage.setItem(LS.sidebar, visible ? "1" : "0");
  if (viewMode === "split") applySplit(splitRatio);
  editor.requestMeasure();
}

function setupSidebar(): void {
  setSidebar(localStorage.getItem(LS.sidebar) !== "0");
  el("btn-sidebar").addEventListener("click", () => setSidebar(sidebarEl.hidden));
  renderRecent();
  void refreshDirList();

  // 分组折叠：aria-expanded 记录的是「当前是否展开」，点击后取反
  const groups: Array<[string, string]> = [
    ["dir-title", "dir-list"],
    ["recent-title", "recent-list"],
  ];
  for (const [titleId, listId] of groups) {
    const title = el<HTMLButtonElement>(titleId);
    const list = el<HTMLElement>(listId);
    title.addEventListener("click", () => {
      const expanded = title.getAttribute("aria-expanded") !== "false";
      title.setAttribute("aria-expanded", String(!expanded));
      list.hidden = expanded;
    });
  }
}

/* ------------------------------ 文档目录 ------------------------------ */
/* 标题来自 Rust 的 render_markdown（comrak 同一套解析规则），带 1 基行号。
 * 点一行 = 编辑器跳到那一行 + 预览滚到对应标题；光标移动时高亮所属章节。 */
function outlineHint(text: string): HTMLElement {
  const div = document.createElement("div");
  div.className = "outline-empty";
  div.textContent = text;
  return div;
}

function renderOutline(): void {
  const headings = activeTab()?.headings ?? [];
  const signature = `${activeId}|${headings.map((h) => `${h.level}:${h.line}:${h.text}`).join("|")}`;

  outlineCountEl.textContent = String(headings.length);

  // 每次渲染都重建 DOM 太浪费（打字时 120ms 一次），标题没变就只更新高亮
  if (signature === outlineSignature) {
    highlightOutline();
    return;
  }
  outlineSignature = signature;

  if (headings.length === 0) {
    outlineListEl.replaceChildren(outlineHint("当前文档没有标题"));
    return;
  }

  outlineListEl.replaceChildren(
    ...headings.map((heading, index) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `outline-item lv${heading.level}`;
      btn.textContent = heading.text;
      btn.title = `${"#".repeat(heading.level)} ${heading.text}（第 ${heading.line} 行）`;
      btn.style.paddingLeft = `${6 + (heading.level - 1) * 10}px`;
      btn.addEventListener("click", () => jumpToHeading(index));
      return btn;
    }),
  );

  highlightOutline();
}

function jumpToHeading(index: number): void {
  const heading = (activeTab()?.headings ?? [])[index];
  if (!heading) return;

  editor.gotoLine(heading.line);
  if (viewMode !== "edit") {
    previewEl
      .querySelector<HTMLElement>(`[data-hi="${index}"]`)
      ?.scrollIntoView({ block: "start" });
  }
  highlightOutline(index);
}

function highlightOutline(forcedIndex?: number): void {
  const items = outlineListEl.querySelectorAll<HTMLElement>(".outline-item");
  if (items.length === 0) return;

  let activeIndex = forcedIndex;
  if (activeIndex === undefined) {
    const headings = activeTab()?.headings ?? [];
    for (let i = 0; i < headings.length; i += 1) {
      if (headings[i].line <= cursorLine) activeIndex = i;
      else break;
    }
  }

  items.forEach((item, i) => item.classList.toggle("active", i === activeIndex));

  if (activeIndex !== undefined && items[activeIndex]) {
    const node = items[activeIndex];
    const top = node.offsetTop;
    const bottom = top + node.offsetHeight;
    if (top < outlineListEl.scrollTop) outlineListEl.scrollTop = top - 8;
    else if (bottom > outlineListEl.scrollTop + outlineListEl.clientHeight) {
      outlineListEl.scrollTop = bottom - outlineListEl.clientHeight + 8;
    }
  }
}

function setOutline(visible: boolean): void {
  outlinePaneEl.hidden = !visible;
  el("btn-outline").setAttribute("aria-pressed", String(visible));
  localStorage.setItem(LS.outline, visible ? "1" : "0");
  // 目录占了宽度，分屏的像素宽度要重算
  if (viewMode === "split") applySplit(splitRatio);
  editor.requestMeasure();
  if (visible) highlightOutline();
}

function setupOutline(): void {
  setOutline(localStorage.getItem(LS.outline) !== "0");
  el("btn-outline").addEventListener("click", () => setOutline(outlinePaneEl.hidden));
  el("outline-close").addEventListener("click", () => setOutline(false));
}

/* ---------------------------- 查找与替换 ---------------------------- */
function isFindBarOpen(): boolean {
  return !findbarEl.hidden;
}

function openFindBar(replaceMode: boolean): void {
  findbarEl.hidden = false;
  replaceFieldEl.hidden = !replaceMode;
  findStatusEl.textContent = "";

  // 选中的一段文字自动填进查找框，省一次复制粘贴
  const selected = editor.getSelectedText();
  if (selected && !selected.includes("\n")) findInputEl.value = selected;

  findInputEl.focus();
  findInputEl.select();
}

function closeFindBar(): void {
  if (!isFindBarOpen()) return;
  findbarEl.hidden = true;
  findStatusEl.textContent = "";
  // 高亮跟着查找栏一起收掉，否则关掉后文档上还留着黄底
  editor.clearSearch();
  if (viewMode !== "preview") editor.focus();
}

function runFind(): void {
  const result = editor.find(findInputEl.value);
  if (!result.ok) {
    findStatusEl.textContent =
      result.reason === "empty" ? "请输入要查找的内容" : "没有找到匹配内容";
    return;
  }
  findStatusEl.textContent =
    result.count === 1 ? "已定位到 1 处匹配" : `已定位，共 ${result.count} 处匹配`;
}

function runReplace(): void {
  const result = editor.replace(findInputEl.value, replaceInputEl.value);
  if (!result.ok) {
    findStatusEl.textContent =
      result.reason === "empty" ? "请输入要替换的查找内容" : "没有找到可替换的内容";
    return;
  }
  findStatusEl.textContent = `已替换当前匹配，全文原有 ${result.count} 处`;
}

function runReplaceAll(): void {
  const result = editor.replaceAll(findInputEl.value, replaceInputEl.value);
  if (!result.ok) {
    findStatusEl.textContent =
      result.reason === "empty" ? "请输入要替换的查找内容" : "没有找到可替换的内容";
    return;
  }
  findStatusEl.textContent = `已全部替换，共处理 ${result.count} 处匹配`;
}

function setupFindBar(): void {
  el("btn-find").addEventListener("click", () => openFindBar(false));
  el("btn-replace").addEventListener("click", () => openFindBar(true));
  el("find-next").addEventListener("click", runFind);
  el("replace-one").addEventListener("click", runReplace);
  el("replace-all").addEventListener("click", runReplaceAll);
  el("find-close").addEventListener("click", closeFindBar);

  findInputEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      runFind();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeFindBar();
    }
  });

  replaceInputEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) runReplaceAll();
      else runReplace();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeFindBar();
    }
  });
}

/* ------------------------------ 菜单栏 ------------------------------ */
type ActionId =
  | "newFile"
  | "openFile"
  | "saveFile"
  | "saveAs"
  | "saveAsText"
  | "closeTab"
  | "closeWindow"
  | "undo"
  | "redo"
  | "cut"
  | "copy"
  | "copyPlainText"
  | "paste"
  | "selectAll"
  | "find"
  | "replace"
  | "viewEdit"
  | "viewSplit"
  | "viewPreview"
  | "toggleSidebar"
  | "toggleOutline"
  | "zoomIn"
  | "zoomOut"
  | "zoomReset"
  | "about"
  | "shortcuts";

interface MenuEntry {
  label: string;
  action: ActionId;
  shortcut?: string;
}
/** null 表示分隔线 */
type MenuItem = MenuEntry | null;

const MENUS: Array<{ key: string; label: string; items: MenuItem[] }> = [
  {
    key: "file",
    label: "文件",
    items: [
      { label: "新建", action: "newFile", shortcut: "Ctrl+N" },
      { label: "打开", action: "openFile", shortcut: "Ctrl+O" },
      null,
      { label: "保存", action: "saveFile", shortcut: "Ctrl+S" },
      { label: "另存为", action: "saveAs", shortcut: "Ctrl+Shift+S" },
      { label: "导出为纯文本 (.txt)", action: "saveAsText" },
      null,
      { label: "关闭标签", action: "closeTab", shortcut: "Ctrl+W" },
      { label: "关闭窗口", action: "closeWindow" },
    ],
  },
  {
    key: "edit",
    label: "编辑",
    items: [
      { label: "撤销", action: "undo", shortcut: "Ctrl+Z" },
      { label: "重做", action: "redo", shortcut: "Ctrl+Y" },
      null,
      { label: "剪切", action: "cut", shortcut: "Ctrl+X" },
      { label: "复制", action: "copy", shortcut: "Ctrl+C" },
      { label: "复制为纯文本", action: "copyPlainText" },
      { label: "粘贴", action: "paste", shortcut: "Ctrl+V" },
      { label: "全选", action: "selectAll", shortcut: "Ctrl+A" },
      null,
      { label: "查找", action: "find", shortcut: "Ctrl+F" },
      { label: "替换", action: "replace", shortcut: "Ctrl+H" },
    ],
  },
  {
    key: "view",
    label: "查看",
    items: [
      { label: "文本模式", action: "viewEdit", shortcut: "Ctrl+1" },
      { label: "左右模式", action: "viewSplit", shortcut: "Ctrl+2" },
      { label: "预览模式", action: "viewPreview", shortcut: "Ctrl+3" },
      null,
      { label: "显示/隐藏侧边栏", action: "toggleSidebar", shortcut: "Ctrl+B" },
      { label: "显示/隐藏目录", action: "toggleOutline", shortcut: "Ctrl+Shift+O" },
      null,
      { label: "放大字体", action: "zoomIn", shortcut: "Ctrl+=" },
      { label: "缩小字体", action: "zoomOut", shortcut: "Ctrl+-" },
      { label: "重置字体大小", action: "zoomReset", shortcut: "Ctrl+0" },
    ],
  },
  {
    key: "help",
    label: "帮助",
    items: [
      { label: "关于 pyMD", action: "about" },
      { label: "快捷键说明", action: "shortcuts" },
    ],
  },
];

function showInfo(title: string, body: string): void {
  infoTitleEl.textContent = title;
  infoBodyEl.classList.remove("about-body");
  infoBodyEl.textContent = body;
  infoDialogEl.showModal();
}

/* ---------- 关于 / 版权信息 ----------
 * 版权并入「关于」，不单独占一个菜单项：多一项只有两行字不划算，也容易和
 * 「关于 pyMD」重复。几点取舍：
 *   1. 邮箱不整串写进源码，拆成两段在运行时拼接 —— 挡的是仓库 / 二进制里的
 *      随手 grep 和爬虫，不是加密（界面上终究要显示出来）；
 *   2. 不生成 mailto 链接：WebView2 里点 mailto 要外部协议授权，默认会被拦，
 *      点了没反应反而更差，所以改成「复制」按钮，走已有的 Rust 剪贴板命令；
 *   3. 对话框只放本机静态信息，不发任何网络请求，也不带机器名 / 路径。 */
// 构建期从 package.json 注入（见 vite.config.ts）。以前这里手写一份，升级时必漏。
const APP_VERSION = __APP_VERSION__;
const AUTHOR = "Pyrrhus";
const COPYRIGHT_YEAR = 2026;
const MAILBOX = ["zhangxuefeng", "batonsoft.com"];
const CONTACT_EMAIL = `${MAILBOX[0]}@${MAILBOX[1]}`;
const COPYRIGHT_LINE = `© ${COPYRIGHT_YEAR} ${AUTHOR} · 保留所有权利`;

/** 建节点的小工具（区别于按 id 取节点的 el） */
function mk<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function buildAboutBody(): HTMLElement {
  const box = mk("div", "about");

  const head = mk("div", "about-head");
  const logo = document.createElement("img");
  logo.className = "about-logo";
  logo.src = appIconUrl;
  logo.alt = "pyMD 图标";
  logo.width = 46;
  logo.height = 46;
  const titles = mk("div", "about-titles");
  titles.append(mk("strong", "about-name", "pyMD"), mk("span", "about-ver", `v${APP_VERSION}`));
  head.append(logo, titles);
  box.append(head);

  const intro = [
    "一个用 Tauri 2 + Rust + CodeMirror 6 写的轻量 Markdown 编辑器。",
    "多文件标签页（各自独立的缓冲与撤销栈）、右侧文档目录、三模式视图。",
    "Markdown 渲染、标题提取、文件读写、目录读取、文件监听都在 Rust 侧完成，公式由后端的 comrak 标注边界、前端 KaTeX（含 mhchem）排版。",
  ];
  for (const line of intro) box.append(mk("p", "about-text", line));

  box.append(mk("hr", "about-rule"));

  const meta = mk("dl", "about-meta");
  meta.append(mk("dt", undefined, "作者"), mk("dd", undefined, AUTHOR));

  const mailDd = mk("dd", "about-mail-row");
  const copyBtn = mk("button", "about-copy", "复制");
  copyBtn.type = "button";
  copyBtn.title = "复制邮箱到剪贴板";
  copyBtn.addEventListener("click", async () => {
    try {
      await api.clipboardWrite(CONTACT_EMAIL);
      copyBtn.textContent = "已复制";
      copyBtn.classList.add("done");
    } catch (err) {
      copyBtn.textContent = "复制失败";
      console.warn("复制邮箱失败", err);
    }
    window.setTimeout(() => {
      copyBtn.textContent = "复制";
      copyBtn.classList.remove("done");
    }, 1600);
  });
  mailDd.append(mk("span", "about-mail", CONTACT_EMAIL), copyBtn);
  meta.append(mk("dt", undefined, "邮箱"), mailDd);
  box.append(meta);

  box.append(mk("p", "about-copyright", COPYRIGHT_LINE));
  box.append(mk("p", "about-dim", "界面布局与三模式切换参考了开源项目 MDPad（MIT）。"));

  return box;
}

function showAbout(): void {
  infoTitleEl.textContent = "关于";
  const box = buildAboutBody();
  infoBodyEl.replaceChildren(box);
  infoBodyEl.classList.add("about-body");
  infoDialogEl.showModal();
  void syncAboutVersion(box);
}

/** 版本号以 Rust 侧 CARGO_PKG_VERSION 为准：常量只是拿不到后端时的兜底 */
async function syncAboutVersion(box: HTMLElement): Promise<void> {
  const verEl = box.querySelector<HTMLElement>(".about-ver");
  if (!verEl) return;
  try {
    const info = await api.appInfo();
    const found = /\d+\.\d+\.\d+/.exec(info);
    if (found) verEl.textContent = `v${found[0]}`;
  } catch {
    /* 纯浏览器里打开时没有 Rust 侧，保留常量 */
  }
}

const SHORTCUT_TEXT = [
  `Ctrl+N   新建标签`,
  `Ctrl+O   打开（可多选，每个文件一个标签）`,
  `Ctrl+S   保存`,
  `Ctrl+Shift+S   另存为`,
  `Ctrl+W   关闭当前标签`,
  `Ctrl+Tab / Ctrl+Shift+Tab   切换标签`,
  `Ctrl+F   查找`,
  `Ctrl+H   替换`,
  `Ctrl+1   文本模式`,
  `Ctrl+2   左右模式`,
  `Ctrl+3   预览模式`,
  `Ctrl+B   显示/隐藏侧边栏`,
  `Ctrl+Shift+O   显示/隐藏目录`,
  `Ctrl+E   循环切换视图模式`,
  `Ctrl+=   放大字体`,
  `Ctrl+-   缩小字体`,
  `Ctrl+0   重置字体大小`,
  `Esc      关闭查找栏 / 菜单`,
].join("\n");

const actions: Record<ActionId, () => void | Promise<void>> = {
  newFile,
  openFile,
  saveFile,
  saveAs: saveFileAs,
  saveAsText: savePlainTextAs,
  closeTab: () => {
    if (activeId) void closeTab(activeId);
  },
  closeWindow: () => requestClose(),
  undo: () => void editor.undo(),
  redo: () => void editor.redo(),
  cut: async () => {
    const text = editor.getSelectedText();
    if (!text) {
      flashStatus("请先选中要剪切的内容");
      return;
    }
    try {
      await api.clipboardWrite(text);
      editor.deleteSelection();
    } catch (err) {
      flashStatus(`剪切失败：${String(err)}`);
    }
  },
  copy: async () => {
    const text = editor.getSelectedText();
    if (!text) {
      flashStatus("请先选中要复制的内容");
      return;
    }
    try {
      await api.clipboardWrite(text);
      flashStatus(`已复制 ${text.length} 个字符`);
    } catch (err) {
      flashStatus(`复制失败：${String(err)}`);
    }
  },
  copyPlainText,
  paste: async () => {
    try {
      const text = await api.clipboardRead();
      if (!text) {
        flashStatus("剪贴板里没有文本");
        return;
      }
      editor.insertText(text);
    } catch (err) {
      flashStatus(`粘贴失败：${String(err)}`);
    }
  },
  selectAll: () => void editor.selectAll(),
  find: () => openFindBar(false),
  replace: () => openFindBar(true),
  viewEdit: () => setView("edit"),
  viewSplit: () => setView("split"),
  viewPreview: () => setView("preview"),
  toggleSidebar: () => setSidebar(sidebarEl.hidden),
  toggleOutline: () => setOutline(outlinePaneEl.hidden),
  zoomIn,
  zoomOut,
  zoomReset,
  about: () => showAbout(),
  shortcuts: () => showInfo("快捷键说明", SHORTCUT_TEXT),
};

let openMenuEl: HTMLElement | null = null;

function closeMenus(): void {
  if (!openMenuEl) return;
  const list = openMenuEl.querySelector<HTMLElement>(".dropdown-list");
  if (list) list.hidden = true;
  openMenuEl.classList.remove("open");
  openMenuEl = null;
}

function openMenu(wrap: HTMLElement): void {
  if (openMenuEl === wrap) return;
  closeMenus();
  const list = wrap.querySelector<HTMLElement>(".dropdown-list");
  if (!list) return;
  list.hidden = false;
  wrap.classList.add("open");
  openMenuEl = wrap;
}

function menuEntryNode(entry: MenuEntry, disabled = false): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "dropdown-item";
  btn.disabled = disabled;

  const name = document.createElement("span");
  name.textContent = entry.label;
  btn.append(name);

  if (entry.shortcut) {
    const sc = document.createElement("span");
    sc.className = "dropdown-shortcut";
    sc.textContent = entry.shortcut;
    btn.append(sc);
  }
  return btn;
}

function buildMenus(): void {
  for (const def of MENUS) {
    const wrap = document.createElement("div");
    wrap.className = "dropdown";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "dropdown-trigger";
    trigger.textContent = def.label;
    trigger.setAttribute("aria-haspopup", "true");

    const list = document.createElement("div");
    list.className = "dropdown-list";
    list.hidden = true;

    for (const item of def.items) {
      if (item === null) {
        const sep = document.createElement("div");
        sep.className = "dropdown-sep";
        list.append(sep);
        continue;
      }
      const btn = menuEntryNode(item);
      btn.addEventListener("click", () => {
        closeMenus();
        void actions[item.action]();
      });
      list.append(btn);
    }

    trigger.addEventListener("click", () => {
      if (openMenuEl === wrap) closeMenus();
      else openMenu(wrap);
    });

    // 已经展开一个菜单时，鼠标滑到别的菜单上直接切过去（Windows 菜单的常规行为）
    wrap.addEventListener("mouseenter", () => {
      if (openMenuEl && openMenuEl !== wrap) openMenu(wrap);
    });

    wrap.append(trigger, list);
    menubarEl.append(wrap);
  }

  document.addEventListener("mousedown", (event) => {
    const target = event.target as Node;
    if (openMenuEl && !openMenuEl.contains(target)) closeMenus();
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenus();
  });
}

/* ------------------------------ 右键菜单 ------------------------------ */
function hideContextMenu(): void {
  ctxMenuEl.hidden = true;
}

function showContextMenu(x: number, y: number, hasSelection: boolean): void {
  closeMenus();
  const editMenu = MENUS.find((m) => m.key === "edit");
  if (!editMenu) return;

  ctxMenuEl.replaceChildren();
  for (const item of editMenu.items) {
    if (item === null) {
      const sep = document.createElement("div");
      sep.className = "dropdown-sep";
      ctxMenuEl.append(sep);
      continue;
    }
    const needsSelection = item.action === "cut" || item.action === "copy";
    const btn = menuEntryNode(item, needsSelection && !hasSelection);
    btn.addEventListener("click", () => {
      hideContextMenu();
      void actions[item.action]();
    });
    ctxMenuEl.append(btn);
  }

  // 先摆出来才能量到尺寸，再据此贴边收一收，避免菜单跑出窗口
  ctxMenuEl.hidden = false;
  const rect = ctxMenuEl.getBoundingClientRect();
  const left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 4));
  const top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 4));
  ctxMenuEl.style.left = `${left}px`;
  ctxMenuEl.style.top = `${top}px`;
}

function setupContextMenu(): void {
  document.addEventListener("mousedown", (event) => {
    if (ctxMenuEl.contains(event.target as Node)) return;
    hideContextMenu();
  });
  window.addEventListener("blur", hideContextMenu);
  window.addEventListener("resize", hideContextMenu);
}

/* ---------------------------- 窗口按钮与关闭 ---------------------------- */
const ICON_MAXIMIZE =
  '<svg class="win-icon" viewBox="0 0 16 16" aria-hidden="true"><rect x="4.5" y="4.5" width="7" height="7" /></svg>';
const ICON_RESTORE =
  '<svg class="win-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M6.5 4.5h5v5" /><rect x="4.5" y="6.5" width="5" height="5" /></svg>';

function paintMaxButton(maximized: boolean): void {
  maxButtonEl.innerHTML = maximized ? ICON_RESTORE : ICON_MAXIMIZE;
  const label = maximized ? "还原" : "最大化";
  maxButtonEl.setAttribute("aria-label", label);
  maxButtonEl.title = label;
}

async function requestClose(): Promise<void> {
  const unsaved = tabs.filter((t) => t.dirty);
  if (unsaved.length > 0) {
    const who =
      unsaved.length === 1 ? `「${unsaved[0].name}」尚未保存` : `有 ${unsaved.length} 个标签尚未保存`;
    const ok = await confirmDialog(`${who}，关闭后内容将丢失。`, {
      title: "关闭窗口",
      kind: "warning",
    });
    if (!ok) return;
  }
  // destroy 绕过 closeRequested，避免和下面的监听互相调
  if (win) await win.destroy();
}

function setupWindowControls(): void {
  el("win-min").addEventListener("click", () => {
    if (win) void win.minimize().catch(() => undefined);
  });
  el("win-close").addEventListener("click", () => void requestClose());

  if (!win) return;
  const w = win;

  maxButtonEl.addEventListener("click", () => {
    void (async () => {
      try {
        await w.toggleMaximize();
        paintMaxButton(await w.isMaximized());
      } catch {
        /* 窗口已销毁时忽略 */
      }
    })();
  });

  void w
    .isMaximized()
    .then(paintMaxButton)
    .catch(() => undefined);
  void w
    .onResized(() => {
      void w
        .isMaximized()
        .then(paintMaxButton)
        .catch(() => undefined);
    })
    .catch(() => undefined);

  // Alt+F4 之类的系统关闭也走同一套「是否保存」判断
  void w
    .onCloseRequested((event) => {
      event.preventDefault();
      void requestClose();
    })
    .catch(() => undefined);
}

function setupTitlebarDrag(): void {
  titlebarEl.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    const target = event.target as HTMLElement;
    // 按钮和菜单区域不拖动，否则点菜单会变成拖窗口
    if (target.closest("button, .dropdown")) return;
    if (win) void win.startDragging().catch(() => undefined);
  });
}

/* ------------------------------ 外部变更 ------------------------------ */
async function setupFileWatcher(): Promise<void> {
  await listen<string>("file-changed", async (event) => {
    const tab = tabs.find((t) => t.path && samePath(t.path, event.payload));
    if (!tab) return;

    if (tab.dirty) {
      flashStatus(`${tab.name} 已被外部修改（本地有未保存改动，未自动重载）`);
      return;
    }

    try {
      const doc = await api.readDocument(event.payload);
      if (tab.id === activeId) {
        suppressChange = true;
        editor.setDoc(doc.content);
        suppressChange = false;
        tab.headings = [];
        updateStatus();
        await doRender();
      } else {
        // 后台标签：直接换掉它的 state，等内容都保留
        tab.state = editor.createState(doc.content);
        tab.headings = [];
        renderTabstrip();
      }
      flashStatus("已从磁盘重新载入");
    } catch (err) {
      console.warn("重新载入失败", err);
    }
  });
}

async function setupDragDrop(): Promise<void> {
  try {
    await getCurrentWebview().onDragDropEvent(async (event) => {
      if (event.payload.type !== "drop") return;
      // 支持一次拖多个文件，每个开一个标签
      for (const path of event.payload.paths) {
        if (MD_EXT.test(path)) await loadFile(path);
      }
    });
  } catch (err) {
    console.warn("拖拽打开未启用：", err);
  }
}

/* ------------------------------- 启动 ------------------------------- */
function setupShortcuts(): void {
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (isFindBarOpen()) {
        event.preventDefault();
        closeFindBar();
      }
      hideContextMenu();
      return;
    }

    if (!(event.ctrlKey || event.metaKey)) return;
    const key = event.key.toLowerCase();

    if (key === "s") {
      event.preventDefault();
      if (event.shiftKey) void saveFileAs();
      else void saveFile();
    } else if (key === "o" && event.shiftKey) {
      event.preventDefault();
      setOutline(outlinePaneEl.hidden);
    } else if (key === "o") {
      event.preventDefault();
      void openFile();
    } else if (key === "w") {
      event.preventDefault();
      if (activeId) void closeTab(activeId);
    } else if (key === "tab") {
      event.preventDefault();
      cycleTab(event.shiftKey ? -1 : 1);
    } else if (key === "n" || key === "t") {
      event.preventDefault();
      newFile();
    } else if (key === "f") {
      event.preventDefault();
      openFindBar(false);
    } else if (key === "h") {
      event.preventDefault();
      openFindBar(true);
    } else if (key === "1") {
      event.preventDefault();
      setView("edit");
    } else if (key === "2") {
      event.preventDefault();
      setView("split");
    } else if (key === "3") {
      event.preventDefault();
      setView("preview");
    } else if (key === "b") {
      event.preventDefault();
      setSidebar(sidebarEl.hidden);
    } else if (key === "e") {
      event.preventDefault();
      cycleView();
    } else if (key === "=" || key === "+") {
      event.preventDefault();
      zoomIn();
    } else if (key === "-") {
      event.preventDefault();
      zoomOut();
    } else if (key === "0") {
      event.preventDefault();
      zoomReset();
    }
  });
}

async function boot(): Promise<void> {
  applyFontSize(fontSize);
  setupTheme();
  setupHardbreaks();
  setupMathImage();
  // 左上角图标与关于页共用同一份打包资源
  titlebarIconEl.src = appIconUrl;
  buildMenus();
  setupTitlebarDrag();
  setupWindowControls();
  setupContextMenu();
  setupTabs();
  setupDivider();
  setupScrollSync();
  setupSidebar();
  setupOutline();
  setupFindBar();
  setupShortcuts();

  el("btn-new").addEventListener("click", newFile);
  el("tab-new").addEventListener("click", newFile);
  el("btn-open").addEventListener("click", () => void openFile());
  el("btn-save").addEventListener("click", () => void saveFile());
  el("btn-save-as").addEventListener("click", () => void saveFileAs());

  // 预览里的链接不在 WebView 内导航，先只提示 href
  previewEl.addEventListener("click", (event) => {
    const anchor = (event.target as HTMLElement).closest("a");
    if (!anchor) return;
    event.preventDefault();
    flashStatus(`链接：${anchor.getAttribute("href") ?? ""}（接入 opener 插件后可直接在系统浏览器打开）`);
  });

  // 恢复上次打开的标签（只恢复已保存过的文件，草稿不落盘）
  await restoreTabs();

  try {
    console.info(await api.appInfo());
  } catch (err) {
    console.warn("Rust 侧未就绪（在纯浏览器里打开时会这样）", err);
  }

  await setupFileWatcher();
  await setupDragDrop();

  // 支持从命令行传一个文件路径进来：pymd.exe README.md
  const fromArgv = new URLSearchParams(location.search).get("file");
  if (fromArgv) await loadFile(fromArgv);

  // 自动化测试用的句柄：只有显式写入 pymd-debug=1 才会挂出来。
  // 正常启动时 window.__pymd 不存在，不给页面留操作编辑器内部的入口。
  if (localStorage.getItem("pymd-debug") === "1") {
    Object.defineProperty(window, "__pymd", {
      value: {
        editor,
        tabs: () => tabs,
        activeTab,
        viewMode: () => viewMode,
      },
      configurable: true,
    });
  }
}

void boot();
