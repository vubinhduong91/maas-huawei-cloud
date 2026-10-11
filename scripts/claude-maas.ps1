<#
.SYNOPSIS
  Run Claude Code on Windows through the lab APISIX on a remote server (via an SSH tunnel).

.DESCRIPTION
  Claude Code (this PC) -> ssh -L 19080 -> APISIX on the server -> ...

  Default (subscription) mode - the server needs ANTHROPIC_PASSTHROUGH=1:
    - You stay logged in with your Claude subscription (your normal Claude Code config).
    - The sonnet tier is mapped to glm-5.3 -> APISIX -> tap -> Huawei MaaS (1M context).
    - opus / haiku keep Claude Code's defaults -> APISIX -> api.anthropic.com with your
      subscription token.
    - APISIX identifies you by the X-Lab-Key header (ANTHROPIC_CUSTOM_HEADERS).
  -ClaudeSonnet: same, but the sonnet tier stays Claude Sonnet (comparison runs, S9).
  -MaasOnly mode:
    - Every tier is glm-5.3 via MaaS; authenticates with the dev key as ANTHROPIC_AUTH_TOKEN
      and uses a separate config dir (%USERPROFILE%\.claude-maas), no Anthropic login.

  Only this process gets the ANTHROPIC_* variables. The developer key is read from
  %USERPROFILE%\.claude-maas\dev-key.txt (one line, an entry of DEV_KEYS on the server)
  and never printed.

.EXAMPLE
  .\claude-maas.ps1 -Server root@1.2.3.4 -IdentityFile C:\keys\ecs.pem
  .\claude-maas.ps1 -Server root@1.2.3.4 -IdentityFile C:\keys\ecs.pem -p "explain this repo"
  .\claude-maas.ps1 -Server root@1.2.3.4 -IdentityFile C:\keys\ecs.pem -MaasOnly
#>
# Plain (non-advanced) script on purpose: undeclared arguments such as -p land in $args and
# are passed to claude unchanged; an advanced script would bind -p to -PipelineVariable.
param(
    [string] $Server,
    [string] $IdentityFile = "",
    [int] $LocalPort = 19080,   # dedicated port: never mistake another local service for the tunnel
    [string] $GlmModel = "glm-5.3",   # not "-Model": PowerShell would bind claude's --model to it
    [switch] $MaasOnly,
    [switch] $ClaudeSonnet,     # S9: keep the real Claude Sonnet for the sonnet tier (no GLM)
    [switch] $NoBackgroundTraffic
)
$ErrorActionPreference = "Stop"
if (-not $Server) { throw "Usage: claude-maas.ps1 -Server root@<ip> [-IdentityFile key] [-MaasOnly] [claude args...]" }
$ClaudeArgs = $args

$maasDir = Join-Path $env:USERPROFILE ".claude-maas"
$keyFile = Join-Path $maasDir "dev-key.txt"
if (-not (Test-Path $keyFile)) {
    New-Item -ItemType Directory -Force $maasDir | Out-Null
    throw "Missing $keyFile - put your developer key (from DEV_KEYS on the server) in it, one line."
}
$devKey = (Get-Content $keyFile -TotalCount 1).Trim()

function Test-Port([int] $port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect("127.0.0.1", $port); return $true } catch { return $false } finally { $client.Close() }
}

# Reuse a running tunnel only if it is ours and points at this -Server; otherwise replace it.
$forward = "${LocalPort}:127.0.0.1:9080"
$tunnels = @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -like "*$forward*" })
foreach ($t in $tunnels | Where-Object { $_.CommandLine -notlike "*$Server*" }) {
    Write-Host "claude-maas: closing tunnel to another server (pid $($t.ProcessId))"
    Stop-Process -Id $t.ProcessId -Force
    Start-Sleep -Milliseconds 500
}
if (Test-Port $LocalPort) {
    $ours = @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -like "*$forward*" })
    if ($ours.Count -eq 0) { throw "localhost:$LocalPort is used by another program; pass -LocalPort <free port>." }
}
if (-not (Test-Port $LocalPort)) {
    $sshArgs = @("-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                 "-o", "StrictHostKeyChecking=accept-new", "-L", $forward)
    if ($IdentityFile) { $sshArgs += @("-i", $IdentityFile) }
    $sshArgs += $Server
    Write-Host "claude-maas: opening SSH tunnel localhost:$LocalPort -> ${Server}:9080"
    Start-Process -FilePath "ssh" -ArgumentList $sshArgs -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(20)
    while (-not (Test-Port $LocalPort)) {
        if ((Get-Date) -gt $deadline) { throw "SSH tunnel did not come up (check -Server / -IdentityFile)." }
        Start-Sleep -Milliseconds 500
    }
}

foreach ($name in "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_CUSTOM_HEADERS",
                  "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CONFIG_DIR") {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
$env:ANTHROPIC_BASE_URL = "http://localhost:$LocalPort"
# "[1m]" tells Claude Code this model has a 1M window; it is stripped before sending.
$glm = "$GlmModel[1m]"
if ($MaasOnly) {
    $env:CLAUDE_CONFIG_DIR = $maasDir
    $env:ANTHROPIC_AUTH_TOKEN = $devKey
    $env:ANTHROPIC_MODEL = $glm
    $env:ANTHROPIC_DEFAULT_OPUS_MODEL = $glm
    $env:ANTHROPIC_DEFAULT_SONNET_MODEL = $glm
    $env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $glm
    $summary = "all tiers=$GlmModel (MaaS), config=$maasDir"
} else {
    $env:ANTHROPIC_CUSTOM_HEADERS = "X-Lab-Key: $devKey"
    $env:ANTHROPIC_MODEL = "sonnet"
    if ($ClaudeSonnet) {
        $summary = "every tier=Claude subscription (via the gateway), config=default"
    } else {
        $env:ANTHROPIC_DEFAULT_SONNET_MODEL = $glm
        $summary = "sonnet=$GlmModel (MaaS), opus/haiku=Claude subscription, config=default"
    }
}
if ($NoBackgroundTraffic) { $env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1" }
else { Remove-Item Env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC -ErrorAction SilentlyContinue }

Write-Host "claude-maas: base=$($env:ANTHROPIC_BASE_URL) $summary"
# claude writes notices to stderr; with "Stop", PowerShell 5.1 would turn them into errors.
$ErrorActionPreference = "Continue"
& claude @ClaudeArgs
exit $LASTEXITCODE
