<#
.SYNOPSIS
    One-shot hardware test gate. Run THIS single command before starting a project.

.DESCRIPTION
    Activates ESP-IDF (via the repo-root activate.local.ps1) and runs the preflight
    gate: build + flash ESP-IDF's hello_world, then read "Hello world!" back over serial.
    Watch the final PASS / FAIL line.

      PASS -> port, flash, and serial all work. Hardware is good; you can hand off.
      FAIL -> the failing step is named (no port / build / flash / no serial).

.EXAMPLE
    .\hwtest\Test-Hardware.ps1
    .\hwtest\Test-Hardware.ps1 -Port COM4 -Chip esp32
#>
param(
    [string]$Port = "",
    [string]$Chip = "esp32",
    [switch]$Keep
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$activate = Join-Path $repo "activate.local.ps1"

if (-not (Test-Path $activate)) {
    Write-Host "Missing $activate" -ForegroundColor Red
    Write-Host "Copy activate.local.ps1.example to activate.local.ps1 and set your ESP-IDF activation, then retry." -ForegroundColor Yellow
    exit 1
}

Write-Host "=== Activating ESP-IDF ===" -ForegroundColor Cyan
. $activate

$argList = @()
if ($Port) { $argList += @("--port", $Port) }
$argList += @("--chip", $Chip)
if ($Keep) { $argList += "--keep" }

Write-Host "=== Running hardware preflight ===" -ForegroundColor Cyan
python (Join-Path $here "preflight.py") @argList
exit $LASTEXITCODE
