@echo off
REM Exemplo de script .bat disparado pelo painel (roda sem janela: "hidden": true).
ipconfig /flushdns > "%TEMP%\controlcenter_flushdns.log" 2>&1
