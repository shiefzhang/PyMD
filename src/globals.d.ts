/// 构建期由 vite 的 `define` 注入（值来自 package.json 的 version）。
/// 声明放在这里，`src` 下任何模块都能直接用，不必逐个 import。
declare const __APP_VERSION__: string;
