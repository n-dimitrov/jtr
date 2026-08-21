#Requires -Version 5.1
<#
.SYNOPSIS
  Install the jtr CLI on Windows from local release files.

.DESCRIPTION
  Installs uv if it's missing, then installs jtr as a uv tool from files
  you already downloaded — no GitHub, no network calls to a repo.

  Auto-detected next to this script:
    * a wheel (jtr-*.whl), or
    * a source tree (pyproject.toml) — the case when you extract the
      release source zip and run the bundled install.ps1.

.PARAMETER Path
  Explicit path to a jtr wheel, or to a source dir containing
  pyproject.toml. Overrides auto-detection.

.EXAMPLE
  # Recommended: extract the release zip, then from the extracted folder:
  .\install.ps1

.EXAMPLE
  # Install a wheel you downloaded:
  .\install.ps1 -Path .\jtr-0.9.0-py3-none-any.whl
#>
[CmdletBinding()]
param(
    [string]$Path
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Error $msg; exit 1 }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Where does this script live? (falls back to cwd if run oddly)
$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }

# --- resolve the install target --------------------------------------
# Returns a wheel path, or a source directory containing pyproject.toml.
function Resolve-Target {
    if ($Path) {
        if (-not (Test-Path $Path)) { Fail "Path not found: $Path" }
        return (Resolve-Path $Path).Path
    }
    $wheel = Get-ChildItem -Path $here -Filter 'jtr-*.whl' -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($wheel) { return $wheel.FullName }
    if (Test-Path (Join-Path $here 'pyproject.toml')) { return $here }
    return $null
}

$target = Resolve-Target
if (-not $target) {
    Fail @"
Couldn't find jtr to install.

Put this script next to either:
  * the extracted release source folder (contains pyproject.toml), or
  * a jtr-*.whl file

...then re-run it. Or point it explicitly:
  .\install.ps1 -Path C:\path\to\jtr-x.y.z-py3-none-any.whl
"@
}

# --- ensure uv -------------------------------------------------------
if (-not (Have uv)) {
    Write-Host 'Installing uv...'
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $uvBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
    if (-not (Have uv)) {
        Fail "uv installed but not on PATH. Open a new terminal and re-run, or add '$uvBin' to PATH."
    }
}

# --- install ---------------------------------------------------------
if (Test-Path $target -PathType Container) {
    Write-Host "Installing jtr from source: $target"
} else {
    Write-Host "Installing jtr from wheel: $target"
}
& uv tool install --force $target
if ($LASTEXITCODE -ne 0) { Fail 'uv tool install failed.' }

# --- ensure the uv tool bin dir is on the user's PATH ----------------
# uv places launchers (jtr.exe) in UV_TOOL_BIN_DIR, defaulting to
# %USERPROFILE%\.local\bin. If uv was preinstalled (winget, pip, ...)
# that dir may never have been added to PATH, leaving jtr unreachable.
$binDir = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR }
          else { Join-Path $env:USERPROFILE '.local\bin' }

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$onPath = ($userPath -split ';' | Where-Object { $_ }) -contains $binDir
if (-not $onPath) {
    Write-Host "Adding '$binDir' to your user PATH..."
    $newPath = if ($userPath) { "$userPath;$binDir" } else { $binDir }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
}
# Make jtr resolvable in this session too.
if (($env:Path -split ';') -notcontains $binDir) {
    $env:Path = "$binDir;$env:Path"
}

Write-Host ''
Write-Host "Installed. Run 'jtr' to start."
if (-not (Have jtr)) {
    Write-Host "(If 'jtr' isn't found, open a new terminal so PATH is refreshed.)"
} elseif (-not $onPath) {
    Write-Host "(PATH was updated - already-open terminals need to be restarted to see jtr.)"
}
