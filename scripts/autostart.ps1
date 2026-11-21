# Liga/desliga a inicialização automática do Control Center com o Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1            # instala
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove    # remove
#
# Cria um atalho na pasta Inicializar do usuário que abre o start.bat MINIMIZADO.
# O alvo é cmd.exe (testado: respeita "Executar: Minimizado" no Windows 11;
# apontar o atalho para conhost.exe abre a janela normal).
param([switch]$Remove)

$startup  = [Environment]::GetFolderPath('Startup')
$lnkPath  = Join-Path $startup 'PC Control Center.lnk'
$root     = Split-Path -Parent $PSScriptRoot
$startBat = Join-Path $root 'start.bat'

if ($Remove) {
    if (Test-Path $lnkPath) { Remove-Item $lnkPath; Write-Host "Removido: $lnkPath" }
    else { Write-Host 'Nada para remover.' }
    exit 0
}

if (-not (Test-Path $startBat)) { Write-Error "start.bat não encontrado em $root"; exit 1 }

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath       = Join-Path $env:SystemRoot 'System32\cmd.exe'
$lnk.Arguments        = "/c `"`"$startBat`" --min`""   # --min: o servidor também se minimiza
$lnk.WorkingDirectory = $root
$lnk.WindowStyle      = 7   # 7 = minimizado
$lnk.IconLocation     = Join-Path $env:SystemRoot 'System32\imageres.dll,109'
$lnk.Description      = 'Servidor do painel do iPad (PC Control Center)'
$lnk.Save()
Write-Host "Atalho criado: $lnkPath"
