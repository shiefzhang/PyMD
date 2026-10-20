// Windows 发布版不弹出背后的黑色控制台窗口
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    pymd_lib::run()
}
