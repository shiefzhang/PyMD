import { readFileSync } from "node:fs";
import { defineConfig } from "vite";

// 版本号只留一个来源：package.json。其余三处（tauri.conf.json、Cargo.toml、
// 前端常量）都以它为基准，这里在构建期把值注入成字面量，前端就不用再手写一份。
// 之前 main.ts 里复制粘贴的那份，升级时是最容易漏掉的。
const { version } = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf-8"),
) as { version: string };

// Tauri 需要一个固定端口的开发服务器，且不能因为 src-tauri 变动而重启。
export default defineConfig({
  clearScreen: false,
  define: {
    __APP_VERSION__: JSON.stringify(version),
  },
  server: {
    port: 1420,
    strictPort: true,
    host: false,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
  build: {
    // Windows 上 Tauri 用的是 WebView2，对应 Chromium 内核，可以直接用较新的语法。
    target: "chrome110",
    sourcemap: !!process.env.TAURI_DEBUG,
    minify: "esbuild",
  },
});
