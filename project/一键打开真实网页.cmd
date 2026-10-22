@echo off
chcp 65001 >nul
setlocal
title 第 2 组 - 网络管理网页
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\open_windows_dashboard.ps1" %*
set "launch_code=%errorlevel%"
if not "%launch_code%"=="0" (
  echo.
  echo 启动未完成，请查看上面的提示。按任意键关闭窗口。
  pause >nul
)
exit /b %launch_code%
