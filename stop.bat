@echo off
chcp 65001 >nul 2>&1
echo ======================================
echo   Clash Dashboard 关闭脚本
echo ======================================
echo.

echo [Step 1] 通过 HTTP 命令关闭 launcher...
powershell -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:9099/shutdown' -TimeoutSec 5 -UseBasicParsing | Out-Null; echo   HTTP 关闭命令已发送 } catch { echo   launcher 未运行或端口不可达 }"

echo.
echo [Step 2] 等待 3 秒让进程退出...
ping -n 4 127.0.0.1 >nul 2>&1

echo.
echo [Step 3] 确认端口已释放...
netstat -ano | findstr ":8080.*LISTEN" >nul 2>&1 && echo   8080 仍占用 || echo   8080 已释放
netstat -ano | findstr ":9090.*LISTEN" >nul 2>&1 && echo   9090 仍占用 || echo   9090 已释放
netstat -ano | findstr ":7890.*LISTEN" >nul 2>&1 && echo   7890 仍占用 || echo   7890 已释放

echo.
echo ======================================
echo [完成] 如需重新启动，请执行 start.bat
echo ======================================
