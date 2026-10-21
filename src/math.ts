/**
 * 数学 / 化学公式渲染。
 *
 * 分工：comrak（Rust）只负责**识别公式边界**，输出
 *   <span data-math-style="inline">…</span>
 *   <span data-math-style="display">…</span>
 *   <pre><code class="language-math" data-math-style="display">…</code></pre>
 * 真正的排版交给 KaTeX（前端、离线、不依赖 CDN）。
 * 化学式靠 mhchem 扩展，写法是 $\ce{H2SO4}$ / $$\ce{2H2 + O2 -> 2H2O}$$。
 *
 * 另有「公式转图」模式（工具栏勾选框）：排版仍走 KaTeX，但排完会再换成一张 PNG。
 * 目的不是显示好看，而是**能粘到别的软件里**——KaTeX 的 DOM 依赖自带字体，
 * 复制到 Word / 微信就散架。转图的实现见 mathimg.ts。
 */
import katex from "katex";
import "katex/contrib/mhchem";
import "katex/dist/katex.min.css";
import { formulaImage } from "./mathimg";

const MATH_SELECTOR = "[data-math-style]";

export interface MathRenderOptions {
  /** 是否把公式换成图片（默认 false，保持 KaTeX 矢量渲染） */
  asImage: boolean;
  /** 预览字号，转图时按这个尺寸出像素，换字号要重转 */
  fontPx: number;
}

/**
 * 把 root 里所有 data-math-style 节点替换成 KaTeX 渲染结果。
 * 每次预览更新后都要跑一遍（innerHTML 会把公式重新变回纯文本）。
 *
 * 返回 Promise 是因为「公式转图」要异步光栅化。**即使没有转图也返回 Promise**，
 * 调用方统一 await 之后再重建滚动锚点——图片是后插进来的，高度会再变一次。
 */
export async function renderMath(root: ParentNode, options: MathRenderOptions): Promise<void> {
  const nodes = root.querySelectorAll<HTMLElement>(MATH_SELECTOR);
  const pending: Array<{ holder: HTMLElement; tex: string; display: boolean }> = [];

  for (const node of nodes) {
    const tex = (node.textContent ?? "").trim();
    if (!tex) continue;

    const display = node.dataset.mathStyle === "display";

    let html: string;
    try {
      html = katex.renderToString(tex, {
        displayMode: display,
        // 语法错不要抛到外层：KaTeX 会把原式标红显示，预览其余部分照常
        throwOnError: false,
        errorColor: "#cf222e",
        // 放宽限制，允许中文标点等 unicode 字符
        strict: false,
        output: "html",
      });
    } catch (err) {
      console.warn("公式渲染失败:", tex, err);
      continue;
    }

    // 用 span 而不是 div：块级公式常位于 <p> 内部，塞 div 会造成非法嵌套
    const holder = document.createElement("span");
    holder.className = display ? "math-display" : "math-inline";
    holder.innerHTML = html;

    // ```math 围栏：comrak 给的是 <pre><code data-math-style="display">，
    // 要把整个 <pre> 换掉，否则会留下灰底代码块的壳
    const target =
      display && node.parentElement?.tagName === "PRE" ? node.parentElement : node;
    target.replaceWith(holder);

    if (options.asImage) pending.push({ holder, tex, display });
  }

  // 没有要转图的就到此为止：KaTeX 的矢量结果已经就位
  if (pending.length === 0) return;

  // 一张一张来。MathJax 是懒加载的，第一张要等 chunk 落地；
  // 顺序处理比并发更稳，公式多的时候也不会一下把主线程占满。
  for (const item of pending) {
    // 上一张还在转的时候，预览可能已经被下一次渲染整块换掉了 —— 别往死节点上写
    if (!item.holder.isConnected) continue;

    const image = await formulaImage(item.tex, item.display, options.fontPx);
    // 转图失败就保留 KaTeX 的矢量渲染：宁可不好粘，也不能让公式消失
    if (!image || !item.holder.isConnected) continue;

    const img = document.createElement("img");
    img.className = `${item.display ? "math-display" : "math-inline"} math-img`;
    img.src = image.src;
    // 按 CSS 像素给出固有尺寸，位图本身是 3 倍，放大不糊
    img.width = Math.max(1, Math.round(image.width));
    img.height = Math.max(1, Math.round(image.height));
    img.alt = item.tex;
    img.draggable = false;
    // MathJax 的 vertical-align 是「沉到基线以下多少」：公式里的分号、下标靠它对齐
    if (image.lift) img.style.verticalAlign = `${-image.lift}px`;

    item.holder.replaceWith(img);
  }
}
