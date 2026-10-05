#Requires -Version 5.1
<#
.SYNOPSIS
  Install or upgrade the jtr CLI on Windows.

.DESCRIPTION
  Installs uv if it's missing, then installs jtr as a uv tool.

  One-liner (latest release, straight from GitHub):
    irm https://n-dimitrov.github.io/jtr/install.ps1 | iex

  Offline / pinned, from files you already downloaded - auto-detected
  next to this script:
    * a wheel (jtr-*.whl), or
    * a source tree (pyproject.toml) - the case when you extract the
      release source zip and run the bundled install.ps1.

  Set $env:JTR_VERSION (e.g. '1.1.0') to install that release instead of
  the latest. Re-running the installer upgrades.

.PARAMETER Path
  Explicit path to a jtr wheel, or to a source dir containing
  pyproject.toml. Overrides auto-detection.

.EXAMPLE
  irm https://n-dimitrov.github.io/jtr/install.ps1 | iex

.EXAMPLE
  # From an extracted release zip:
  powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
  # Install a wheel you downloaded:
  .\install.ps1 -Path .\jtr-1.1.0-py3-none-any.whl
#>
param(
    [string]$Path
)

# Everything runs in a child scope and fails by throwing: under
# `irm | iex` this code executes in the user's own session, where a stray
# `exit` would close their window and a leaked $ErrorActionPreference
# would outlive the install.
& {
    param([string]$Path, [string]$ScriptDir)

    $ErrorActionPreference = 'Stop'
    $repo = 'n-dimitrov/jtr'

    function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

    # Windows PowerShell 5.1 can still default to TLS 1.0, which GitHub refuses.
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    # --- resolve the install target ----------------------------------
    # Local files win when the script sits next to them (the extracted
    # release zip). Under `irm | iex` there is no script dir, and the cwd
    # is deliberately not searched: it could hold someone else's
    # pyproject.toml.
    function Resolve-LocalTarget {
        if ($Path) {
            if (-not (Test-Path $Path)) { throw "Path not found: $Path" }
            return (Resolve-Path $Path).Path
        }
        if (-not $ScriptDir) { return $null }
        $wheel = Get-ChildItem -Path $ScriptDir -Filter 'jtr-*.whl' -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        if ($wheel) { return $wheel.FullName }
        if (Test-Path (Join-Path $ScriptDir 'pyproject.toml')) { return $ScriptDir }
        return $null
    }

    # /releases/latest redirects to /releases/tag/vX.Y.Z - no API call, so
    # no rate limit to hit from behind a shared proxy.
    function Get-LatestVersion {
        $url = "https://github.com/$repo/releases/latest"
        try {
            $req = [System.Net.WebRequest]::Create($url)
            $req.Method = 'HEAD'
            $req.AllowAutoRedirect = $false
            if ($req.Proxy) {
                $req.Proxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials
            }
            $resp = $req.GetResponse()
            $location = $resp.Headers['Location']
            $resp.Close()
        } catch {
            throw "Couldn't reach github.com to find the latest jtr release: $($_.Exception.Message)"
        }
        if ($location -match '/releases/tag/v(\d+(\.\d+)+)$') { return $Matches[1] }
        throw "Couldn't determine the latest jtr release (got: $location)."
    }

    $target = Resolve-LocalTarget
    if (-not $target) {
        $version = if ($env:JTR_VERSION) { $env:JTR_VERSION -replace '^v', '' }
                   else { Get-LatestVersion }
        $target = "https://github.com/$repo/releases/download/v$version/jtr-$version-py3-none-any.whl"
    }

    # --- ensure uv ---------------------------------------------------
    if (-not (Have uv)) {
        Write-Host 'Installing uv...'
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        $uvBin = Join-Path $env:USERPROFILE '.local\bin'
        if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
        if (-not (Have uv)) {
            throw "uv installed but not on PATH. Open a new terminal and re-run, or add '$uvBin' to PATH."
        }
    }

    # --- install -----------------------------------------------------
    Write-Host "Installing jtr from $target"
    & uv tool install --force $target
    if ($LASTEXITCODE -ne 0) { throw 'uv tool install failed.' }

    # --- ensure the uv tool bin dir is on the user's PATH ------------
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

    # --- refresh the Claude Code skill -------------------------------
    # Copies of the /jtr skill installed by an older jtr would otherwise
    # go stale. Only existing, unedited copies are touched. Releases
    # before 1.2.0 have no `skill` command, hence the probe - run with
    # errors relaxed, since 5.1 turns native stderr into a terminating
    # error under 'Stop'.
    $jtr = Join-Path $binDir 'jtr.exe'
    if (Test-Path $jtr) {
        try {
            $ErrorActionPreference = 'Continue'
            & $jtr skill --help 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { & $jtr skill update }
        } catch {
        } finally {
            $ErrorActionPreference = 'Stop'
        }
    }

    Write-Host ''
    Write-Host "Installed. Run 'jtr' to start."
    if (-not (Have jtr)) {
        Write-Host "(If 'jtr' isn't found, open a new terminal so PATH is refreshed.)"
    } elseif (-not $onPath) {
        Write-Host "(PATH was updated - already-open terminals need to be restarted to see jtr.)"
    }
} $Path $PSScriptRoot
