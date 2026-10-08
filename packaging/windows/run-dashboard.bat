@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  GT7 Web 仪表盘 —— 浏览器将打开 http://127.0.0.1:8787
echo ============================================
start "" "http://127.0.0.1:8787"
gt7-dashboard.exe --history "%~dp0data" --status "%~dp0data\status.json" --port 8787
echo.
echo 仪表盘已退出。
pause
