@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  GT7 遥测接收器 —— 请保持本窗口开着
echo  数据目录: %~dp0data
echo ============================================
gt7-recorder.exe --output "%~dp0data" --status-file "%~dp0data\status.json" --decryptor "%~dp0gt7-decrypt.exe" --verbose
echo.
echo 接收器已退出。
pause
