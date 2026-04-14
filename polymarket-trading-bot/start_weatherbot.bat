@echo off
set BOT_DIR=C:\Users\ander\Downloads\gemini-research-mcp\.claude\worktrees\charming-feistel\polymarket-trading-bot
set PYTHON=C:\Users\ander\AppData\Local\Microsoft\WindowsApps\python.exe
set LOG=%BOT_DIR%\weatherbot_startup.log
echo [%date% %time%] Arrancando WeatherBot... >> "%LOG%"
cd /d "%BOT_DIR%"
timeout /t 15 /nobreak >/dev/null
:loop
echo [%date% %time%] Iniciando... >> "%LOG%"
"%PYTHON%" -u weatherbot_bridge.py >> "%LOG%" 2>&1
echo [%date% %time%] Caido, reiniciando en 30s... >> "%LOG%"
timeout /t 30 /nobreak >/dev/null
goto loop
