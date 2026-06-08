$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $repoRoot ".venv"
$python = Join-Path $venvDir "Scripts\python.exe"
$pyinstaller = Join-Path $venvDir "Scripts\pyinstaller.exe"
$source = Join-Path $repoRoot "src\pingshi_qishen.pyw"
$buildRoot = Join-Path $repoRoot "build"
$distRoot = Join-Path $repoRoot "dist"
$appName = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("5bGP5pe26LW36Lqr"))
$zipName = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("5bGP5pe26LW36LqrLeaZrumAmueUqOaIt+eJiC56aXA="))
$packageDir = Join-Path $distRoot $appName
$zipPath = Join-Path $distRoot $zipName

if (!(Test-Path -LiteralPath $python)) {
  python -m venv $venvDir
}

& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $repoRoot "requirements-dev.txt")

if (Test-Path -LiteralPath $buildRoot) {
  Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $distRoot) {
  Remove-Item -LiteralPath $distRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $distRoot | Out-Null

& $pyinstaller `
  --clean `
  --noconfirm `
  --onefile `
  --windowed `
  --name $appName `
  --distpath (Join-Path $buildRoot "pyinstaller-dist") `
  --workpath (Join-Path $buildRoot "pyinstaller-build") `
  --specpath $buildRoot `
  $source

New-Item -ItemType Directory -Force -Path $packageDir | Out-Null
Copy-Item -LiteralPath (Join-Path $buildRoot "pyinstaller-dist\$appName.exe") -Destination (Join-Path $packageDir "$appName.exe") -Force
Copy-Item -Path (Join-Path $repoRoot "packaging\*") -Destination $packageDir -Force

Compress-Archive -LiteralPath $packageDir -DestinationPath $zipPath -Force
Write-Host "Built release package: $zipPath"
