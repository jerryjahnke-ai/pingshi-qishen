@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command "$startup=[Environment]::GetFolderPath('Startup'); Remove-Item -LiteralPath (Join-Path $startup 'PingShiQiShen.lnk') -Force -ErrorAction SilentlyContinue; $exe=Join-Path $env:LOCALAPPDATA 'PingShiQiShen\PingShiQiShen.exe'; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $exe -or $_.CommandLine -like '*PingShiQiShen.exe*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; if (Test-Path -LiteralPath $exe) { Remove-Item -LiteralPath $exe -Force }; Write-Host 'Removed autostart and installed app. Usage history is kept.'"

echo.
echo Done.
if "%PINGSHIQISHEN_NOPAUSE%"=="" pause
