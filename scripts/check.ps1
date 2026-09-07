<#
.SYNOPSIS
  Run every quality gate CI runs, in the same order, and report a summary.

.EXAMPLE
  .\scripts\check.ps1
  .\scripts\check.ps1 -Only backend
  .\scripts\check.ps1 -Skip build,test
  .\scripts\check.ps1 -ConfigLoader runner

.NOTES
  -ConfigLoader runner is an escape hatch for sandboxes that deny enumerating the
  user profile. Vite and Vitest normally bundle their config with esbuild, whose
  ancestor-directory walk then fails with EPERM before any test runs. "runner"
  loads the config through Vite's own module runner instead and needs no
  bundling. CI and normal developer machines must keep the "default" behaviour.
#>
[CmdletBinding()]
param(
  [ValidateSet("all", "backend", "frontend")] [string] $Only = "all",
  [string[]] $Skip = @(),
  [ValidateSet("default", "runner")] [string] $ConfigLoader = "default"
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$venvBin = Join-Path $backend ".venv\Scripts"
$results = [ordered]@{}
$viteExtra = if ($ConfigLoader -eq "runner") { @("--", "--configLoader", "runner") } else { @() }

function Invoke-Gate {
  param([string] $Name, [string] $WorkDir, [scriptblock] $Body)
  if ($Skip -contains $Name) { $script:results[$Name] = "skipped"; return }
  Write-Host ""
  Write-Host "==> $Name" -ForegroundColor Cyan
  Push-Location $WorkDir
  $gateError = $null
  try { & $Body } catch { $gateError = $_.Exception.Message } finally { Pop-Location }
  if ($gateError) {
    Write-Host $gateError -ForegroundColor Red
    $script:results[$Name] = "FAIL"
    return
  }
  $script:results[$Name] = if ($LASTEXITCODE -eq 0) { "pass" } else { "FAIL" }
}

function Invoke-Alembic {
  param([string[]] $Arguments)
  & (Join-Path $venvBin "alembic.exe") @Arguments
  if ($LASTEXITCODE -ne 0) { throw "alembic $($Arguments -join ' ') exited with $LASTEXITCODE" }
}

# Applies every revision to a throwaway SQLite database, proves the chain is
# reversible, and asks Alembic whether the ORM metadata still matches what the
# revisions build. Catches ORM/migration drift without needing PostgreSQL.
function Test-Migrations {
  $keys = @("DATABASE__URL", "APP__ENVIRONMENT", "LLM__PROVIDER", "DATA_MODE")
  $saved = @{}
  foreach ($key in $keys) { $saved[$key] = [Environment]::GetEnvironmentVariable($key) }
  $tmpDb = Join-Path ([System.IO.Path]::GetTempPath()) ("adoptimizer-migrations-" + [guid]::NewGuid().ToString("N") + ".db")
  $url = "sqlite+aiosqlite:///" + ($tmpDb -replace "\\", "/")
  try {
    $env:DATABASE__URL = $url
    $env:APP__ENVIRONMENT = "test"
    $env:LLM__PROVIDER = "mock"
    $env:DATA_MODE = "mock"
    Invoke-Alembic @("upgrade", "head")
    Invoke-Alembic @("check")
    Invoke-Alembic @("downgrade", "base")
    Invoke-Alembic @("upgrade", "head")
    Invoke-Alembic @("current")
  } finally {
    foreach ($key in $keys) {
      if ($null -eq $saved[$key]) { Remove-Item "Env:\$key" -ErrorAction SilentlyContinue }
      else { Set-Item "Env:\$key" $saved[$key] }
    }
    Remove-Item -LiteralPath $tmpDb -Force -ErrorAction SilentlyContinue
  }
}

if ($Only -in @("all", "backend")) {
  Invoke-Gate "ruff-format" $backend { & (Join-Path $venvBin "ruff.exe") format --check src tests }
  Invoke-Gate "ruff-lint"   $backend { & (Join-Path $venvBin "ruff.exe") check src tests }
  Invoke-Gate "mypy"        $backend { & (Join-Path $venvBin "mypy.exe") src }
  if (Test-Path (Join-Path $venvBin "alembic.exe")) {
    Invoke-Gate "migrations" $backend { Test-Migrations }
  } else {
    Write-Warning "backend\.venv\Scripts\alembic.exe missing; skipping migrations gate. Run .\scripts\setup.ps1"
  }
  Invoke-Gate "pytest"      $backend { & (Join-Path $venvBin "pytest.exe") --cov }
}

if ($Only -in @("all", "frontend")) {
  if (Test-Path (Join-Path $frontend "node_modules")) {
    Invoke-Gate "eslint"    $frontend { npm run lint }
    Invoke-Gate "tsc"       $frontend { npm run typecheck }
    Invoke-Gate "vitest"    $frontend { npm run test @viteExtra }
    Invoke-Gate "build"     $frontend { npm run build @viteExtra }
  } else {
    Write-Warning "frontend\node_modules missing; skipping frontend gates. Run .\scripts\setup.ps1"
  }
}

Write-Host ""
Write-Host "================ SUMMARY ================" -ForegroundColor White
$failed = 0
foreach ($key in $results.Keys) {
  $value = $results[$key]
  $colour = switch ($value) { "pass" { "Green" } "skipped" { "DarkGray" } default { "Red" } }
  if ($value -eq "FAIL") { $failed++ }
  Write-Host ("  {0,-14} {1}" -f $key, $value) -ForegroundColor $colour
}
Write-Host "=========================================" -ForegroundColor White
if ($failed -gt 0) { exit 1 }
exit 0