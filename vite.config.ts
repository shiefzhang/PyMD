import { defineConfig } from "vite";

// Tauri 需要一个固定端口的开发服务器，且不能因为 src-tauri 变动而重启。
export default defineConfig({
  clearScreen: false,
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
