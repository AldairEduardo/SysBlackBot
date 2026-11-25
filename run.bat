@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual...
    python -m venv .venv
    if errorlevel 1 goto :error
)

echo Comprobando dependencias...
".venv\Scripts\python.exe" -c "import telegram, httpx, cryptography, dotenv, playwright, apscheduler; from zoneinfo import ZoneInfo; ZoneInfo('America/Lima')" >nul 2>&1
if errorlevel 1 (
    echo Instalando dependencias...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check
    if errorlevel 1 goto :error
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Se creo el archivo .env.
    echo Pega tu BOT_TOKEN de @BotFather y guarda el archivo.
    echo.
    start notepad ".env"
    pause
)

".venv\Scripts\python.exe" bot.py
if errorlevel 1 pause
goto :end

:error
echo.
echo Ocurrio un error. Revisa los mensajes de arriba.
pause

:end
endlocal
