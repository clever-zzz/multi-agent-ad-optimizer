<#
.SYNOPSIS
  Remove build artefacts, tool caches and the local SQLite database.

.DESCRIPTION
  Mirrors `make clean`. Never touches source files, .env files or virtualenvs.
  Use -IncludeNodeModules to also drop frontend\node_modules.
#>
[CmdletBinding()]
param(
  [switch] $IncludeNodeModules
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot

$targets = @(
  "frontend\dist",
  "frontend\coverage",
  "frontend\node_modules\.vite",
  "backend\.pytest_cache",
  "backend\.mypy_cache",
  "backend\.ruff_cache",
  "backend\htmlcov",
  "backend\.coverage",
  "backend\coverage.xml",
  "backend\adoptimizer.db",
  "backend\adoptimizer.db-journal"
)

if ($IncludeNodeModules) { $targets += "frontend\node_modules" }

foreach ($relative in $targets) {
  $full = Join-Path $root $relative
  if (-not (Test-Path -LiteralPath $full)) { continue }
  # Guard: only ever delete inside this repository.
  if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Warning "Refusing to delete outside the repo: $full"
    continue
  }
  Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "removed $relative" -ForegroundColor DarkGray
}

Get-ChildItem -Path (Join-Path $root "backend") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "Clean complete." -ForegroundColor Green