$ErrorActionPreference = "Stop"
$rootDir = Split-Path -Parent $PSScriptRoot
Set-Location $rootDir

Write-Host "=== AI Agent Builder ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/4] Installing pyinstaller..." -ForegroundColor Yellow
pip install pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "[2/4] Cleaning old builds..." -ForegroundColor Yellow
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }

Write-Host "[3/4] Building main executable..." -ForegroundColor Yellow
pyinstaller --clean build.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

Write-Host "[4/4] Done!" -ForegroundColor Green
Write-Host ""
Write-Host "Output:" -ForegroundColor Cyan
Write-Host "  dist\ai-agent.exe"
Write-Host ""
Write-Host "Usage:" -ForegroundColor Cyan
Write-Host "  dist\ai-agent.exe -w . -a AI_dev_team"
Write-Host ""

Read-Host "Press Enter to exit"
