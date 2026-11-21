# Exemplo de script PowerShell disparado pelo painel do iPad.
# Abre numa janela nova; troque pelo que quiser automatizar.
Write-Host "Olá do Control Center! $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor Cyan
Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name, CPU, WS | Format-Table
Read-Host "Enter para fechar"
