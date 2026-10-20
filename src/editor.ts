/**
 * CodeMirror 6 编辑器的封装。
 *
 * CodeMirror 6 是「状态驱动」的：所有改动都通过 view.dispatch() 派发，
 * 状态变化后由 updateListener 通知外部。不要直接改 DOM。
 *
 * 查找/替换走 @codemirror/search 的 SearchQuery + 命令函数：
 * 先把查询写进编辑器状态（setSearchQuery），再调 findNext / replaceNext / replaceAll。
 * 内置的搜索面板（searchKeymap / openSearchPanel）不用，界面由我们自己画。
 */
import { EditorSelection, EditorState, type Extension, type Range } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  ViewPlugin,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentWithTab,
  redo,
  selectAll,
  undo,
} from "@codemirror/commands";
import {
  HighlightStyle,
  bracketMatching,
  foldGutter,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import {
  SearchQuery,
  findNext,
  getSearchQuery,
  highlightSelectionMatches,
  replaceAll as replaceAllCommand,
  replaceNext,
  search,
  setSearchQuery,
} from "@codemirror/search";

/**
 * 自定义高亮样式。颜色用 CSS 变量，明暗主题切换时不用重建编辑器。
 */
const mdHighlightStyle = HighlightStyle.define([
  { tag: tags.heading, fontWeight: "600", color: "var(--md-heading)" },
  { tag: tags.strong, fontWeight: "700" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: tags.strikethrough, textDecoration: "line-through" },
  { tag: tags.link, color: "var(--md-link)", textDecoration: "underline" },
  { tag: tags.url, color: "var(--md-link)" },
  { tag: [tags.monospace, tags.string], color: "var(--md-code)" },
  { tag: tags.quote, color: "var(--md-quote)", fontStyle: "italic" },
  { tag: tags.list, color: "var(--md-list)" },
  { tag: tags.contentSeparator, color: "var(--text-dim)" },
]);

/** 查找类命令的统一返回。`reason` 只在 ok=false 时有意义。 */
export interface SearchResult {
  ok: boolean;
  reason: "empty" | "nomatch" | "unavailable";
  /** 命中数量：find 时为总匹配数；replace 时为替换前的总匹配数 */
  count: number;
}

export interface EditorHandle {
  view: EditorView;
  getDoc(): string;
  setDoc(text: string): void;
  focus(): void;
  /** 容器从 display:none 恢复显示后，让 CodeMirror 重新测量布局。 */
  requestMeasure(): void;

  /* 多标签：整个 EditorState 换进换出（缓冲 + 撤销栈 + 选区都在 state 里） */
  createState(doc: string): EditorState;
  getState(): EditorState;
  setState(state: EditorState, scrollTop?: number): void;
  getScrollTop(): number;
  /** 把光标送到指定行（1 基）并滚动到可见区。 */
  gotoLine(line: number): void;
  /** 当前主选区光标所在行（1 基）。 */
  getCursorLine(): number;
  /** 视口顶部那一行的行号（1 基）。软换行不影响，返回的仍是逻辑行。 */
  getTopLine(): number;
  /** 把第 line 行顶到视口最上面（只滚动，不动光标、不抢焦点）。 */
  scrollToLine(line: number): void;

  /* 查找替换 */
  find(text: string): SearchResult;
  replace(findText: string, replaceText: string): SearchResult;
  replaceAll(findText: string, replaceText: string): SearchResult;
  /** 清掉当前查询（关掉查找栏时用，避免高亮一直留在文档上）。 */
  clearSearch(): void;

  /* 编辑菜单用 */
  undo(): boolean;
  redo(): boolean;
  selectAll(): boolean;
  /** 删除当前选区（剪切在 Rust 把文本写进剪贴板之后调用）。 */
  deleteSelection(): boolean;
  /** 在光标处插入文本（粘贴用），有选区则替换选区。 */
  insertText(text: string): void;
  /** 当前主选区是否选中了内容（右键菜单据此灰掉剪切/复制）。 */
  hasSelection(): boolean;
  /** 当前选中的纯文本，打开查找栏时用来预填。 */
  getSelectedText(): string;
}

const FAIL = (reason: SearchResult["reason"]): SearchResult => ({ ok: false, reason, count: 0 });

/* ------------------------- 查找命中的高亮 -------------------------
 * 内置的 searchHighlighter 只在 CodeMirror 自带搜索面板打开时才画高亮
 * （它内部判断 `state.panel`）。我们不用那个面板，所以自己写一个等价的装饰插件：
 * 拿当前查询在可视区里逐个匹配，命中的加淡色底，当前选中的那个再加描边。
 */
const findMark = Decoration.mark({ class: "cm-pyFind" });
const findMarkActive = Decoration.mark({ class: "cm-pyFind cm-pyFind-active" });

function buildFindDecorations(view: EditorView): DecorationSet {
  const query = getSearchQuery(view.state);
  if (!query.search) return Decoration.none;

  const ranges: Range<Decoration>[] = [];
  const selection = view.state.selection.main;
  for (const { from, to } of view.visibleRanges) {
    const cursor = query.getCursor(view.state, from, to);
    for (let step = cursor.next(); !step.done; step = cursor.next()) {
      const match = step.value;
      const active = match.from === selection.from && match.to === selection.to;
      ranges.push((active ? findMarkActive : findMark).range(match.from, match.to));
    }
  }
  return Decoration.set(ranges, true);
}

const findHighlighter = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = buildFindDecorations(view);
    }
    update(update: ViewUpdate) {
      if (
        update.docChanged ||
        update.selectionSet ||
        update.viewportChanged ||
        // 只改了查询（setSearchQuery 是个 effect）时也要重画
        update.transactions.some((tr) => tr.effects.length > 0)
      ) {
        this.decorations = buildFindDecorations(update.view);
      }
    }
  },
  { decorations: (plugin) => plugin.decorations },
);

export function createEditor(options: {
  parent: HTMLElement;
  onChange: () => void;
  /** 右键（含 mousedown 右键）时回调，坐标是客户端坐标。 */
  onContextMenu?: (x: number, y: number, hasSelection: boolean) => void;
  /** 光标/选区变化时回调行号（1 基），目录用它高亮当前章节。 */
  onCursor?: (line: number) => void;
}): EditorHandle {
  const extensions: Extension[] = [
    lineNumbers(),
    highlightActiveLineGutter(),
    highlightSpecialChars(),
    history(),
    foldGutter(),
    drawSelection(),
    dropCursor(),
    indentOnInput(),
    bracketMatching(),
    highlightActiveLine(),
    highlightSelectionMatches(),
    // 提供 search 状态字段 + .cm-searchMatch 高亮；不挂 searchKeymap，
    // 免得 Ctrl+F 弹出 CodeMirror 自带的搜索面板（我们用自绘的查找栏）
    search(),
    findHighlighter,
    syntaxHighlighting(mdHighlightStyle),
    markdown({ base: markdownLanguage, codeLanguages: languages }),
    keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
    EditorView.lineWrapping,
    EditorView.domEventHandlers({
      contextmenu(event, view) {
        event.preventDefault();
        options.onContextMenu?.(event.clientX, event.clientY, !view.state.selection.main.empty);
        return true;
      },
    }),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) options.onChange();
      if (update.selectionSet) {
        const line = update.state.doc.lineAt(update.state.selection.main.head).number;
        options.onCursor?.(line);
      }
    }),
  ];

  const view = new EditorView({
    state: EditorState.create({ doc: "", extensions }),
    parent: options.parent,
  });

  function createState(doc: string): EditorState {
    return EditorState.create({ doc, extensions });
  }

  /** 把查询写进编辑器状态。返回查询对象，供调用方统计匹配数。 */
  function setQuery(searchText: string, replaceText = ""): SearchQuery {
    const query = new SearchQuery({ search: searchText, replace: replaceText });
    view.dispatch({ effects: setSearchQuery.of(query) });
    return query;
  }

  function countMatches(query: SearchQuery): number {
    let count = 0;
    const cursor = query.getCursor(view.state);
    for (let step = cursor.next(); !step.done; step = cursor.next()) count += 1;
    return count;
  }

  function selectedText(): string {
    return view.state.selection.ranges
      .filter((range) => !range.empty)
      .map((range) => view.state.sliceDoc(range.from, range.to))
      .join("\n");
  }

  return {
    view,
    getDoc: () => view.state.doc.toString(),
    setDoc: (text: string) =>
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: text },
      }),
    focus: () => view.focus(),
    requestMeasure: () => view.requestMeasure(),

    createState,

    getState: () => view.state,

    setState(next, scrollTop = 0) {
      // setState 不会触发 updateListener，所以外部要自己负责同步标题/预览
      view.setState(next);
      view.requestMeasure();
      // 测量是异步的，先立刻设一次，再等一帧补一次，避免被重置回顶部
      view.scrollDOM.scrollTop = scrollTop;
      requestAnimationFrame(() => {
        view.scrollDOM.scrollTop = scrollTop;
        view.requestMeasure();
      });
    },

    getScrollTop: () => view.scrollDOM.scrollTop,

    gotoLine(line) {
      const target = Math.min(Math.max(1, line), view.state.doc.lines);
      const pos = view.state.doc.line(target).from;
      view.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
      view.focus();
    },

    getCursorLine: () => view.state.doc.lineAt(view.state.selection.main.head).number,

    // 分屏滚动联动：编辑区顶部行号 ⇄ 像素位置。
    // lineBlockAtHeight 收的是「相对文档顶部的像素」，正好等于 scrollTop。
    getTopLine: () => view.state.doc.lineAt(view.lineBlockAtHeight(view.scrollDOM.scrollTop).from).number,

    scrollToLine(line) {
      const target = Math.min(Math.max(1, Math.round(line)), view.state.doc.lines);
      view.scrollDOM.scrollTop = view.lineBlockAt(view.state.doc.line(target).from).top;
    },

    find(searchText) {
      if (!searchText) return FAIL("empty");
      const query = setQuery(searchText);
      const count = countMatches(query);
      if (count === 0) return FAIL("nomatch");
      view.focus();
      // findNext 会从当前选区往后找，所以连按 Enter 就能逐个往下跳
      findNext(view);
      return { ok: true, reason: "unavailable", count };
    },

    replace(findText, replaceText) {
      if (!findText) return FAIL("empty");
      const query = setQuery(findText, replaceText);
      const count = countMatches(query);
      if (count === 0) return FAIL("nomatch");
      view.focus();
      replaceNext(view);
      return { ok: true, reason: "unavailable", count };
    },

    replaceAll(findText, replaceText) {
      if (!findText) return FAIL("empty");
      const query = setQuery(findText, replaceText);
      const count = countMatches(query);
      if (count === 0) return FAIL("nomatch");
      view.focus();
      replaceAllCommand(view);
      return { ok: true, reason: "unavailable", count };
    },

    clearSearch() {
      view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: "" })) });
    },

    undo() {
      view.focus();
      return undo(view);
    },
    redo() {
      view.focus();
      return redo(view);
    },
    selectAll() {
      view.focus();
      return selectAll(view);
    },

    deleteSelection() {
      if (view.state.selection.ranges.every((range) => range.empty)) return false;
      view.focus();
      const transaction = view.state.changeByRange((range) =>
        range.empty
          ? { range }
          : {
              changes: { from: range.from, to: range.to, insert: "" },
              range: EditorSelection.cursor(range.from),
            },
      );
      view.dispatch({ ...transaction, scrollIntoView: true, userEvent: "delete.cut" });
      return true;
    },

    insertText(text: string) {
      view.focus();
      view.dispatch({
        ...view.state.replaceSelection(text),
        scrollIntoView: true,
        userEvent: "input.paste",
      });
    },

    hasSelection: () => !view.state.selection.main.empty,
    getSelectedText: () => selectedText(),
  };
}
