@echo off
REM ==========================================================
REM  Control Center - inicia o servidor no PC
REM  1o uso: cria o venv e instala as dependencias.
REM ==========================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Criando ambiente virtual...
    python -m venv .venv || goto :erro
    echo [setup] Instalando dependencias...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :erro
)

REM %* repassa argumentos (ex.: --min, usado pelo atalho de inicializacao do Windows)
".venv\Scripts\python.exe" -m server.main %*
pause
exit /b 0

:erro
echo.
echo Falha na instalacao. Verifique se o Python 3.11+ esta no PATH.
pause
exit /b 1
