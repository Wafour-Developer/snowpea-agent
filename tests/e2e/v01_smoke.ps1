<#
.SYNOPSIS
v0.1 end-to-end smoke run for Windows (plan §7.9, M8 contract §6).

.DESCRIPTION
The PowerShell mirror of tests/e2e/v01_smoke.sh: the same fifteen steps, the
same PASS n / FAIL n / SKIP n lines, exit 0 only when no step failed. Steps 10
and 11 need real messenger credentials and run only with
SNOWPEA_E2E_CREDENTIALED=1.

Self-contained: a throwaway git repo, a throwaway SNOWPEA_HOME and a throwaway
uv tool directory under $env:TEMP. Every LLM call goes to the scripted fake
provider, so no API key is needed.

.PARAMETER FromCheckout
Install the checkout this script lives in (the default, and what CI uses).

.PARAMETER FromUrl
Install from the published install.ps1 URL instead.

.PARAMETER Keep
Leave the fixture repo and the home directory behind.
#>
[CmdletBinding()]
param(
    [switch]$FromCheckout,
    [switch]$FromUrl,
    [switch]$Keep
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$E2ERoot = if ($env:SNOWPEA_E2E_ROOT) { $env:SNOWPEA_E2E_ROOT } else { $env:TEMP }

$FixtureRepo = Join-Path $E2ERoot 'snowpea-fixture'
$E2EHome = Join-Path $E2ERoot 'snowpea-e2e-home'
$ToolRoot = Join-Path $E2ERoot 'snowpea-e2e-tools'
$FakeScript = Join-Path $RepoRoot 'tests\fixtures\providers\fake\e2e.json'
$SamplePlugin = Join-Path $RepoRoot 'tests\fixtures\plugins\sample-plugin'
$InstallUrl = 'https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.ps1'

if (-not $FromCheckout -and -not $FromUrl) {
    $FromCheckout = Test-Path (Join-Path $RepoRoot 'installer\install.ps1')
}

$script:Passed = 0
$script:Failed = 0
$script:Skipped = 0

function Pass-Step([int]$N, [string]$What) { Write-Host "PASS $N $What"; $script:Passed++ }
function Fail-Step([int]$N, [string]$Why) { Write-Host "FAIL $N $Why"; $script:Failed++ }
function Skip-Step([int]$N, [string]$Why) { Write-Host "SKIP $N $Why"; $script:Skipped++ }
function Note([string]$Text) { Write-Host "     $Text" }

$env:SNOWPEA_HOME = $E2EHome
$env:SNOWPEA_PROVIDER = "fake:$FakeScript"
$env:UV_TOOL_DIR = Join-Path $ToolRoot 'tools'
$env:UV_TOOL_BIN_DIR = Join-Path $ToolRoot 'bin'
$env:SNOWPEA_BIN_DIR = Join-Path $ToolRoot 'bin'
$env:Path = (Join-Path $ToolRoot 'bin') + ';' + $env:Path
Remove-Item Env:\SNOWPEA_TUI_ENTRY -ErrorAction SilentlyContinue

$Snowpea = Join-Path $ToolRoot 'bin\snowpea.exe'

function Invoke-Snowpea {
    # Runs `snowpea <args>`, returns the combined output, sets $script:LastRc.
    $output = & $Snowpea @args 2>&1 | Out-String
    $script:LastRc = $LASTEXITCODE
    return $output
}

function Reset-FixtureRepo {
    if (Test-Path $FixtureRepo) { Remove-Item -Recurse -Force $FixtureRepo }
    New-Item -ItemType Directory -Force -Path $FixtureRepo | Out-Null
    git -C $FixtureRepo init -q .
    git -C $FixtureRepo config user.email 'e2e@snowpea.invalid'
    git -C $FixtureRepo config user.name 'snowpea e2e'
    Set-Content -Path (Join-Path $FixtureRepo 'README.md') -Value '# snowpea e2e fixture' -NoNewline
    # The /ralph script patches these two; they must be tracked and unmodified.
    Set-Content -Path (Join-Path $FixtureRepo 'tracked_a.txt') -Value 'a'
    Set-Content -Path (Join-Path $FixtureRepo 'tracked_b.txt') -Value 'b'
    git -C $FixtureRepo add -A
    git -C $FixtureRepo commit -qm 'e2e fixture'
}

function Dirty-Count {
    return (@(git -C $FixtureRepo status --porcelain) | Where-Object { $_ }).Count
}

function Command-Registered([string]$Name) {
    $out = Invoke-Snowpea 'commands' 'list' '--json'
    return ($out -match """$Name""")
}

function Cleanup {
    if (Test-Path $Snowpea) { & $Snowpea daemon stop 2>&1 | Out-Null }
    if (-not $Keep) {
        foreach ($path in @($FixtureRepo, $E2EHome, $ToolRoot)) {
            if (Test-Path $path) { Remove-Item -Recurse -Force $path -ErrorAction SilentlyContinue }
        }
    }
    else {
        Note "kept $FixtureRepo, $E2EHome and $ToolRoot"
    }
}

Write-Host 'snowpea v0.1 end-to-end smoke'
Write-Host "  repo     $RepoRoot"
Write-Host "  fixture  $FixtureRepo"
Write-Host "  home     $E2EHome"
Write-Host ''

foreach ($path in @($E2EHome, $ToolRoot)) {
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}
New-Item -ItemType Directory -Force -Path $E2EHome, (Join-Path $ToolRoot 'bin') | Out-Null
Reset-FixtureRepo

try {
    # -----------------------------------------------------------------------
    # 1 — install
    # -----------------------------------------------------------------------
    if ($FromCheckout) {
        Note "installing from the checkout at $RepoRoot"
        & (Join-Path $RepoRoot 'installer\install.ps1') -FromCheckout 2>&1 | Out-String | Out-Null
        $installRc = $LASTEXITCODE
    }
    else {
        Note "installing from $InstallUrl"
        $script = (Invoke-WebRequest -UseBasicParsing $InstallUrl).Content
        $file = Join-Path $E2ERoot 'snowpea-install.ps1'
        Set-Content -Path $file -Value $script
        & powershell -NoProfile -ExecutionPolicy Bypass -File $file | Out-Null
        $installRc = $LASTEXITCODE
    }
    $versionOut = (Invoke-Snowpea '--version').Trim()
    if ($installRc -ne 0) {
        Fail-Step 1 "the installer exited $installRc"
    }
    elseif ($versionOut -match '^snowpea 0\.1\.') {
        Pass-Step 1 "installed; $versionOut"
    }
    else {
        Fail-Step 1 "snowpea --version printed '$versionOut', expected 'snowpea 0.1.x'"
    }

    if (-not (Test-Path $Snowpea)) {
        Write-Host ''
        Write-Host "FAIL: no snowpea executable at $Snowpea; the remaining steps cannot run"
        Write-Host "PASS=$script:Passed FAIL=$($script:Failed + 14) SKIP=$script:Skipped"
        exit 1
    }

    # -----------------------------------------------------------------------
    # 2 — setup writes settings.json
    # -----------------------------------------------------------------------
    Invoke-Snowpea 'setup' '--quick' '--vendor' 'deepseek' '--key' 'sk-e2e-fixture' | Out-Null
    $settings = Join-Path $E2EHome 'settings.json'
    if ($script:LastRc -eq 0 -and (Test-Path $settings) -and (Get-Content $settings -Raw) -match 'deepseek') {
        Pass-Step 2 'snowpea setup --quick wrote settings.json'
    }
    else {
        Fail-Step 2 "snowpea setup --quick did not record the vendor in $settings"
    }

    # -----------------------------------------------------------------------
    # 3 — daemon status
    # -----------------------------------------------------------------------
    $statusOut = Invoke-Snowpea 'daemon' 'status'
    $statusRc = $script:LastRc
    $port = 0
    if ($statusOut -match '(?m)^port\s+(\d+)') { $port = [int]$Matches[1] }
    if ($statusRc -ne 0) { Fail-Step 3 "snowpea daemon status exited $statusRc" }
    elseif ($port -le 0) { Fail-Step 3 'daemon status reported no usable port' }
    elseif ($statusOut -match 'will (not )?exit') { Pass-Step 3 "daemon up on port $port, lifecycle reason printed" }
    else { Fail-Step 3 'daemon status printed no keepalive reason' }

    # -----------------------------------------------------------------------
    # 4 — headless edit in accept mode
    # -----------------------------------------------------------------------
    Invoke-Snowpea '-c' 'edit README.md: add one line' '--mode' 'accept' '--cwd' $FixtureRepo | Out-Null
    $editRc = $script:LastRc
    $changed = (@(git -C $FixtureRepo diff --name-only) | Where-Object { $_ }).Count
    if ($editRc -eq 0 -and $changed -eq 1) { Pass-Step 4 'one file edited in accept mode' }
    else { Fail-Step 4 "exit $editRc, $changed file(s) changed (wanted exit 0 and 1 file)" }
    git -C $FixtureRepo checkout -q -- .

    # -----------------------------------------------------------------------
    # 5 — /help lists the nine built-ins
    # -----------------------------------------------------------------------
    $helpOut = Invoke-Snowpea '-c' '/help' '--json' '--cwd' $FixtureRepo
    $helpRc = $script:LastRc
    $missing = @()
    foreach ($name in @('ralph', 'ralplan', 'ultrawork', 'deepinit', 'deep-research', 'deep-interview', 'plan', 'accept', 'auto')) {
        if ($helpOut -notmatch [regex]::Escape("/$name")) { $missing += $name }
    }
    if ($helpRc -ne 0) { Fail-Step 5 "snowpea -c /help --json exited $helpRc" }
    elseif ($missing.Count -gt 0) { Fail-Step 5 "/help is missing: $($missing -join ', ')" }
    else { Pass-Step 5 '/help lists all nine built-in commands' }

    # -----------------------------------------------------------------------
    # 6 — tools list shows the media tools with a state
    # -----------------------------------------------------------------------
    $toolsOut = Invoke-Snowpea 'tools' 'list' '--json'
    if ($script:LastRc -ne 0) { Fail-Step 6 "snowpea tools list --json exited $($script:LastRc)" }
    elseif ($toolsOut -match '"image_generate"' -and $toolsOut -match '"video_generate"' -and $toolsOut -match '"state"') {
        Pass-Step 6 'media tools present with a state field'
    }
    else { Fail-Step 6 'media tools missing from tools list --json' }

    # -----------------------------------------------------------------------
    # 7 — /ralph
    # -----------------------------------------------------------------------
    if (-not (Command-Registered 'ralph')) {
        Skip-Step 7 '/ralph is not registered yet (pending US-019)'
    }
    else {
        Invoke-Snowpea '-c' "/ralph 'make tests pass'" '--mode' 'auto' '--cwd' $FixtureRepo | Out-Null
        $ralphRc = $script:LastRc
        $ralphChanged = Dirty-Count
        if ($ralphRc -eq 0 -and $ralphChanged -gt 0) { Pass-Step 7 "/ralph finished, $ralphChanged file(s) changed" }
        else { Fail-Step 7 "exit $ralphRc, $ralphChanged file(s) changed (wanted exit 0 and a non-empty diff)" }
        git -C $FixtureRepo checkout -q -- .
        git -C $FixtureRepo clean -qfd
    }

    # -----------------------------------------------------------------------
    # 8 — /team
    # -----------------------------------------------------------------------
    if (-not (Command-Registered 'team')) {
        Skip-Step 8 '/team is not registered yet (pending US-020)'
    }
    else {
        Invoke-Snowpea '-c' "/workers 2 'add docstrings'" '--mode' 'auto' '--cwd' $FixtureRepo | Out-Null
        $teamRc = $script:LastRc
        $teamStatus = Invoke-Snowpea 'team' 'status'
        $merged = ([regex]::Matches($teamStatus, 'merged')).Count
        $worktrees = (@(git -C $FixtureRepo worktree list) | Where-Object { $_ }).Count
        if ($teamRc -eq 0 -and $merged -eq 2 -and $worktrees -eq 1) {
            Pass-Step 8 '/team merged 2 tasks and left no worktree behind'
        }
        else { Fail-Step 8 "exit $teamRc, $merged merged task(s), $worktrees worktree(s) (wanted 0/2/1)" }
    }

    # -----------------------------------------------------------------------
    # 9 — plugin install: hook marker plus an MCP tool
    # -----------------------------------------------------------------------
    $marker = Join-Path $E2EHome 'fixture-hook.marker'
    if (Test-Path $marker) { Remove-Item -Force $marker }
    Invoke-Snowpea 'skill' 'install' $SamplePlugin | Out-Null
    $skillRc = $script:LastRc
    $mcpTool = (Invoke-Snowpea 'tools' 'list' '--json') -match 'mcp__fixture-echo__echo'
    Invoke-Snowpea '-c' 'run ls' '--mode' 'auto' '--cwd' $FixtureRepo | Out-Null
    if ($skillRc -ne 0) { Fail-Step 9 "snowpea skill install exited $skillRc" }
    elseif (-not $mcpTool) { Fail-Step 9 'the plugin''s MCP tool (mcp__fixture-echo__echo) did not register' }
    elseif ((Test-Path $marker) -and (Get-Item $marker).Length -gt 0) {
        Pass-Step 9 "plugin installed: MCP tool registered, PreToolUse hook wrote $(Get-Content $marker -Raw)"
    }
    else { Fail-Step 9 "the plugin's PreToolUse hook did not write $marker" }

    # -----------------------------------------------------------------------
    # 10, 11 — messenger delivery and approval timeout
    # -----------------------------------------------------------------------
    if ($env:SNOWPEA_E2E_CREDENTIALED -ne '1') {
        Skip-Step 10 'messenger delivery needs SNOWPEA_E2E_CREDENTIALED=1 and a channel'
        Skip-Step 11 'approval timeout needs SNOWPEA_E2E_CREDENTIALED=1 and a channel'
    }
    elseif (-not $env:SNOWPEA_E2E_CHANNEL) {
        Fail-Step 10 'SNOWPEA_E2E_CREDENTIALED=1 but SNOWPEA_E2E_CHANNEL is unset'
        Fail-Step 11 'SNOWPEA_E2E_CREDENTIALED=1 but SNOWPEA_E2E_CHANNEL is unset'
    }
    else {
        Invoke-Snowpea 'job' 'schedule' '--in' '60s' '--task' 'echo hi' '--channel' $env:SNOWPEA_E2E_CHANNEL | Out-Null
        if ($script:LastRc -eq 0) {
            Start-Sleep -Seconds 65
            if ((Invoke-Snowpea 'job' 'list') -match 'done|sent') {
                Pass-Step 10 "the scheduled job fired and was delivered to $env:SNOWPEA_E2E_CHANNEL"
            }
            else { Fail-Step 10 'the job did not report delivery within 65s' }
        }
        else { Fail-Step 10 'snowpea job schedule failed' }
        $approvals = Join-Path $E2EHome 'logs\approvals.jsonl'
        if ((Test-Path $approvals) -and (Get-Content $approvals -Raw) -match 'denied_by_timeout') {
            Pass-Step 11 'an approval timeout was recorded as denied_by_timeout'
        }
        else { Fail-Step 11 'no denied_by_timeout entry in logs/approvals.jsonl' }
    }

    # -----------------------------------------------------------------------
    # 12 — docker backend
    # -----------------------------------------------------------------------
    $dockerOk = $false
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        docker info 2>&1 | Out-Null
        $dockerOk = ($LASTEXITCODE -eq 0)
    }
    if (-not $dockerOk) {
        Skip-Step 12 'docker is not available on this machine'
    }
    else {
        # The confirmation arrives as a backend.changed event, hence --json.
        # tests/test_backends.py is what asserts the container is another host.
        $backendOut = Invoke-Snowpea '-c' '/backend docker' '--json' '--mode' 'auto' '--cwd' $FixtureRepo
        if ($script:LastRc -eq 0 -and $backendOut -match '"backend":\s*"docker"') {
            Pass-Step 12 'the session switched to the docker backend'
        }
        else { Fail-Step 12 "exit $($script:LastRc); /backend docker did not report a docker backend" }
    }

    # -----------------------------------------------------------------------
    # 13 — plan mode denies a write
    # -----------------------------------------------------------------------
    $foo = Join-Path $FixtureRepo 'foo.txt'
    if (Test-Path $foo) { Remove-Item -Force $foo }
    Invoke-Snowpea '--mode' 'plan' '-c' 'write foo.txt' '--cwd' $FixtureRepo | Out-Null
    $planRc = $script:LastRc
    if ($planRc -eq 4 -and -not (Test-Path $foo)) { Pass-Step 13 'plan mode denied the write (exit 4, no file)' }
    else { Fail-Step 13 "exit $planRc, file created: $(if (Test-Path $foo) { 'yes' } else { 'no' }) (wanted exit 4 and no file)" }

    # -----------------------------------------------------------------------
    # 14 — generated protocol artifacts are current
    # -----------------------------------------------------------------------
    $gen = Join-Path $RepoRoot 'scripts\gen_protocol.py'
    if (-not (Test-Path $gen)) {
        Skip-Step 14 'scripts/gen_protocol.py is not in this install'
    }
    else {
        Push-Location $RepoRoot
        python $gen --check 2>&1 | Out-Null
        $protocolRc = $LASTEXITCODE
        Pop-Location
        if ($protocolRc -eq 0) { Pass-Step 14 'gen_protocol.py --check reports no drift' }
        else { Fail-Step 14 "gen_protocol.py --check exited $protocolRc" }
    }

    # -----------------------------------------------------------------------
    # 15 — daemon stop leaves nothing behind
    # -----------------------------------------------------------------------
    $daemonJson = Join-Path $E2EHome 'daemon.json'
    $daemonPid = 0
    if (Test-Path $daemonJson) {
        try { $daemonPid = [int]((Get-Content $daemonJson -Raw | ConvertFrom-Json).pid) } catch { $daemonPid = 0 }
    }
    Invoke-Snowpea 'daemon' 'stop' | Out-Null
    $stopRc = $script:LastRc
    Start-Sleep -Seconds 1
    $alive = $false
    if ($daemonPid -gt 0) {
        $alive = [bool](Get-Process -Id $daemonPid -ErrorAction SilentlyContinue)
    }
    if ($stopRc -eq 0 -and -not $alive -and -not (Test-Path $daemonJson)) {
        Pass-Step 15 'daemon stopped, pid gone, daemon.json cleaned up'
    }
    else {
        Fail-Step 15 "exit $stopRc, pid alive: $alive, daemon.json present: $(Test-Path $daemonJson)"
    }
}
finally {
    Cleanup
}

Write-Host ''
Write-Host "PASS=$script:Passed FAIL=$script:Failed SKIP=$script:Skipped"
if ($script:Failed -gt 0) { exit 1 }
exit 0
