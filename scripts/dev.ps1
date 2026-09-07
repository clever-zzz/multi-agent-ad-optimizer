<#
.SYNOPSIS
  Prepare the database and run the API plus the web console side by side.

.DESCRIPTION
  Mirrors `make dev` for Windows. The API listens on :8000, the Vite dev server
  on :5173 and proxies /api, /healthz, /readyz and /metrics to the API, so the
  browser never needs CORS in development.

.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -SkipSeed -BackendOnly
#>
[CmdletBinding()]
param(
  [switch] $SkipMigrate,
  [switch] $SkipSeed,
  [switch] $BackendOnly,
  [switch] $FrontendOnly,
  [int] $BackendPort = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
  throw "backend\.venv is missing. Run .\scripts\setup.ps1 first."
}

if (-not $FrontendOnly) {
  if (-not (Test-Path (Join-Path $backend ".env"))) {
    Copy-Item (Join-Path $backend ".env.example") (Join-Path $backend ".env")
    Write-Host "Created backend\.env from the example" -ForegroundColor Yellow
  }

  if (-not $SkipMigrate) {
    Write-Host "==> Applying migrations" -ForegroundColor Cyan
    Push-Location $backend
    try { & $venvPython -m adoptimizer.cli migrate } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Write-Warning "migrate exited $LASTEXITCODE" }
  }

  if (-not $SkipSeed) {
    Write-Host "==> Seeding demo dataset" -ForegroundColor Cyan
    Push-Location $backend
    try { & $venvPython -m adoptimizer.cli seed } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Write-Warning "seed exited $LASTEXITCODE" }
  }
}

$jobs = @()

if (-not $FrontendOnly) {
  Write-Host "==> API on http://localhost:$BackendPort (docs at /docs)" -ForegroundColor Green
  $jobs += Start-Job -Name "adoptimizer-api" -WorkingDirectory $backend -ScriptBlock {
    param($py, $port)
    & $py -m uvicorn adoptimizer.main:app --reload --app-dir src --host 127.0.0.1 --port $port
  } -ArgumentList $venvPython, $BackendPort
}

if (-not $BackendOnly) {
  if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    throw "frontend\node_modules is missing. Run .\scripts\setup.ps1 first."
  }
  Write-Host "==> Web console on http://localhost:5173" -ForegroundColor Green
  $env:VITE_BACKEND_URL = "http://localhost:$BackendPort"
  $jobs += Start-Job -Name "adoptimizer-web" -WorkingDirectory $frontend -ScriptBlock {
    npm run dev
  }
}

Write-Host ""
Write-Host "Press Ctrl+C to stop. Default login: admin@adoptimizer.dev / Adm1n!ChangeMe" -ForegroundColor Yellow
Write-Host ""

try {
  while ($true) {
    foreach ($job in $jobs) {
      Receive-Job -Job $job | ForEach-Object { Write-Host ("[{0}] {1}" -f $job.Name, $_) }
      if ($job.State -eq "Failed") {
        Write-Error "Job $($job.Name) failed; see output above."
      }
    }
    Start-Sleep -Milliseconds 500
  }
} finally {
  foreach ($job in $jobs) { Stop-Job -Job $job -ErrorAction SilentlyContinue; Remove-Job -Job $job -Force -ErrorAction SilentlyContinue }
  Write-Host "Stopped." -ForegroundColor DarkGray
}