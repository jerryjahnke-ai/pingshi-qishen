@echo off
setlocal

set "SOURCE_EXE="
for %%F in ("%~dp0*.exe") do (
  set "SOURCE_EXE=%%~fF"
  goto :found_exe
)

:found_exe
if not defined SOURCE_EXE (
  echo Could not find the app exe in this folder.
  pause
  exit /b 1
)

set "APP_DIR=%LOCALAPPDATA%\PingShiQiShen"
set "APP_EXE=%APP_DIR%\PingShiQiShen.exe"

if not exist "%APP_DIR%" mkdir "%APP_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$exe=Join-Path $env:LOCALAPPDATA 'PingShiQiShen\PingShiQiShen.exe'; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $exe } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Start-Sleep -Milliseconds 800"
copy /Y "%SOURCE_EXE%" "%APP_EXE%" >nul
if errorlevel 1 (
  echo Install failed because the app exe could not be copied.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$startup=[Environment]::GetFolderPath('Startup'); Remove-Item -LiteralPath (Join-Path $startup 'AppUsageTimer.lnk') -Force -ErrorAction SilentlyContinue; Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and $_.CommandLine -like '*AppTimer.pyw*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; $shortcut=Join-Path $startup 'PingShiQiShen.lnk'; $wsh=New-Object -ComObject WScript.Shell; $s=$wsh.CreateShortcut($shortcut); $s.TargetPath=(Join-Path $env:LOCALAPPDATA 'PingShiQiShen\PingShiQiShen.exe'); $s.WorkingDirectory=(Join-Path $env:LOCALAPPDATA 'PingShiQiShen'); $s.Description='PingShiQiShen app usage timer and posture reminder'; $s.WindowStyle=1; $s.Save()"

start "" "%APP_EXE%"

echo.
echo Installed and started.
echo You can close this window.
if "%PINGSHIQISHEN_NOPAUSE%"=="" pause
