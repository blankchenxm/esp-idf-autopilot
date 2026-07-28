<#
.SYNOPSIS
    Read-only ESP chip identity probe for an already-preflighted board.

.DESCRIPTION
    This intentionally does not build or flash firmware.  The execution Harness
    calls it immediately before side effects to refresh the mutable COM locator
    and bind it to the stable chip/MAC identity established by Test-Hardware.ps1.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Port
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$activate = Join-Path $repo "activate.local.ps1"
if (-not (Test-Path $activate)) {
    Write-Error "activate.local.ps1 not found at $activate"
    exit 1
}

. $activate
esptool.py --chip auto --port $Port chip_id
exit $LASTEXITCODE
