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

/// 预览与纯文本导出共用的一份解析配置。
///
/// 抽出来是为了**保证两边对「什么是列表 / 什么是代码块 / 什么是标题」的判断完全一致**：
/// 如果这边开了 `table` 扩展、那边没开，同一份文档在预览里是表格、导出成 .txt 却变成
/// 一堆带竖线的段落，用户会以为是 bug。唯一不共用的是纯渲染相关的开关
/// （`sourcepos`、`hardbreaks`），它们在各自调用处单独设。
fn preview_options() -> comrak::Options<'static> {
    // comrak 0.56 的 Options 带生命周期参数；这里所有字段都是 owned，因此是 'static
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

    options
}

/// Markdown → HTML。渲染放在 Rust 侧：comrak 比 JS 实现快，且前端只管显示。
///
/// `hardbreaks`：段内单个换行是否渲染成 `<br>`。由工具栏的勾选框控制，
/// 默认开——中文写作里一个换行通常就是一条。
#[tauri::command]
fn render_markdown(source: String, hardbreaks: bool) -> CmdResult<Rendered> {
    let mut options = preview_options();
    // 单个换行是否也换行（CommonMark 默认把段内换行当空格）。
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

// ---------------------------------------------------------------------------
// 纯文本导出
// ---------------------------------------------------------------------------

/// Markdown → 纯文本：去掉所有标记，只留可读文字。
///
/// 服务两个入口：「复制为纯文本」（写剪贴板）和「另存为纯文本」（写 .txt）。
/// 两边共用同一个函数，保证「复制出来的」和「存下去的」逐字节相同。
#[tauri::command]
fn render_plain_text(source: String) -> CmdResult<String> {
    Ok(markdown_to_plain(&source))
}

/// 走 AST 而不是正则去标记。
///
/// 原因：`**` 出现在代码块里不该被删、`#` 出现在围栏里根本不是标题、表格的 `|`
/// 也可能是正文里的字符 —— 用正则清理要给每一项配例外，最后一定会漏。交给解析器
/// 只需要按节点类型决定「这一块怎么摊平」。
///
/// 输出约定：
/// - 块与块之间隔一个空行（标题、段落、列表、代码块都算块）
/// - 段内换行保留成换行（txt 是纯文本，按作者写的断行展示最自然）
/// - 列表保留 `- ` / `1. ` 前缀和缩进，任务列表保留 `[x]` / `[ ]`
/// - 代码块保留原文（缩进不清洗）；行内代码只留内容，去掉反引号
/// - 链接只留文字、图片只留 alt、公式只留 TeX 源码
/// - 原始 HTML 块整块丢弃（那不是给人读的正文）
fn markdown_to_plain(source: &str) -> String {
    use comrak::{parse_document, Arena};

    let arena = Arena::new();
    let options = preview_options();
    let root = parse_document(&arena, source, &options);

    let mut blocks: Vec<String> = Vec::new();
    for child in root.children() {
        let text = block_plain(child);
        let text = text.trim_end();
        if !text.trim().is_empty() {
            blocks.push(text.to_string());
        }
    }

    let mut out = blocks.join("\n\n");
    out.push('\n');
    out
}

/// 摊平一个块级节点。
///
/// **约定：返回值的每一行都不带缩进**，缩进由上一层（`list_plain` 缩子块）统一加。
/// 早期版本让 `pad` 一路往下传，结果嵌套列表被上、下两层各缩一次，变成 4 空格起步 ——
/// 「谁负责缩进」只能有一个答案，所以挪到最外面统一处理。
fn block_plain<'a>(node: &'a comrak::nodes::AstNode<'a>) -> String {
    use comrak::nodes::NodeValue;

    let data = node.data.borrow();
    match &data.value {
        // 叶子块：一行一段文字
        NodeValue::Paragraph
        | NodeValue::Heading(_)
        | NodeValue::DescriptionTerm
        | NodeValue::DescriptionDetails => inline_plain(node),

        // 代码块：原文照抄，语言标记（```rust 的 rust）本来就不在 literal 里
        NodeValue::CodeBlock(code) => code.literal.trim_end().to_string(),

        // 分隔线、原始 HTML、YAML front matter 都不是给人读的正文
        NodeValue::ThematicBreak | NodeValue::HtmlBlock(_) | NodeValue::FrontMatter(_) => {
            String::new()
        }

        NodeValue::BlockQuote | NodeValue::MultilineBlockQuote(_) => join_children(node),

        NodeValue::List(list) => list_plain(node, list),

        // Item / TaskItem 只在 list_plain 里被消费；单独撞上时按普通块拼
        NodeValue::Item(_) | NodeValue::TaskItem(_) => join_children(node),

        NodeValue::Table(_) => table_plain(node),

        NodeValue::DescriptionList | NodeValue::DescriptionItem(_) => join_children(node),

        // 脚注定义：`[1] 条文`，否则条文会孤零零地接在正文后面，看不出是脚注
        NodeValue::FootnoteDefinition(def) => {
            let body = join_children(node);
            if body.is_empty() {
                String::new()
            } else {
                format!("[{}] {body}", def.name)
            }
        }

        // 其他（Alert 等容器）直接摊平子节点
        _ => join_children(node),
    }
}

/// 把子节点各自当成一个块，块之间空一行。
fn join_children<'a>(node: &'a comrak::nodes::AstNode<'a>) -> String {
    let mut parts: Vec<String> = Vec::new();
    for child in node.children() {
        let text = block_plain(child);
        let text = text.trim_end();
        if !text.trim().is_empty() {
            parts.push(text.to_string());
        }
    }
    parts.join("\n\n")
}

/// 给多行文本加行首前缀。`first_too=false` 时保留首行不动
/// （用在「记号后面紧跟首块」的场合，比如 `- 第一项`）。
fn pad_lines(text: &str, pad: &str, first_too: bool) -> String {
    if pad.is_empty() {
        return text.to_string();
    }
    let mut out = String::new();
    for (index, line) in text.split('\n').enumerate() {
        if index > 0 {
            out.push('\n');
        }
        // 空行不加前缀，免得 txt 里到处是尾随空格
        if (index > 0 || first_too) && !line.is_empty() {
            out.push_str(pad);
        }
        out.push_str(line);
    }
    out
}

/// 列表：保留记号，子块（续段、嵌套列表、代码块）按记号宽度缩进。
fn list_plain<'a>(
    node: &'a comrak::nodes::AstNode<'a>,
    list: &comrak::nodes::NodeList,
) -> String {
    use comrak::nodes::{ListType, NodeValue};

    let ordered = matches!(list.list_type, ListType::Ordered);
    let mut ordinal = list.start;
    let mut items: Vec<String> = Vec::new();

    for item in node.children() {
        // 任务列表的项是 TaskItem（不是在 Item 里套一个 checkbox 节点）
        let marker = match &item.data.borrow().value {
            NodeValue::TaskItem(task) => {
                let box_mark = if task.symbol.is_some() { "[x] " } else { "[ ] " };
                if ordered {
                    format!("{ordinal}. {box_mark}")
                } else {
                    format!("- {box_mark}")
                }
            }
            _ => {
                if ordered {
                    format!("{ordinal}. ")
                } else {
                    "- ".to_string()
                }
            }
        };
        ordinal += 1;

        // 续行与子块缩进对齐到记号后面，嵌套列表因此自然往右退
        let child_pad = " ".repeat(marker.chars().count());
        let mut out = String::new();
        out.push_str(&marker);

        let mut first = true;
        for child in item.children() {
            let text = block_plain(child);
            let text = text.trim_end();
            if text.trim().is_empty() {
                continue;
            }
            if first {
                // 首块接在记号后面，只有它的续行需要缩进
                out.push_str(&pad_lines(text, &child_pad, false));
                first = false;
            } else {
                // tight 列表（默认写法）换行即可；loose 列表才空一行
                out.push('\n');
                if !list.tight {
                    out.push('\n');
                }
                out.push_str(&pad_lines(text, &child_pad, true));
            }
        }

        items.push(out.trim_end().to_string());
    }

    // 项与项之间一律只换行、不留空行：纯文本里列表本来就靠缩进和记号区分，
    // 空行只会让它看起来散了。而且 comrak 对任务列表的 `tight` 判定并不可靠
    // （`- [x] a` / `- [ ] b` 这种紧凑写法也可能报 loose），指望它反而多插空行。
    items.join("\n")
}

/// 表格：一行一条记录，单元格之间用 ` | ` 分隔，表头不加分隔线。
fn table_plain<'a>(node: &'a comrak::nodes::AstNode<'a>) -> String {
    let mut rows: Vec<String> = Vec::new();
    for row in node.children() {
        let mut cells: Vec<String> = Vec::new();
        for cell in row.children() {
            // 单元格里可能有换行 / 多段落，压成一行才能保持表格形状
            let text = inline_plain(cell);
            let flattened = text.split_whitespace().collect::<Vec<_>>().join(" ");
            cells.push(flattened);
        }
        rows.push(cells.join(" | ").trim_end().to_string());
    }
    rows.join("\n")
}

/// 摊平一段行内内容（粗体、斜体、行内代码、链接、图片都退化成文字）。
fn inline_plain<'a>(node: &'a comrak::nodes::AstNode<'a>) -> String {
    let mut out = String::new();
    walk_inline(node, &mut out);
    out
}

fn walk_inline<'a>(node: &'a comrak::nodes::AstNode<'a>, out: &mut String) {
    use comrak::nodes::NodeValue;

    for child in node.children() {
        match &child.data.borrow().value {
            NodeValue::Text(text) => out.push_str(text),
            NodeValue::Code(code) => out.push_str(&code.literal),
            // 公式留 TeX 源码：`$x^2$` → `x^2`
            NodeValue::Math(math) => out.push_str(&math.literal),
            // 段内换行保留成换行；硬换行同理
            NodeValue::SoftBreak | NodeValue::LineBreak => out.push('\n'),
            // 原始 HTML 内联标签（<br> / <b> 之类）直接丢掉
            NodeValue::HtmlInline(_) | NodeValue::EscapedTag(_) => {}
            NodeValue::FootnoteReference(reference) => {
                out.push('[');
                out.push_str(&reference.name);
                out.push(']');
            }
            // 链接与图片：只留可见文字（图片是 alt），地址不保留
            _ => walk_inline(child, out),
        }
    }
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
            render_plain_text,
            list_dir,
            clipboard_read,
            clipboard_write,
            watch_document,
        ])
        .run(tauri::generate_context!())
        .expect("启动 mdpad 失败");
}
