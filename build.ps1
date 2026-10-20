<#
.SYNOPSIS
    Build GenerousUnseen.exe from GenerousUnseen.spec with PyInstaller.

.DESCRIPTION
    Resolves a Python launcher, ensures PyInstaller (and the icon) are present,
    then runs PyInstaller on the one-file, windowed, UAC-admin spec. The result
    is dist\GenerousUnseen.exe.

.PARAMETER Clean
    Remove build\ and dist\ before building.

.PARAMETER Run
    Launch the built exe when the build succeeds (triggers a UAC prompt).

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Clean
    .\build.ps1 -Clean -Run
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Run
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$Spec = "GenerousUnseen.spec"
$ExePath = Join-Path $PSScriptRoot "dist\GenerousUnseen.exe"

# --- resolve a Python launcher (prefer the py launcher, then python) ---------
$Py = $null
$PyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $Py = "py"
    $PyArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Py = "python"
} else {
    throw "No Python found on PATH. Install Python 3.11+ (with the 'py' launcher) and retry."
}
Write-Host "Using Python: $Py $($PyArgs -join ' ')" -ForegroundColor Cyan
& $Py @PyArgs --version

# --- sanity: spec present ----------------------------------------------------
if (-not (Test-Path $Spec)) {
    throw "Build spec '$Spec' not found in $PSScriptRoot."
}

# --- ensure the icon exists (regenerate from make_icon.py if missing) --------
if (-not (Test-Path "icon.ico")) {
    if (Test-Path "make_icon.py") {
        Write-Host "icon.ico missing - generating via make_icon.py..." -ForegroundColor Yellow
        & $Py @PyArgs make_icon.py
        if ($LASTEXITCODE -ne 0) { throw "make_icon.py failed (exit $LASTEXITCODE)." }
    } else {
        throw "icon.ico is missing and make_icon.py is not present to regenerate it."
    }
}

# --- ensure PyInstaller is installed -----------------------------------------
& $Py @PyArgs -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller not found - installing..." -ForegroundColor Yellow
    & $Py @PyArgs -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed (exit $LASTEXITCODE)." }
}

# --- optional clean ----------------------------------------------------------
if ($Clean) {
    foreach ($dir in @("build", "dist")) {
        if (Test-Path $dir) {
            Write-Host "Removing $dir\ ..." -ForegroundColor Yellow
            Remove-Item -Recurse -Force $dir
        }
    }
}

# --- build -------------------------------------------------------------------
Write-Host "Building $Spec ..." -ForegroundColor Cyan
& $Py @PyArgs -m PyInstaller $Spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed (exit $LASTEXITCODE)." }

# --- verify result -----------------------------------------------------------
if (-not (Test-Path $ExePath)) {
    throw "Build reported success but $ExePath was not produced."
}
$sizeMB = [math]::Round((Get-Item $ExePath).Length / 1MB, 1)
Write-Host ""
Write-Host "Build OK -> $ExePath ($sizeMB MB)" -ForegroundColor Green
Write-Host "Note: warnings in build\GenerousUnseen\warn-GenerousUnseen.txt are normal PyInstaller noise." -ForegroundColor DarkGray

# --- optional run (elevates via UAC, per the spec's admin manifest) ----------
if ($Run) {
    Write-Host "Launching (a UAC prompt is expected)..." -ForegroundColor Cyan
    Start-Process -FilePath $ExePath
}
