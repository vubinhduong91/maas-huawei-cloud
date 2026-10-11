<#
.SYNOPSIS
  Run Claude Code on Windows through the lab APISIX on a remote server (via an SSH tunnel).

.DESCRIPTION
  Claude Code (this PC) -> ssh -L 9080 -> APISIX on the server -> (tap, mode B) -> Huawei MaaS.
  - Opens the tunnel in the background if localhost:9080 is not answering yet.
  - Sets the ANTHROPIC_* variables for this process only: your normal Claude Code setup
    (Anthropic login, settings) is untouched.
  - Uses a separate config dir (%USERPROFILE%\.claude-maas) so the MaaS session does not
    mix login, history or memory with your normal Claude Code.
  The developer key is read from %USERPROFILE%\.claude-maas\dev-key.txt (one line; it must
  match an entry of DEV_KEYS in the server's .env). It is never printed.

.EXAMPLE
  .\claude-maas.ps1 -Server root@1.2.3.4 -IdentityFile C:\keys\ecs.pem
  .\claude-maas.ps1 -Server root@1.2.3.4 -IdentityFile C:\keys\ecs.pem -p "explain this repo"
#>
# Plain (non-advanced) script on purpose: undeclared arguments such as -p land in $args and
# are passed to claude unchanged; an advanced script would bind -p to -PipelineVariable.
param(
    [string] $Server,
    [string] $IdentityFile = "",
    [int] $LocalPort = 19080,   # dedicated port: never mistake another local service for the tunnel
    [string] $Model = "glm-5.3",
    [string] $FastModel = "glm-5.2",
    [switch] $NoBackgroundTraffic
)
$ErrorActionPreference = "Stop"
if (-not $Server) { throw "Usage: claude-maas.ps1 -Server root@<ip> [-IdentityFile key] [claude args...]" }
$ClaudeArgs = $args

$configDir = Join-Path $env:USERPROFILE ".claude-maas"
$keyFile = Join-Path $configDir "dev-key.txt"
if (-not (Test-Path $keyFile)) {
    New-Item -ItemType Directory -Force $configDir | Out-Null
    throw "Missing $keyFile - put your developer key (from DEV_KEYS on the server) in it, one line."
}
$devKey = (Get-Content $keyFile -TotalCount 1).Trim()

function Test-Port([int] $port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect("127.0.0.1", $port); return $true } catch { return $false } finally { $client.Close() }
}

if (-not (Test-Port $LocalPort)) {
    $sshArgs = @("-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                 "-L", "${LocalPort}:127.0.0.1:9080")
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

Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$env:CLAUDE_CONFIG_DIR = $configDir
$env:ANTHROPIC_BASE_URL = "http://localhost:$LocalPort"
$env:ANTHROPIC_AUTH_TOKEN = $devKey
$env:ANTHROPIC_MODEL = $Model
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $Model
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $Model
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $FastModel
if ($NoBackgroundTraffic) { $env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1" }
else { Remove-Item Env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC -ErrorAction SilentlyContinue }

Write-Host "claude-maas: base=$($env:ANTHROPIC_BASE_URL) model=$Model fast=$FastModel config=$configDir"
& claude @ClaudeArgs
