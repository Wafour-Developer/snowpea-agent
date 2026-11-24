# installed by snowpea; reinstalling or updating the integration overwrites this file.
# add your own hooks beside this file instead of editing it.
# SNOWPEA_INTEGRATION_ID=snowpea-agent-state
# SNOWPEA_INTEGRATION_VERSION=1
#
# Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0.
#
# Usage: snowpea-agent-state.ps1 <cli> <action>   (the hook's JSON arrives on stdin)
# Reports the CLI's session id and state to the snowpea terminal it runs in.
# Outside a snowpea terminal (SNOWPEA_TERMINAL_ID unset) it does nothing.

param([string]$Cli = "", [string]$Action = "")

# Antigravity CLI wants a JSON object on stdout from every hook; we never inject anything.
if ($Cli -eq "agy") { [Console]::Out.WriteLine("{}") }

if ([string]::IsNullOrEmpty($env:SNOWPEA_TERMINAL_ID) -or [string]::IsNullOrEmpty($env:SNOWPEA_TERMINAL_REPORT)) { exit 0 }

$bin = $env:SNOWPEA_CORE_BIN
if ([string]::IsNullOrEmpty($bin)) {
    $found = Get-Command snowpea-core -ErrorAction SilentlyContinue
    if (-not $found) { exit 0 }
    $bin = $found.Source
}

# Read the hook input as UTF-8 bytes whatever the console code page is (CP949 on Korean Windows).
try {
    $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), [System.Text.Encoding]::UTF8)
    $inputText = $reader.ReadToEnd()
} catch {
    $inputText = ""
}

try {
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $inputText | & $bin hook report --cli $Cli --action $Action 2>$null | Out-Null
} catch {
}
exit 0
