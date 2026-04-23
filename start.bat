@echo off
chcp 65001 >nul 2>&1
echo ======================================
echo   Clash Dashboard 启动脚本
echo ======================================
echo.

cd /d "%~dp0backend"

REM 检测端口是否已被占用（比进程名检测更可靠）
netstat -ano | findstr ":8080.*LISTEN" >nul 2>&1
if %errorlevel% equ 0 (
    echo [警告] 端口 8080 已被占用，请先执行 stop.bat 关闭后再启动。
    pause
    exit /b 1
)

REM 启动 launcher（后台窗口）
start "ClashLauncher" python launcher.py

echo [启动中] 等待服务就绪...
ping -n 9 127.0.0.1 >nul 2>&1

REM 检查端口
netstat -ano | findstr ":8080.*LISTEN" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Backend  0.0.0.0:8080
) else (
    echo [ERR] Backend 未启动，请检查 backend.log
)

netstat -ano | findstr ":9090.*LISTEN" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Mihomo   0.0.0.0:9090
) else (
    echo [ERR] Mihomo 未启动，请检查 mihomo.log
)

echo.
echo 访问: http://localhost:8080
echo ======================================
exit /b 0
