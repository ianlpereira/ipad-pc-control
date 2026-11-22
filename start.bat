@echo off
REM ==========================================================
REM  PC Control Center - inicia o servidor no PC
REM
REM    start.bat           inicia (na 1a vez: instala e abre o assistente)
REM    start.bat --setup   refaz a configuracao pelo assistente
REM    start.bat --min     inicia minimizado (usado pelo atalho do Windows)
REM ==========================================================
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :deps

REM ---- 1o uso: procura um Python 3.11+ (launcher "py" ou "python" no PATH)
set "PY="
for %%V in (3.13 3.12 3.14 3.11) do (
    if not defined PY ( py -%%V -c "" >nul 2>&1 && set "PY=py -%%V" )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython
echo [setup] Criando ambiente virtual (%PY%)...
%PY% -m venv .venv || goto :erro

:deps
REM ---- Instala/atualiza dependencias quando o requirements.txt muda (ex.: depois de um git pull)
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo [setup] Instalando dependencias, aguarde...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :erro
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

REM ---- Assistente: sem config.json ou com --setup (nunca no atalho minimizado)
set "WIZ="
if not exist "config.json" set "WIZ=1"
for %%A in (%*) do (
    if /i "%%~A"=="--setup" set "WIZ=1"
    if /i "%%~A"=="--min" set "MIN=1"
)
if defined MIN set "WIZ="
if defined WIZ (
    ".venv\Scripts\python.exe" -m server.wizard
    if not exist "config.json" (
        echo.
        echo Configuracao cancelada. Rode start.bat de novo quando quiser.
        pause
        exit /b 1
    )
)

REM %* repassa argumentos (ex.: --min)
".venv\Scripts\python.exe" -m server.main %*
pause
exit /b 0

:nopython
echo.
echo Python 3.11 ou mais novo nao encontrado.
echo Instale em https://www.python.org/downloads/ (recomendado: 3.13)
echo e marque "Add python.exe to PATH" no instalador. Depois rode start.bat de novo.
pause
exit /b 1

:erro
echo.
echo Falha na instalacao. Veja a mensagem acima.
echo Dica: com Python muito novo, algumas dependencias podem nao ter versao pronta;
echo instale o Python 3.13, apague a pasta .venv e rode start.bat de novo.
pause
exit /b 1
