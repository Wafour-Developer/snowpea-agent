<#
.SYNOPSIS
Snowpea installer for Windows (M8 contract §2, plan §3.4, AC-01).

.DESCRIPTION
    irm https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.ps1 | iex

Installs uv and Node LTS with winget when they are missing, installs the
`snowpea-agent` tool with `uv tool install`, puts the uv tool bin directory on
the user PATH, and sets SNOWPEA_HOME to %LOCALAPPDATA%\snowpea (contract §2:
that is the Windows default for agent state).

Idempotent — every step checks first. Any failure exits non-zero after printing
the exact command to run by hand.

.PARAMETER DryRun
Print the planned steps, change nothing, exit 0.

.PARAMETER FromCheckout
Install the checkout this script lives in (CI and E2E use this).

.PARAMETER Force
Reinstall even when `snowpea` is already present.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$FromCheckout,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/Wafour-Developer/snowpea-agent'
$DefaultSource = "git+$RepoUrl"
$MinNodeMajor = 20

function Say([string]$Message) { Write-Host "snowpea: $Message" }
function Step([string]$Message) { Write-Host "  -> $Message" }
function Plan([string]$Message) { Write-Host "  [dry-run] $Message" }

function Die([string]$Message, [string]$Manual) {
    Write-Host "snowpea: error: $Message" -ForegroundColor Red
    if ($Manual) {
        Write-Host "snowpea: run this by hand, then re-run the installer:"
        Write-Host "    $Manual"
    }
    exit 1
}

function Have([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Winget([string]$PackageId, [string]$Label) {
    if (-not (Have 'winget')) {
        Die "$Label is missing and winget is not available" "install $Label manually, then re-run this script"
    }
    Say "installing $Label ($PackageId)"
    winget install --id $PackageId --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Die "winget could not install $Label" "winget install --id $PackageId"
    }
    Update-SessionPath
}

function Update-SessionPath {
    # winget and uv write the user PATH; re-read it so the rest of this run
    # sees what they installed without a new shell.
    $parts = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
        [Environment]::GetEnvironmentVariable('Path', 'User')
        "$env:USERPROFILE\.local\bin"
    ) | Where-Object { $_ }
    $env:Path = ($parts -join ';')
}

function Ensure-Uv {
    if (Have 'uv') {
        Step "uv already installed ($(uv --version))"
        return
    }
    if ($DryRun) { Plan 'winget install astral-sh.uv'; return }
    Invoke-Winget 'astral-sh.uv' 'uv'
    if (-not (Have 'uv')) {
        Die 'uv installed but is not on PATH' '$env:Path += ";$env:USERPROFILE\.local\bin"'
    }
}

function Get-NodeMajor {
    if (-not (Have 'node')) { return 0 }
    $raw = (node --version) -replace '^v', ''
    return [int]($raw -split '\.')[0]
}

function Ensure-Node {
    $major = Get-NodeMajor
    if ($major -ge $MinNodeMajor) {
        Step "node already installed (v$major)"
        return
    }
    if ($DryRun) { Plan 'winget install OpenJS.NodeJS.LTS'; return }
    Invoke-Winget 'OpenJS.NodeJS.LTS' "Node $MinNodeMajor+"
    if ((Get-NodeMajor) -lt $MinNodeMajor) {
        Die "Node $MinNodeMajor+ is still not on PATH" 'install Node LTS from https://nodejs.org/en/download'
    }
}

function Get-CheckoutRoot {
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-InstallSource {
    if ($FromCheckout) { return (Get-CheckoutRoot) }
    if ($env:SNOWPEA_WHEEL_URL) { return $env:SNOWPEA_WHEEL_URL }
    if ($env:SNOWPEA_INSTALL_SOURCE) { return $env:SNOWPEA_INSTALL_SOURCE }
    return $DefaultSource
}

function Install-Snowpea {
    $source = Get-InstallSource
    $uvArgs = @('tool', 'install')
    if ($FromCheckout) { $uvArgs += @('--force', '--editable') }
    elseif ($Force) { $uvArgs += '--force' }
    $uvArgs += $source

    if ($DryRun) { Plan "uv $($uvArgs -join ' ')"; return }
    if (-not $Force -and -not $FromCheckout -and (Have 'snowpea')) {
        Step 'snowpea is already installed; re-run with -Force to reinstall'
        return
    }
    Say "installing snowpea from $source"
    & uv @uvArgs
    if ($LASTEXITCODE -ne 0) {
        Die 'uv tool install failed' "uv $($uvArgs -join ' ')"
    }
    Update-SessionPath
}

function Ensure-SnowpeaHome {
    $home_ = Join-Path $env:LOCALAPPDATA 'snowpea'
    if ($DryRun) { Plan "set SNOWPEA_HOME=$home_ for the current user"; return }
    if ([Environment]::GetEnvironmentVariable('SNOWPEA_HOME', 'User')) {
        Step 'SNOWPEA_HOME is already set for this user'
    }
    else {
        [Environment]::SetEnvironmentVariable('SNOWPEA_HOME', $home_, 'User')
        Step "SNOWPEA_HOME set to $home_"
    }
    if (-not $env:SNOWPEA_HOME) { $env:SNOWPEA_HOME = $home_ }
    New-Item -ItemType Directory -Force -Path $env:SNOWPEA_HOME | Out-Null
}

function Ensure-Path {
    $binDir = if ($env:SNOWPEA_BIN_DIR) { $env:SNOWPEA_BIN_DIR } else { "$env:USERPROFILE\.local\bin" }
    if ($DryRun) { Plan "ensure $binDir is on the user PATH"; return }
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($user -and ($user -split ';' | Where-Object { $_ -eq $binDir })) {
        Step "$binDir is already on the user PATH"
    }
    else {
        $parts = @($user, $binDir) | Where-Object { $_ }
        [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
        Step "added $binDir to the user PATH (open a new shell to pick it up)"
    }
    Update-SessionPath
}

Say 'installing on windows'
if ($DryRun) { Say 'dry run: planned steps' }
Ensure-Uv
Ensure-Node
Install-Snowpea
Ensure-SnowpeaHome
Ensure-Path

if ($DryRun) {
    Plan 'snowpea --version'
    Say 'dry run complete; nothing was changed'
    exit 0
}

if (-not (Have 'snowpea')) {
    Die 'snowpea is installed but not on PATH' '$env:Path += ";$env:USERPROFILE\.local\bin"'
}
Write-Host ''
snowpea --version
if ($LASTEXITCODE -ne 0) { Die 'snowpea --version failed' 'snowpea --version' }

Write-Host @"

Next:
  snowpea setup      configure a provider (API key or browser login)
  snowpea            start the agent
  snowpea --help     every subcommand

Docs: $RepoUrl
"@
