<#
.SYNOPSIS
  Windows-side convenience wrapper over deploy\package.py. Builds the offline air-gap package
  for the box (RHEL8 / tcsh / no network).

.DESCRIPTION
  1. Picks a Python (repo .venv first, then 'python', then 'py -3').
  2. Verifies it is 3.10+ with a QUOTE-FREE probe. Windows PowerShell 5.1 strips embedded
     double quotes when passing arguments to a native executable, so the probe prints an integer
     version code (310) instead of a quoted string. The DESK interpreter does not have to be 3.11:
     `pip download --python-version 311 --platform ...` fetches the box's wheels from any Python.
     Only the BOX needs 3.11 (apply checks that there).
  3. Calls package.py, which cross-downloads the cp311 / x86_64 / manylinux2014 wheels, audits
     them against glibc 2.17, writes MANIFEST.json + SHA256SUMS (LF), and stages app/.
  4. Lists what to carry to the box.

  All text artifacts are written LF by package.py. Do not re-save them with a Windows editor.

.PARAMETER Mode
  full (default) = source + wheels. incremental = only changed files + a delete list; needs
  -Previous pointing at the package you last shipped.

.PARAMETER Out
  Package directory to build. Default dist\pkg.

.PARAMETER Previous
  Incremental only: the previous package directory (or its MANIFEST.json) to diff against.

.PARAMETER DryRun
  Do everything except the network download; wheels come from the local cache.

.PARAMETER Tar
  Also emit <Out>.tar.gz plus a .sha256 sidecar.

.EXAMPLE
  .\deploy\package.ps1
.EXAMPLE
  .\deploy\package.ps1 -Mode incremental -Previous dist\pkg -Out dist\pkg_i
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\package.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet('full', 'incremental')]
    [string]$Mode = 'full',
    [string]$Out = 'dist\pkg',
    [string]$Previous = '',
    [switch]$DryRun,
    [switch]$Tar
)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --- pick an interpreter (any 3.10+: the cp311 wheels are cross-downloaded, not host-matched) ---
$exe = $null
$pre = @()
$venv = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $venv) {
    $exe = $venv
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $exe = 'python'
}
elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $exe = 'py'; $pre = @('-3')
}
else {
    throw 'No Python found. Install Python 3.10+ (with pip) and retry.'
}

# Quote-free version probe: 3.10 -> 310. PS 5.1 would eat embedded double quotes here.
$ver = (& $exe @pre -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])')
$ver = "$ver".Trim()
if ([int]$ver -lt 310) {
    throw "Need Python 3.10+ to run the packager; probed version code = $ver (310 = 3.10)."
}

Write-Host "[pkg] python : $exe $($pre -join ' ')  (version code $ver)"
Write-Host "[pkg] mode   : $Mode"
Write-Host "[pkg] out    : $Out"

if ($Mode -eq 'incremental' -and -not $Previous) {
    throw 'Incremental needs -Previous <previous package dir>, e.g. -Previous dist\pkg'
}

$argv = @((Join-Path $Root 'deploy\package.py'), '--out', $Out)
if ($Mode -eq 'incremental') { $argv += @('--incremental', $Previous) } else { $argv += '--full' }
if ($DryRun) { $argv += '--dry-run' }
if ($Tar) { $argv += '--tar' }

& $exe @pre @argv
if ($LASTEXITCODE -ne 0) {
    throw "package.py failed (exit $LASTEXITCODE). Read the output above: an AUDIT FAIL means a wheel needs glibc newer than 2.17 - downpin it in requirements.txt and re-run."
}

Write-Host ''
Write-Host "== built: $Out =="
Get-ChildItem -Path $Out -ErrorAction SilentlyContinue |
    Select-Object Name, @{N = 'MB'; E = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime |
    Format-Table -AutoSize
if ($Tar) {
    $name = Split-Path -Leaf $Out
    $dir = Split-Path -Parent (Resolve-Path $Out)
    Write-Host "Upload these 3 files from $dir into one folder on the box (e.g. <workarea>/pmukit):"
    Write-Host "    $name.tar.gz   $name.tar.gz.sha256   pmukit_install.sh"
    Write-Host 'then, in that folder:'
    Write-Host '    bash pmukit_install.sh'
}
else {
    Write-Host 'Carry the whole directory to the box, then:'
    Write-Host '    cd <package>'
    Write-Host '    bash apply'
}
