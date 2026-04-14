@echo off
set BOT_DIR=C:\Users\ander\Downloads\gemini-research-mcp\.claude\worktrees\charming-feistel\weatherbot
set PYTHON=C:\Users\ander\AppData\Local\Microsoft\WindowsApps\python.exe
set LOG=%BOT_DIR%\weatherbot_live.log
echo [%date% %time%] Arrancando WeatherBot scanner... >> "%LOG%"
cd /d "%BOT_DIR%"
timeout /t 20 /nobreak >/dev/null
:loop
echo [%date% %time%] Iniciando bot_v2.py... >> "%LOG%"
"%PYTHON%" bot_v2.py >> "%LOG%" 2>&1
echo [%date% %time%] Caido, reiniciando en 30s... >> "%LOG%"
timeout /t 30 /nobreak >/dev/null
goto loop
