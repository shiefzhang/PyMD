/**
 * 数学 / 化学公式渲染。
 *
 * 分工：comrak（Rust）只负责**识别公式边界**，输出
 *   <span data-math-style="inline">…</span>
 *   <span data-math-style="display">…</span>
 *   <pre><code class="language-math" data-math-style="display">…</code></pre>
 * 真正的排版交给 KaTeX（前端、离线、不依赖 CDN）。
 * 化学式靠 mhchem 扩展，写法是 $\ce{H2SO4}$ / $$\ce{2H2 + O2 -> 2H2O}$$。
 */
import katex from "katex";
import "katex/contrib/mhchem";
import "katex/dist/katex.min.css";

const MATH_SELECTOR = "[data-math-style]";

/**
 * 把 root 里所有 data-math-style 节点替换成 KaTeX 渲染结果。
 * 每次预览更新后都要跑一遍（innerHTML 会把公式重新变回纯文本）。
 */
export function renderMath(root: ParentNode): void {
  const nodes = root.querySelectorAll<HTMLElement>(MATH_SELECTOR);

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
  }
}
