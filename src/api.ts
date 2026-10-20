/**
 * 前端与 Rust 之间的唯一通道。
 *
 * 每个函数都对应 src-tauri/src/lib.rs 里一个 #[tauri::command]。
 * 参数名必须与 Rust 侧形参名一致（Tauri 会自动把 snake_case 命令名映射过来，
 * 但参数对象里的 key 保持 snake_case 就对了）。
 */
import { invoke } from "@tauri-apps/api/core";

export interface FileDoc {
  path: string;
  content: string;
  modifiedMs: number;
  size: number;
  encoding: string;
}

export interface DirEntry {
  name: string;
  path: string;
  isDir: boolean;
  size: number;
}

export interface Heading {
  level: number;
  text: string;
  id: string;
  /** 1 基行号，点目录时用它定位 */
  line: number;
}

export interface Rendered {
  html: string;
  words: number;
  lines: number;
  headings: Heading[];
}

export const readDocument = (path: string) => invoke<FileDoc>("read_document", { path });

export const writeDocument = (path: string, content: string) =>
  invoke<number>("write_document", { path, content });

/**
 * `hardbreaks`：段内单个换行是否渲染成 `<br>`（对应 comrak 的 `render.hardbreaks`）。
 */
export const renderMarkdown = (source: string, hardbreaks: boolean) =>
  invoke<Rendered>("render_markdown", { source, hardbreaks });

export const listDir = (path: string) => invoke<DirEntry[]>("list_dir", { path });

export const watchDocument = (path: string) => invoke<void>("watch_document", { path });

/** 剪贴板走 Rust（Win32），WebView 的 navigator.clipboard 在 Tauri 里会卡住。 */
export const clipboardRead = () => invoke<string>("clipboard_read");

export const clipboardWrite = (text: string) => invoke<void>("clipboard_write", { text });

export const appInfo = () => invoke<string>("app_info");
