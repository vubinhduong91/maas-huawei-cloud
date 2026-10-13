<#
.SYNOPSIS
  Hub-fix lab: one short Claude Code session (variant 2b of the key-order lab,
  scenarios/prompts/keyorder-short.txt + the lab-tfs MCP server) from this Windows PC, logged in
  with a Claude subscription, through tap-pre -> APISIX -> tap-post.

.DESCRIPTION
  -Client sonnet : scripts\claude-maas.ps1 -ClaudeSonnet -TapPre. Claude Code sends its own names
                   (claude-sonnet-*, claude-haiku-*); the gateway (P3/P4/P5) maps Sonnet to GLM,
                   haiku/opus go to Anthropic (passthrough).
  -Client glm    : scripts\claude-maas.ps1 -TapPre. The sonnet tier is glm-5.3 on the client, so a
                   gateway that does not touch the body (P0) can serve it; haiku/opus still Anthropic.
  The gateway profile is set on the server beforehand (make up MODE=B LAB_GATEWAY_PROFILE=...);
  -Gateway / -Workers are only recorded. The repo is reset to the same commit before every
  session. One line per session is appended to the server's logs/hubfix/sessions.jsonl
  (read by scenarios/hubfix_phase1.py).

.EXAMPLE
  .\scenarios\hubfix_win_session.ps1 -Label p1-P3-a -Client sonnet -Gateway P3 -Workers 4
#>
param(
    [Parameter(Mandatory = $true)] [string] $Label,
    [ValidateSet("sonnet", "glm")] [string] $Client = "sonnet",
    [string] $Gateway = "?",
    [string] $Workers = "?",
    [string] $Effort = "",      # passed to every prompt as --effort (empty: Claude Code's default)
    [string] $Server = "root@111.119.197.251",
    [string] $IdentityFile = "$env:USERPROFILE\.ssh\maas_ecs",
    [string] $ConfigDir = "$env:USERPROFILE\.claude-hubfix",
    [string] $Repo = "$env:USERPROFILE\maas-lab\sample-repo-hubfix",
    [string] $Prompts = "",
    # Run in a copy of the repo without .git: Claude Code then sends JSON nulls in the body
    # (safeguards git_state), the case that broke plain core.json.stably_encode.
    [switch] $NoGit
)
$ErrorActionPreference = "Stop"
$lab = Split-Path $PSScriptRoot -Parent
$launcher = Join-Path $lab "scripts\claude-maas.ps1"
if (-not $Prompts) { $Prompts = Join-Path $lab "scenarios\prompts\keyorder-short.txt" }
$commit = "3f0246356944e622407cb9c57328d808bbc9da42"   # same pin as scripts/prepare-sample-repo.sh
$git = @("-c", "user.name=lab", "-c", "user.email=lab@localhost", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false")
# Same tool allowance as scenarios/session_script.sh (variant 2b).
$allowed = "Read,Grep,Glob,Edit,MultiEdit,Write,TaskCreate,TaskUpdate,TaskGet,TaskList,Agent," +
           "Bash(git diff:*),Bash(git status:*),Bash(git log:*),Bash(ls:*),Bash(wc:*),Bash(grep:*),mcp__lab-tfs"

function Invoke-Git { param([Parameter(ValueFromRemainingArguments = $true)] $a)
    $ErrorActionPreference = "Continue"
    & git @git -C $Repo @a 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git $a failed" }
}

if (-not (Test-Path (Join-Path $Repo ".git"))) {
    New-Item -ItemType Directory -Force $Repo | Out-Null
    Invoke-Git init -q -b lab
    Invoke-Git remote add origin https://github.com/Reaparr/Reaparr.git
    Invoke-Git fetch -q --depth 1 origin $commit
    Invoke-Git checkout -q -B lab FETCH_HEAD
    Copy-Item (Join-Path $lab "sample\CLAUDE.md") (Join-Path $Repo "CLAUDE.md")
    Invoke-Git add CLAUDE.md
    Invoke-Git commit -q -m "lab: add CLAUDE.md"
    Write-Host "hubfix-session: prepared $Repo"
}
Invoke-Git reset -q --hard lab
Invoke-Git clean -qfd
if ($NoGit) {
    $copy = "$Repo-nogit"
    if (Test-Path $copy) { Remove-Item -Recurse -Force $copy }
    & robocopy $Repo $copy /E /XD .git /NFL /NDL /NJH /NJS /NP | Out-Null
    $Repo = $copy
}

$runId = Get-Date -Format "yyyyMMdd-HHmmss"
$state = Join-Path $env:TEMP "hubfix-session"
New-Item -ItemType Directory -Force $state | Out-Null
$tfsState = Join-Path $state "lab-tfs-state-$runId.json"
$mcpCfg = Join-Path $state "mcp-lab-tfs-$runId.json"
# the WindowsApps python.exe is only the Microsoft Store stub
$py = @(Get-Command python -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch "WindowsApps" })[0].Source
if (-not $py) { $py = @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue)[-1].FullName }
if (-not $py) { throw "no Python found (needed for the lab-tfs MCP server)" }
@{ mcpServers = @{ "lab-tfs" = @{ command = $py; args = @((Join-Path $lab "scenarios\mcp\lab_tfs.py"));
                                  env = @{ LAB_TFS_STATE = $tfsState } } } } |
    ConvertTo-Json -Depth 6 | Set-Content -Path $mcpCfg -Encoding ASCII

$turns = @(Get-Content $Prompts | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith("#") } |
           ForEach-Object { $_ -replace "^mcp: ", "" })
$version = (& claude --version) -join " "
$session = [guid]::NewGuid().ToString()
$started = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Write-Host "hubfix-session: $Label client=$Client gateway=$Gateway w$Workers effort=$(if ($Effort) { $Effort } else { 'default' }), $($turns.Count) prompts, $version, session $session"

$done = 0; $errors = 0
Push-Location $Repo
try {
    for ($i = 0; $i -lt $turns.Count; $i++) {
        $a = @("-Server", $Server, "-IdentityFile", $IdentityFile, "-TapPre", "-ConfigDir", $ConfigDir)
        if ($Client -eq "sonnet") { $a += "-ClaudeSonnet" }
        # Embedded double quotes (the MCP prompt has JSON) must be escaped for powershell -File;
        # claude-maas.ps1 escapes them again for claude.
        $a += @("-p", ($turns[$i] -replace '(\\*)"', '$1$1\"'), "--output-format", "json", "--permission-mode", "acceptEdits",
                "--strict-mcp-config", "--mcp-config", $mcpCfg, "--allowedTools", $allowed)
        if ($Effort) { $a += @("--effort", $Effort) }
        if ($i -eq 0) { $a += @("--session-id", $session) } else { $a += @("--resume", $session) }
        $t0 = Get-Date
        $ErrorActionPreference = "Continue"
        $out = & powershell -NoProfile -File $launcher @a 2>$null
        $ErrorActionPreference = "Stop"
        $line = @($out | Where-Object { $_ -match '^\s*\{' })[-1]
        $o = if ($line) { try { $line | ConvertFrom-Json } catch { $null } } else { $null }
        $done = $i + 1
        if (-not $o -or $o.is_error) { $errors++ }
        $sid = if ($o) { $o.session_id } else { "-" }
        if ($o -and $sid -ne $session) { Write-Host "  warning: prompt $done ran in session $sid" }
        Write-Host ("  prompt {0}/{1}: {2:n0}s turns={3} error={4}" -f $done, $turns.Count,
                    ((Get-Date) - $t0).TotalSeconds, $(if ($o) { $o.num_turns } else { "-" }), $(if ($o) { $o.is_error } else { "no-json" }))
    }
} finally {
    Pop-Location
}

$rec = [ordered]@{ run_id = $runId; label = $Label; session = $session; client = $Client; gateway = $Gateway
                   workers = $Workers; effort = $(if ($Effort) { $Effort } else { "default" }); claude_version = $version; repo_commit = $commit; prompts_done = $done
                   prompts_total = $turns.Count; prompt_errors = $errors; started = $started
                   ended = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($rec | ConvertTo-Json -Compress) + "`n"))
& ssh -o BatchMode=yes -i $IdentityFile $Server "mkdir -p /opt/maas-lab/logs/hubfix && echo $b64 | base64 -d >> /opt/maas-lab/logs/hubfix/sessions.jsonl"
Write-Host "hubfix-session: $Label done ($done/$($turns.Count) prompts, $errors with error), session $session"
