/**
 * 公式 → PNG 图片。
 *
 * 为什么要另起一套引擎：KaTeX 排出来的是「HTML + 自带字体」，黏到 Word / 微信 / 邮件里
 * 字体一丢就散架——那些软件不认 KaTeX 的 DOM，也不认 MathML 和 TeX。位图才是通用格式。
 *
 * 为什么不用 KaTeX 自己光栅化：它的 HTML 布局靠 KaTeX_Main / KaTeX_Math 这些字体，
 * 转 SVG 必须把 katex.min.css 和十几个 woff2 一起内联进 foreignObject，少一个就出豆腐块。
 * MathJax 的 SVG 输出是**路径化**的——每个字形就是一段 <path>，天生不依赖字体，
 * 序列化出来就能直接当图片画进 canvas。
 *
 * 代价是多一个引擎（排版细节和 KaTeX 略有差异），所以整块代码只在用户勾了「公式转图」
 * 时才动态 import，平时一个字节都不下载。
 */

/** 浅色纸面上的正文墨色。图片是「拿去粘到别处」的，所以不跟深色主题走—— */
/** 深色主题下的可读性由 CSS 加一层白色描边解决（见 style.css 的 .math-img）。 */
const INK = "#1f2328";

/** 位图按 3 倍像素出，粘进 Word 放大或打印都不糊。 */
const SCALE = 3;

/** 缓存上限：同一份文档里公式通常几十个，超过就整体丢弃重来，免得长会话吃内存。 */
const CACHE_LIMIT = 512;

export interface FormulaImage {
  /** data:image/png;base64,… */
  src: string;
  /** CSS 像素宽高（图片按 CSS 尺寸显示，位图本身是 3 倍） */
  width: number;
  height: number;
  /** 基线以下多少像素（MathJax 的 vertical-align，正数表示要往下沉） */
  lift: number;
}

const cache = new Map<string, FormulaImage>();

let engine: Promise<(tex: string, display: boolean, em: number) => string> | null = null;

/** 兼容 CJS 的具名导出：mathjax-full 是 CommonJS，不同打包路径下拿到的东西不完全一样。 */
function pick<T>(mod: unknown, name: string): T {
  const bag = mod as Record<string, unknown>;
  if (bag && bag[name]) return bag[name] as T;
  const def = bag?.default as Record<string, unknown> | undefined;
  if (def && def[name]) return def[name] as T;
  return (bag?.default ?? mod) as T;
}

/**
 * 懒加载 MathJax。用 liteAdaptor：不需要真 DOM，也不会去碰 document，
 * 纯字符串进出，适合「一批公式反复转字符串」的场合。
 */
async function loadEngine(): Promise<(tex: string, display: boolean, em: number) => string> {
  if (engine) return engine;

  engine = (async () => {
    const [mjMod, texMod, svgMod, adaptorMod, handlerMod, packagesMod] = await Promise.all([
      import("mathjax-full/js/mathjax.js"),
      import("mathjax-full/js/input/tex.js"),
      import("mathjax-full/js/output/svg.js"),
      import("mathjax-full/js/adaptors/liteAdaptor.js"),
      import("mathjax-full/js/handlers/html.js"),
      import("mathjax-full/js/input/tex/AllPackages.js"),
    ]);

    const mathjax = pick<{ document: (doc: string, opts: unknown) => any }>(mjMod, "mathjax");
    const TeX = pick<any>(texMod, "TeX");
    const SVG = pick<any>(svgMod, "SVG");
    const liteAdaptor = pick<() => any>(adaptorMod, "liteAdaptor");
    const RegisterHTMLHandler = pick<(a: unknown) => void>(handlerMod, "RegisterHTMLHandler");
    const AllPackages = pick<string[]>(packagesMod, "AllPackages");

    const adaptor = liteAdaptor();
    RegisterHTMLHandler(adaptor);

    const doc = mathjax.document("", {
      // AllPackages 含 mhchem，所以 $\ce{H2SO4}$ 这类化学式和 KaTeX 那边一样能用
      InputJax: new TeX({ packages: AllPackages }),
      // fontCache: "none" 让每个 SVG 自带 <defs>，互相不引用，单拎出来也是完整图片
      OutputJax: new SVG({ fontCache: "none" }),
    });

    return (tex: string, display: boolean, em: number) => {
      const node = doc.convert(tex, {
        display,
        em,
        // ex 按半个 em 估：16px 正文 → 8px/ex，和浏览器里的换算一致
        ex: em / 2,
        containerWidth: 120 * em,
      });
      return adaptor.outerHTML(node);
    };
  })();

  return engine;
}

/**
 * 把 mjx-container 的 HTML 收拾成一张独立 SVG：
 * 宽度单位 ex → px（不然 <img> 拿不到确定的固有尺寸），去掉容器上的定位样式，
 * 把 currentColor 换成实色（SVG 当图片用时没有「当前颜色」可继承）。
 */
function toStandaloneSvg(markup: string, em: number): { svg: string; width: number; height: number; lift: number } | null {
  const match = markup.match(/<svg[\s\S]*?<\/svg>/);
  if (!match) return null;

  const exToPx = em / 2;
  const whole = markup;
  const liftMatch = whole.match(/vertical-align:\s*(-?[\d.]+)ex/);
  const lift = liftMatch ? -parseFloat(liftMatch[1]) * exToPx : 0;

  const svg = match[0];
  const tagEnd = svg.indexOf(">");
  let tag = svg.slice(0, tagEnd);
  const body = svg.slice(tagEnd);

  const widthMatch = tag.match(/width="([\d.]+)ex"/);
  const heightMatch = tag.match(/height="([\d.]+)ex"/);
  if (!widthMatch || !heightMatch) return null;

  const width = parseFloat(widthMatch[1]) * exToPx;
  const height = parseFloat(heightMatch[1]) * exToPx;

  tag = tag
    .replace(/width="[\d.]+ex"/, `width="${width.toFixed(2)}"`)
    .replace(/height="[\d.]+ex"/, `height="${height.toFixed(2)}"`)
    // 容器定位样式（vertical-align 之类）在这里没有意义，已经折算成 lift
    .replace(/\sstyle="[^"]*"/, "");
  if (!/\sxmlns=/.test(tag)) {
    tag = tag.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"');
  }

  return {
    svg: (tag + body).replaceAll("currentColor", INK),
    width,
    height,
    lift,
  };
}

async function rasterize(svg: string, width: number, height: number): Promise<string | null> {
  const img = new Image();
  // data URI 里的 SVG 不能加载外部资源——MathJax 的路径化输出本来也不需要，
  // 这正好是它能用、KaTeX 的 HTML 不能用的原因
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  try {
    await img.decode();
  } catch {
    return null;
  }

  // 画到 canvas 时不留白边：宽高向上取整，避免右/下边缘被裁掉半像素
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.ceil(width * SCALE));
  canvas.height = Math.max(1, Math.ceil(height * SCALE));
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/png");
}

/**
 * 取一张公式图片。同一公式 / 同一字号会命中缓存。
 * 失败返回 null —— 调用方保留 KaTeX 的矢量渲染，不要因为转图失败让公式消失。
 */
export async function formulaImage(tex: string, display: boolean, em: number): Promise<FormulaImage | null> {
  const key = `${display ? "d" : "i"}|${Math.round(em)}|${tex}`;
  const hit = cache.get(key);
  if (hit) return hit;

  try {
    const convert = await loadEngine();
    const standalone = toStandaloneSvg(convert(tex, display, em), em);
    if (!standalone) return null;

    const src = await rasterize(standalone.svg, standalone.width, standalone.height);
    if (!src) return null;

    const image: FormulaImage = {
      src,
      width: standalone.width,
      height: standalone.height,
      lift: standalone.lift,
    };

    if (cache.size >= CACHE_LIMIT) cache.clear();
    cache.set(key, image);
    return image;
  } catch (err) {
    console.warn("公式转图失败，保留矢量渲染:", tex, err);
    return null;
  }
}
