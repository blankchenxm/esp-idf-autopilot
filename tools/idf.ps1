# idf.ps1 - run idf.py in an ESP-IDF-activated shell, from any fresh shell.
#
# Codex and Claude tool calls can start a new, un-activated shell every time,
# and a bare `idf.py` can resolve to a bogus launcher (reports v1.0.3).
# This wrapper self-activates ESP-IDF, so `idf.py` always hits the real v6.0.
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
#   powershell -ExecutionPolicy Bypass -File tools\idf.ps1 -C projects\crumb build
#
# All arguments after the script name are forwarded verbatim to idf.py.

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$activate = Join-Path $repo 'activate.local.ps1'

if (-not (Test-Path $activate)) {
    Write-Error "activate.local.ps1 not found at $activate - cannot activate ESP-IDF. Create it from activate.local.ps1.example (see README.md)."
    exit 1
}

. $activate

# Codex can execute under an isolated Windows account while the machine-local IDF
# checkout is owned by the interactive user.  Keep the exception process-local;
# never mutate the user's global Git configuration from automation.
$gitConfigCount = 0
if ($env:GIT_CONFIG_COUNT -and -not [int]::TryParse($env:GIT_CONFIG_COUNT, [ref]$gitConfigCount)) {
    $gitConfigCount = 0
}
Set-Item -Path "Env:GIT_CONFIG_KEY_$gitConfigCount" -Value 'safe.directory'
Set-Item -Path "Env:GIT_CONFIG_VALUE_$gitConfigCount" -Value $env:IDF_PATH
$env:GIT_CONFIG_COUNT = [string]($gitConfigCount + 1)

# Resolve the version-matched compiler to an absolute path.  This is equivalent
# to ESP-IDF's PATH lookup, while remaining reliable when a managed Windows
# runner starts CMake under a different security token.
$extraIdfArgs = @()
if ($true) {
    $forwarded = @($args)
    $projectDir = $repo
    $target = $null
    for ($i = 0; $i -lt $forwarded.Count; $i++) {
        if ($forwarded[$i] -eq '-C' -and $i + 1 -lt $forwarded.Count) {
            $projectDir = $forwarded[$i + 1]
        }
        if ($forwarded[$i] -eq 'set-target' -and $i + 1 -lt $forwarded.Count) {
            $target = $forwarded[$i + 1]
        }
    }
    if (-not $target) {
        $sdkconfig = Join-Path $projectDir 'sdkconfig'
        if (Test-Path $sdkconfig) {
            $targetLine = Select-String -Path $sdkconfig -Pattern '^CONFIG_IDF_TARGET="([^"]+)"$' | Select-Object -First 1
            if ($targetLine) { $target = $targetLine.Matches[0].Groups[1].Value }
        }
    }
    $compilerPrefix = switch -Regex ($target) {
        '^esp32$' { 'xtensa-esp32-elf'; break }
        '^esp32s2$' { 'xtensa-esp32s2-elf'; break }
        '^esp32s3$' { 'xtensa-esp32s3-elf'; break }
        '^esp32(c2|c3|c5|c6|h2|p4)$' { 'riscv32-esp-elf'; break }
        default { $null }
    }
    if ($compilerPrefix) {
        $cc = Get-Command "$compilerPrefix-gcc" -ErrorAction SilentlyContinue
        $cxx = Get-Command "$compilerPrefix-g++" -ErrorAction SilentlyContinue
        if ($cc) {
            $env:CC = $cc.Source
            $env:ASM = $cc.Source
            $compilerBin = Split-Path $cc.Source -Parent
            $env:CMAKE_PROGRAM_PATH = $compilerBin
            $extraIdfArgs += @('-D', "CMAKE_PROGRAM_PATH=$compilerBin")
            $extraIdfArgs += @('-D', "CMAKE_C_COMPILER=$($cc.Source)")
            $extraIdfArgs += @('-D', "CMAKE_ASM_COMPILER=$($cc.Source)")
        }
        if ($cxx) {
            $env:CXX = $cxx.Source
            $extraIdfArgs += @('-D', "CMAKE_CXX_COMPILER=$($cxx.Source)")
        }
    }
}

& idf.py @extraIdfArgs @args
exit $LASTEXITCODE
