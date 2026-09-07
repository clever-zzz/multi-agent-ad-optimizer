<#
.SYNOPSIS
  One-shot local setup: backend virtualenv + dependencies, frontend packages.

.DESCRIPTION
  Mirrors `make install` for Windows. Pass -Proxy when your network needs an
  HTTP proxy to reach PyPI or the npm registry.

.EXAMPLE
  .\scripts\setup.ps1
  .\scripts\setup.ps1 -Proxy http://127.0.0.1:7897
#>
[CmdletBinding()]
param(
  [string] $Proxy,
  [string] $PythonExe = "python",
  [switch] $SkipBackend,
  [switch] $SkipFrontend
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

if ($Proxy) {
  $env:HTTP_PROXY = $Proxy
  $env:HTTPS_PROXY = $Proxy
  $env:NPM_CONFIG_PROXY = $Proxy
  $env:NPM_CONFIG_HTTPS_PROXY = $Proxy
  Write-Host "Using proxy $Proxy" -ForegroundColor DarkGray
}

function Invoke-Step {
  param([string] $Name, [scriptblock] $Body)
  Write-Host ""
  Write-Host "==> $Name" -ForegroundColor Cyan
  & $Body
  if ($LASTEXITCODE -ne 0) {
    throw "$Name failed with exit code $LASTEXITCODE"
  }
}

if (-not $SkipBackend) {
  $venvPython = Join-Path $backend ".venv\Scripts\python.exe"
  if (-not (Test-Path $venvPython)) {
    Invoke-Step "Create backend virtualenv" {
      Push-Location $backend
      try { & $PythonExe -m venv .venv } finally { Pop-Location }
    }
  } else {
    Write-Host "Reusing existing backend\.venv" -ForegroundColor DarkGray
  }

  Invoke-Step "Upgrade pip" {
    & $venvPython -m pip install --upgrade pip --disable-pip-version-check
  }

  Invoke-Step "Install backend (dev + analytics extras)" {
    Push-Location $backend
    try { & $venvPython -m pip install -e ".[dev,analytics]" --disable-pip-version-check } finally { Pop-Location }
  }

  Invoke-Step "Copy backend\.env.example" {
    $target = Join-Path $backend ".env"
    if (Test-Path $target) {
      Write-Host "backend\.env already exists, leaving it alone" -ForegroundColor DarkGray
    } else {
      Copy-Item (Join-Path $backend ".env.example") $target
      Write-Host "Created backend\.env (edit SECURITY__JWT_SECRET before any real deployment)" -ForegroundColor Yellow
    }
  }
}

if (-not $SkipFrontend) {
  Invoke-Step "Install frontend packages" {
    Push-Location $frontend
    try {
      if (Test-Path "package-lock.json") {
        # Reproducible install, same as CI and Dockerfile.frontend. If this
        # fails with EUSAGE the lockfile has drifted from package.json: run
        # `npm install`, then commit package.json and package-lock.json together.
        npm ci
      } else {
        Write-Host "No package-lock.json - falling back to npm install (commit the lockfile)" -ForegroundColor Yellow
        npm install
      }
    } finally { Pop-Location }
  }

  Invoke-Step "Copy frontend\.env.example" {
    $target = Join-Path $frontend ".env"
    if (Test-Path $target) {
      Write-Host "frontend\.env already exists, leaving it alone" -ForegroundColor DarkGray
    } else {
      Copy-Item (Join-Path $frontend ".env.example") $target
    }
  }
}

Write-Host ""
Write-Host "Setup complete. Next: .\scripts\dev.ps1" -ForegroundColor Green