@echo off
chcp 65001 >nul
title pyMD build

rem ---------------------------------------------------------------
rem 本机 C:\Users\Pyrrhus\.rustup 下的 rustc.exe 会被安全策略
rem 间歇性阻断（表现为 cargo 报 "could not execute process rustc /
rem os error 2"，但文件其实存在）。
rem 规避办法：使用 D 盘的完整工具链副本。请勿改掉下面这行 PATH。
rem ---------------------------------------------------------------
set PATH=D:\rust-toolchain\bin;%PATH%
set CARGO_TARGET_DIR=D:\mdpad-target

cd /d "%~dp0"

echo ============================
echo  [1/2] 构建前端 (Vite + TS)
echo ============================
call npm run build
if errorlevel 1 goto fail

echo.
echo ============================
echo  [2/2] 编译 Rust release
echo ============================
cd src-tauri
rem --features custom-protocol 必须带：否则产物会去连 localhost:1420（dev server）
cargo build --release --features custom-protocol
if errorlevel 1 goto fail

echo.
echo ============================
echo  构建成功
echo ============================
echo 可执行文件：
dir /b "%CARGO_TARGET_DIR%\release\*.exe"
echo 完整路径：%CARGO_TARGET_DIR%\release\pymd.exe
cd ..
pause
exit /b 0

:fail
echo.
echo !!! 构建失败，请把上面的错误信息发给我。
pause
exit /b 1
