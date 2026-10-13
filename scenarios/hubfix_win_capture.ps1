<#
.SYNOPSIS
  Hub-fix lab Phase 1: Claude Code on Windows (Claude subscription login) at each effort level,
  through tap-pre -> APISIX (P3) -> MaaS / Anthropic.

.DESCRIPTION
  One `claude -p` per case via scripts\claude-maas.ps1 -ClaudeSonnet -TapPre, so Claude Code
  sends its own model names: sonnet -> claude-sonnet-* (the gateway maps it to GLM), haiku ->
  claude-haiku-* (passthrough to Anthropic). Each case gets a fixed --session-id; the lines
  written to -Out use the same format as logs/hubfix/capture.jsonl on the server, so
  scenarios/hubfix_extract.py --capture <file> reads them.

.EXAMPLE
  .\scenarios\hubfix_win_capture.ps1 -Server root@1.2.3.4 -IdentityFile $env:USERPROFILE\.ssh\maas_ecs `
      -ConfigDir $env:USERPROFILE\.claude-hubfix -Out logs\hubfix\capture-win.jsonl
#>
param(
    [Parameter(Mandatory = $true)] [string] $Server,
    [string] $IdentityFile = "",
    [Parameter(Mandatory = $true)] [string] $ConfigDir,
    [string] $Out = "logs\hubfix\capture-win.jsonl",
    [string[]] $Cases = @("S-default", "S-low", "S-medium", "S-high", "S-xhigh", "S-max", "H-default")
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repo "scripts\claude-maas.ps1"
$prompt = "Reply with exactly the word OK and nothing else. Do not use any tools."
$work = Join-Path $env:TEMP "hubfix-capture"
New-Item -ItemType Directory -Force $work, (Split-Path -Parent $Out) | Out-Null
$Out = (Resolve-Path (Split-Path -Parent $Out)).Path + "\" + (Split-Path -Leaf $Out)   # cwd changes below
$utf8 = New-Object System.Text.UTF8Encoding($false)   # no BOM: the extractor reads it with json.loads
$version = (& claude --version) -join " "

foreach ($case in $Cases) {
    $extra = $null
    switch -Regex ($case) {
        "^S-default$" { $extra = @("--model", "sonnet"); break }
        "^S-(.+)$"    { $extra = @("--model", "sonnet", "--effort", $Matches[1]) }
        "^H-default$" { $extra = @("--model", "haiku") }
    }
    if ($null -eq $extra) { Write-Host "unknown case $case"; continue }
    $sid = [guid]::NewGuid().ToString()
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $a = @("-Server", $Server, "-ClaudeSonnet", "-TapPre", "-ConfigDir", $ConfigDir, "-NoBackgroundTraffic")
    if ($IdentityFile) { $a += @("-IdentityFile", $IdentityFile) }
    $a += @("-p", $prompt, "--session-id", $sid, "--output-format", "json") + $extra
    Push-Location $work
    $ErrorActionPreference = "Continue"
    # A child powershell (as in s9.ps1): fresh env per case, -p etc. reach claude untouched.
    $text = (& powershell -NoProfile -File $launcher @a 2>&1 | Out-String)
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    Pop-Location
    $result = $null
    foreach ($line in $text -split "`r?`n") {
        $i = $line.IndexOf("{")
        if ($i -lt 0) { continue }
        try { $d = $line.Substring($i) | ConvertFrom-Json } catch { continue }
        if ($d.type -eq "result") {
            $result = [ordered]@{ is_error = $d.is_error; api_error_status = $d.api_error_status; result = $d.result; num_turns = $d.num_turns }
            break
        }
    }
    if (-not $result) { $result = @{ raw = $text.Substring([Math]::Max(0, $text.Length - 400)) } }
    $rec = [ordered]@{ case = $case; session = $sid; ts = $ts; rc = $rc; env = "windows claude $version"
                       args = ($extra -join " "); claude = $result }
    [System.IO.File]::AppendAllText($Out, ($rec | ConvertTo-Json -Compress -Depth 5) + "`n", $utf8)
    Write-Host "$case rc=$rc session=$sid $($result | ConvertTo-Json -Compress)"
}
