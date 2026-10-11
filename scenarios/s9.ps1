<#
.SYNOPSIS
  S9 (Windows): the first turns of S2 with real Claude Sonnet (Claude subscription) and with
  glm-5.3 (MaaS), same repo, same prompts, same Claude Code config.

.DESCRIPTION
  Runs on the PC that is logged in to Claude Code, through scripts/claude-maas.ps1 (SSH
  tunnel -> APISIX mode B -> tap -> MaaS or api.anthropic.com). The server needs
  ANTHROPIC_PASSTHROUGH=1 so Claude models are routed through the tap and recorded.
  Kept small on purpose (-Turns 5, read-only prompts) to spare the subscription.
  Sessions are labelled scenario S9 in the server's logs/scenarios.jsonl; then run
  `make analyze` on the server and compare the two sessions (report.md -> By model).

.EXAMPLE
  .\scenarios\s9.ps1
  .\scenarios\s9.ps1 -Turns 3 -Variants claude
#>
param(
    [string] $Server = "root@111.119.197.251",
    [string] $IdentityFile = "$env:USERPROFILE\.ssh\maas_ecs",
    [int] $Turns = 5,
    [string[]] $Variants = @("glm", "claude"),
    [string] $RunId = (Get-Date -Format "yyyyMMdd-HHmmss"),
    [string] $Repo = "$env:USERPROFILE\maas-lab\sample-repo"
)
$ErrorActionPreference = "Stop"
$lab = Split-Path $PSScriptRoot -Parent
$launcher = Join-Path $lab "scripts\claude-maas.ps1"
$commit = "3f0246356944e622407cb9c57328d808bbc9da42"   # same pin as scripts/prepare-sample-repo.sh
$tools = "Read,Grep,Glob,LS,Edit,MultiEdit,Write,TodoWrite,Bash(git diff:*),Bash(git status:*),Bash(git log:*),Bash(ls:*),Bash(find:*),Bash(wc:*)"
$git = @("-c", "user.name=lab", "-c", "user.email=lab@localhost", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false")

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
    Write-Host "s9: prepared $Repo"
}

$prompts = Get-Content (Join-Path $lab "scenarios\prompts\coding-15.txt") |
    Where-Object { $_.Trim() -and -not $_.StartsWith("#") } | Select-Object -First $Turns

function Add-Label([string] $session, [string] $variant) {
    $json = (@{ session = $session; scenario = "S9"; run = $RunId; variant = $variant;
                ts = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json -Compress) + "`n"
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    & ssh -o BatchMode=yes -i $IdentityFile $Server "echo $b64 | base64 -d >> /opt/maas-lab/logs/scenarios.jsonl"
}

$results = @()
foreach ($variant in $Variants) {
    Invoke-Git reset -q --hard lab
    Invoke-Git clean -qfd
    $session = $null
    Push-Location $Repo
    try {
        for ($i = 0; $i -lt $prompts.Count; $i++) {
            $a = @("-Server", $Server, "-IdentityFile", $IdentityFile)
            if ($variant -eq "claude") { $a += "-ClaudeSonnet" }
            $a += @("-p", $prompts[$i], "--output-format", "json", "--permission-mode", "acceptEdits")
            if ($session) { $a += @("--resume", $session) }
            $a += @("--allowedTools", $tools)
            $t0 = Get-Date
            $ErrorActionPreference = "Continue"
            # A child powershell (as tested): fresh env per turn, -p etc. reach claude untouched.
            $out = & powershell -NoProfile -File $launcher @a 2>$null
            $ErrorActionPreference = "Stop"
            $line = @($out | Where-Object { $_ -match '^\s*\{' })[-1]
            if (-not $line) { throw "s9: claude returned no JSON in turn $($i + 1) ($variant)" }
            $res = $line | ConvertFrom-Json
            if (-not $session) { $session = $res.session_id; Add-Label $session $variant }
            $secs = [int]((Get-Date) - $t0).TotalSeconds
            Write-Host ("s9 {0} turn {1}/{2} done in {3}s{4} session {5}" -f $variant, ($i + 1), $prompts.Count, $secs,
                        $(if ($res.is_error) { " (error)" } else { "" }), $session)
        }
    } finally { Pop-Location }
    $results += [pscustomobject]@{ variant = $variant; session = $session }
}
Invoke-Git reset -q --hard lab
Invoke-Git clean -qfd
$results | Format-Table -AutoSize | Out-String | Write-Host
Write-Host "s9: run $RunId done. On the server: make analyze  (report.md -> By model, By session)"
