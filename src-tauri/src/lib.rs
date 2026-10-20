//! mdpad 的 Rust 侧（Tauri 后端）。
//!
//! 这里的每个 `#[tauri::command]` 都是前端可以直接 `invoke()` 的函数。
//! 设计原则：**能放 Rust 的就放 Rust** —— Markdown 渲染、文件读写、目录读取、文件监听
//! 都在这一侧完成，前端只负责显示。
//!
//! 用到的 crate：
//! - `comrak`  ：CommonMark + GFM 解析器，把 Markdown 编译成 HTML
//! - `notify`  ：跨平台文件系统监听
//! - `serde`   ：结构体与 JSON 的双向映射（Tauri IPC 的底座）
//!
//! 注：文档内查找/替换走 CodeMirror 的 @codemirror/search，不需要 Rust 参与；
//! 早期那个「在所在目录搜索」的命令已随功能下线一并删除。

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::UNIX_EPOCH;

use notify::{RecursiveMode, Watcher};
use serde::Serialize;
use tauri::{AppHandle, Emitter, State};

/// 命令统一用 `Result<T, String>` 作为返回类型。
/// Tauri 会把 `Err` 变成前端 Promise 的 reject，所以 `String` 就是错误信息载体。
type CmdResult<T> = Result<T, String>;

// ---------------------------------------------------------------------------
// 数据传输对象（DTO）—— 这些结构体会被 serde 序列化成 JSON 发给前端。
// `rename_all = "camelCase"` 让 Rust 的 snake_case 字段在 JS 侧变成 camelCase。
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FileDoc {
    pub path: String,
    pub content: String,
    pub modified_ms: u64,
    pub size: u64,
    pub encoding: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DirEntry {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
    pub size: u64,
}

#[derive(Debug, Serialize)]
pub struct Heading {
    pub level: u8,
    pub text: String,
    pub id: String,
    /// 标题所在的 1 基行号，前端点目录时用它把光标送过去。
    pub line: usize,
}

#[derive(Debug, Serialize)]
pub struct Rendered {
    pub html: String,
    pub words: usize,
    pub lines: usize,
    pub headings: Vec<Heading>,
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

/// 把字节流解成文本。
///
/// 返回 `(文本, 编码说明)`。这里刻意不 panic：
/// 不是合法 UTF-8 时退化成有损解码，宁可显示几个乱码也不要崩掉。
/// （想练手的话，这里可以接 `encoding_rs` 支持 GBK/Shift_JIS 探测。）
fn decode(bytes: Vec<u8>) -> (String, String) {
    const BOM: [u8; 3] = [0xEF, 0xBB, 0xBF];

    let (payload, label) = if bytes.starts_with(&BOM) {
        // `bytes[3..].to_vec()` 会产生一份拷贝；也可以改签名收 `&[u8]` 来避免
        (bytes[3..].to_vec(), "UTF-8 (BOM)")
    } else {
        (bytes, "UTF-8")
    };

    match String::from_utf8(payload) {
        Ok(text) => (text, label.to_string()),
        Err(err) => {
            // 拿回原始字节，做有损转换
            let raw = err.into_bytes();
            (String::from_utf8_lossy(&raw).into_owned(), "UTF-8 (lossy)".to_string())
        }
    }
}

fn modified_ms(path: &Path) -> CmdResult<u64> {
    let meta = fs::metadata(path).map_err(|e| format!("读取元信息失败: {e}"))?;
    let modified = meta.modified().map_err(|e| format!("读取修改时间失败: {e}"))?;
    Ok(modified
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0))
}

/// 中日韩统一表意文字 + 假名 + 韩文音节。用于中英混排的「字数」统计。
fn is_cjk(ch: char) -> bool {
    matches!(
        ch,
        '\u{4E00}'..='\u{9FFF}'   // CJK 统一表意文字
        | '\u{3400}'..='\u{4DBF}' // 扩展 A
        | '\u{3040}'..='\u{30FF}' // 平假名 / 片假名
        | '\u{AC00}'..='\u{D7AF}' // 韩文音节
    )
}

/// 中英混排计数：中文按字算，英文按词算。
fn count_words(source: &str) -> usize {
    let mut count = 0;
    let mut inside_latin_word = false;

    for ch in source.chars() {
        if ch.is_ascii_alphanumeric() {
            if !inside_latin_word {
                count += 1;
                inside_latin_word = true;
            }
        } else {
            inside_latin_word = false;
            if is_cjk(ch) {
                count += 1;
            }
        }
    }

    count
}

fn slugify(text: &str) -> String {
    text.chars()
        .map(|ch| {
            if ch.is_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '-'
            }
        })
        .collect::<String>()
        .to_lowercase()
}

/// 用 comrak 的 AST 取标题，供前端做目录（TOC）。
///
/// 为什么不用「逐行扫 `#`」：那样代码围栏里的 `#` 注释会被误判，`>` 引用里的标题
/// 又会被漏掉，导致目录条数和预览里的 `h1~h6` 对不上。走 AST 用的是和正文渲染
/// 完全相同的解析规则，第 i 条目录必然对应预览里第 i 个标题。
fn collect_headings(source: &str) -> Vec<Heading> {
    use comrak::{parse_document, Arena};

    let arena = Arena::new();
    let options = comrak::Options::default();
    let root = parse_document(&arena, source, &options);

    let mut headings = Vec::new();
    walk_headings(root, &mut headings, source);
    headings
}

fn walk_headings<'a>(
    node: &'a comrak::nodes::AstNode<'a>,
    out: &mut Vec<Heading>,
    source: &str,
) {
    use comrak::nodes::NodeValue;

    for child in node.children() {
        {
            let data = child.data.borrow();
            if let NodeValue::Heading(heading) = &data.value {
                let mut text = String::new();
                node_text(child, &mut text);
                let text = text.trim().to_string();

                if !text.is_empty() {
                    let mut line = data.sourcepos.start.line;
                    if line == 0 {
                        // 极少数情况下 sourcepos 不可用，退化成按文本找行
                        line = find_heading_line(source, &text, heading.level);
                    }
                    out.push(Heading {
                        level: heading.level,
                        id: slugify(&text),
                        text,
                        line,
                    });
                }
            }
        }
        walk_headings(child, out, source);
    }
}

/// 拼接一行标题里的纯文本（把 **粗体**、`代码`、公式都摊平成文字）。
fn node_text<'a>(node: &'a comrak::nodes::AstNode<'a>, out: &mut String) {
    use comrak::nodes::NodeValue;

    for child in node.children() {
        match &child.data.borrow().value {
            NodeValue::Text(t) => out.push_str(t),
            NodeValue::Code(c) => out.push_str(&c.literal),
            NodeValue::Math(m) => out.push_str(&m.literal),
            NodeValue::SoftBreak | NodeValue::LineBreak => out.push(' '),
            _ => node_text(child, out),
        }
    }
}

/// `sourcepos` 不可用时的兜底：按文本 + 级别找第一行匹配的 ATX 标题。
fn find_heading_line(source: &str, text: &str, level: u8) -> usize {
    source
        .lines()
        .enumerate()
        .find(|(_, line)| {
            let trimmed = line.trim_start();
            let hashes = trimmed.chars().take_while(|c| *c == '#').count();
            hashes == level as usize
                && trimmed.trim_start_matches('#').trim().trim_end_matches('#').trim() == text
        })
        .map(|(index, _)| index + 1)
        .unwrap_or(1)
}

// ---------------------------------------------------------------------------
// 剪贴板
// ---------------------------------------------------------------------------

/// 剪贴板走 Win32 原生 API，而不是 `navigator.clipboard`。
///
/// 原因：WebView2 里读剪贴板属于「需要用户授权」的权限，Chromium 会走
/// PermissionRequested 弹窗；Tauri 没接管这个事件，结果 `readText()` 既不 resolve
/// 也不 reject，整个 Promise 就挂在那里。写剪贴板相对宽松，但为了一致性一起走原生。
///
/// 这里用 `#[link]` 直接声明 extern，省掉再引一个 windows-sys 依赖。
#[cfg(windows)]
mod clipboard {
    use std::ffi::c_void;
    use std::ptr;

    // 直接写常量，省得为了两个数字去开 Ole 那一整套 API
    const CF_UNICODETEXT: u32 = 13;
    const GMEM_MOVEABLE: u32 = 0x0002;

    #[link(name = "user32")]
    extern "system" {
        fn OpenClipboard(hwnd: *mut c_void) -> i32;
        fn CloseClipboard() -> i32;
        fn EmptyClipboard() -> i32;
        fn IsClipboardFormatAvailable(format: u32) -> i32;
        fn GetClipboardData(format: u32) -> *mut c_void;
        fn SetClipboardData(format: u32, mem: *mut c_void) -> *mut c_void;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GlobalAlloc(flags: u32, bytes: usize) -> *mut c_void;
        fn GlobalLock(mem: *mut c_void) -> *mut c_void;
        fn GlobalUnlock(mem: *mut c_void) -> i32;
        fn GlobalFree(mem: *mut c_void) -> *mut c_void;
    }

    /// 拿着打开的剪贴板句柄，离开作用域自动 CloseClipboard（相当于 RAII）
    struct ClipboardGuard;

    impl Drop for ClipboardGuard {
        fn drop(&mut self) {
            unsafe {
                CloseClipboard();
            }
        }
    }

    /// 剪贴板是全局独占资源，别的程序正在用时 OpenClipboard 会失败，重试几次。
    fn open() -> Result<ClipboardGuard, String> {
        for _ in 0..5 {
            if unsafe { OpenClipboard(ptr::null_mut()) } != 0 {
                return Ok(ClipboardGuard);
            }
            std::thread::sleep(std::time::Duration::from_millis(40));
        }
        Err("剪贴板被其他程序占用，暂时打不开".to_string())
    }

    pub fn read_text() -> Result<String, String> {
        let _guard = open()?;

        if unsafe { IsClipboardFormatAvailable(CF_UNICODETEXT) } == 0 {
            return Err("剪贴板里没有文本".to_string());
        }

        let handle = unsafe { GetClipboardData(CF_UNICODETEXT) };
        if handle.is_null() {
            return Err("读取剪贴板数据失败".to_string());
        }

        let wide = unsafe { GlobalLock(handle) } as *const u16;
        if wide.is_null() {
            return Err("锁定剪贴板数据失败".to_string());
        }

        // UTF-16 以 0 结尾，先数长度再切片
        let mut len = 0usize;
        unsafe {
            while *wide.add(len) != 0 {
                len += 1;
            }
        }

        let text = unsafe { String::from_utf16_lossy(std::slice::from_raw_parts(wide, len)) };
        unsafe {
            GlobalUnlock(handle);
        }
        Ok(text)
    }

    pub fn write_text(text: &str) -> Result<(), String> {
        // 结尾补一个 0，Windows 那边按 C 字符串读
        let wide: Vec<u16> = text.encode_utf16().chain(std::iter::once(0)).collect();
        let bytes = wide.len() * std::mem::size_of::<u16>();

        let _guard = open()?;
        unsafe {
            if EmptyClipboard() == 0 {
                return Err("清空剪贴板失败".to_string());
            }

            let handle = GlobalAlloc(GMEM_MOVEABLE, bytes);
            if handle.is_null() {
                return Err("分配剪贴板内存失败".to_string());
            }

            let dst = GlobalLock(handle) as *mut u16;
            if dst.is_null() {
                GlobalFree(handle);
                return Err("锁定剪贴板内存失败".to_string());
            }
            ptr::copy_nonoverlapping(wide.as_ptr(), dst, wide.len());
            GlobalUnlock(handle);

            // SetClipboardData 成功后这块内存归系统所有，不能再自己 free
            if SetClipboardData(CF_UNICODETEXT, handle).is_null() {
                GlobalFree(handle);
                return Err("写入剪贴板失败".to_string());
            }
        }
        Ok(())
    }
}

#[cfg(not(windows))]
mod clipboard {
    pub fn read_text() -> Result<String, String> {
        Err("剪贴板命令目前只实现了 Windows".to_string())
    }
    pub fn write_text(_text: &str) -> Result<(), String> {
        Err("剪贴板命令目前只实现了 Windows".to_string())
    }
}

// ---------------------------------------------------------------------------
// Tauri 命令
// ---------------------------------------------------------------------------

/// 最简命令：用来验证「前端 → Rust → 前端」这条链路是通的。
#[tauri::command]
fn app_info() -> String {
    format!(
        "pyMD {} · {} · Rust 侧已连接",
        env!("CARGO_PKG_VERSION"),
        std::env::consts::OS
    )
}

#[tauri::command]
fn read_document(path: String) -> CmdResult<FileDoc> {
    let target = PathBuf::from(&path);
    let bytes = fs::read(&target).map_err(|e| format!("读取失败 {}: {e}", target.display()))?;

    let size = bytes.len() as u64;
    let (content, encoding) = decode(bytes);
    let modified_ms = modified_ms(&target)?;

    Ok(FileDoc {
        path,
        content,
        modified_ms,
        size,
        encoding,
    })
}

#[tauri::command]
fn write_document(path: String, content: String) -> CmdResult<u64> {
    let target = PathBuf::from(&path);

    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("创建目录失败: {e}"))?;
    }

    fs::write(&target, content.as_bytes())
        .map_err(|e| format!("写入失败 {}: {e}", target.display()))?;

    Ok(content.len() as u64)
}

/// Markdown → HTML。渲染放在 Rust 侧：comrak 比 JS 实现快，且前端只管显示。
///
/// `hardbreaks`：段内单个换行是否渲染成 `<br>`。由工具栏的勾选框控制，
/// 默认开——中文写作里一个换行通常就是一条。
#[tauri::command]
fn render_markdown(source: String, hardbreaks: bool) -> CmdResult<Rendered> {
    // comrak 0.56 的 Options 带生命周期参数，这里让编译器自己推断
    let mut options = comrak::Options::default();

    // GFM 扩展
    options.extension.strikethrough = true;
    options.extension.table = true;
    options.extension.tasklist = true;
    options.extension.autolink = true;
    options.extension.footnotes = true;
    options.extension.description_lists = true;
    options.extension.alerts = true;
    // 中文写作必备：`**粗体**` 紧贴汉字时也能正确解析（CommonMark 默认会漏掉）
    options.extension.cjk_friendly_emphasis = true;
    // 数学 / 化学公式：comrak 只负责识别并输出
    //   <span data-math-style="inline|display">TeX</span>
    // 真正的排版交给前端的 KaTeX（含 mhchem，可写 \ce{H2SO4}）
    options.extension.math_dollars = true; // $...$ 与 $$...$$
    options.extension.math_latex = true; // \(...\) 与 \[...\]
    options.extension.math_code = true; // $`...`$ 与 ```math 围栏
    // 允许原始 HTML 直通。读的是本地文件，不设防
    options.render.r#unsafe = true;
    // 单个换行是否也换行（CommonMark 默认把段内换行当空格）。
    // 由工具栏的「硬换行」勾选框控制。
    options.render.hardbreaks = hardbreaks;
    // 块级元素带 data-sourcepos="起行:列-止行:列"，
    // 供前端做「源码 ↔ 预览」的滚动联动（按行号对齐，不按比例）。
    options.render.sourcepos = true;

    Ok(Rendered {
        html: comrak::markdown_to_html(&source, &options),
        words: count_words(&source),
        lines: source.lines().count(),
        headings: collect_headings(&source),
    })
}

#[tauri::command]
fn list_dir(path: String) -> CmdResult<Vec<DirEntry>> {
    let dir = PathBuf::from(&path);
    let reader = fs::read_dir(&dir).map_err(|e| format!("读取目录失败 {}: {e}", dir.display()))?;

    let mut entries = Vec::new();
    for item in reader {
        let item = item.map_err(|e| e.to_string())?;
        let name = item.file_name().to_string_lossy().into_owned();

        if name.starts_with('.') {
            continue;
        }

        let file_type = item.file_type().map_err(|e| e.to_string())?;
        let size = item.metadata().map(|m| m.len()).unwrap_or(0);

        entries.push(DirEntry {
            name,
            path: item.path().to_string_lossy().into_owned(),
            is_dir: file_type.is_dir(),
            size,
        });
    }

    // 目录排在文件前面，同组内按名称字典序
    entries.sort_by(|a, b| {
        b.is_dir
            .cmp(&a.is_dir)
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
    });

    Ok(entries)
}

/// 持有当前的文件监听器。旧监听器在赋值时被 drop，监听随之解除。
struct WatchState(Mutex<Option<notify::RecommendedWatcher>>);

/// 监听单个文件；磁盘上发生变化时通过 `file-changed` 事件通知前端。
#[tauri::command]
fn watch_document(app: AppHandle, path: String, state: State<'_, WatchState>) -> CmdResult<()> {
    let target = PathBuf::from(&path);
    let event_path = path.clone();
    let emitter = app.clone();

    let mut watcher =
        notify::recommended_watcher(move |result: Result<notify::Event, notify::Error>| {
            let Ok(event) = result else { return };
            if matches!(
                event.kind,
                notify::EventKind::Modify(_) | notify::EventKind::Create(_)
            ) {
                // 忽略 emit 失败：前端可能已经关闭
                let _ = emitter.emit("file-changed", event_path.clone());
            }
        })
        .map_err(|e| format!("创建监听器失败: {e}"))?;

    watcher
        .watch(&target, RecursiveMode::NonRecursive)
        .map_err(|e| format!("监听失败 {}: {e}", target.display()))?;

    *state.0.lock().map_err(|e| e.to_string())? = Some(watcher);
    Ok(())
}

/// 读系统剪贴板里的文本（编辑菜单的「粘贴」用）。
#[tauri::command]
fn clipboard_read() -> CmdResult<String> {
    clipboard::read_text()
}

/// 把文本写进系统剪贴板（编辑菜单的「复制 / 剪切」用）。
#[tauri::command]
fn clipboard_write(text: String) -> CmdResult<()> {
    clipboard::write_text(&text)
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(WatchState(Mutex::new(None::<notify::RecommendedWatcher>)))
        .invoke_handler(tauri::generate_handler![
            app_info,
            read_document,
            write_document,
            render_markdown,
            list_dir,
            clipboard_read,
            clipboard_write,
            watch_document,
        ])
        .run(tauri::generate_context!())
        .expect("启动 mdpad 失败");
}
