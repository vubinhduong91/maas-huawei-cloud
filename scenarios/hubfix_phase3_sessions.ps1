<#
.SYNOPSIS
  Hub-fix lab Phase 3: real Claude Code sessions (scenarios\hubfix_win_session.ps1) for P0, P3, P4,
  P5 at two effort levels that need mapping, -Runs times, gateway 4 workers.

.DESCRIPTION
  Order: for each run, for each level, P0 -> P3 -> P4 -> P5 (round robin, so the configs share the
  MaaS conditions of the same minutes; MaaS keeps a cache per instance without session affinity).
    level "default": Sonnet with Claude Code's default effort (medium -> high at the gateway);
                     P0 = the glm client at its default (high).
    level "xhigh":   Sonnet --effort xhigh (-> max); P0 = the glm client --effort max.
  The gateway is switched over SSH (render-check first, so a config that does not render never
  takes APISIX down) and set back to P0 (workers auto) at the end. Labels: p3-<cfg>-<level>-r<run>.
  Summary on the server: python3 scenarios/hubfix_sessions.py --prefix p3- --out reports/hubfix-phase3-sessions.md

.EXAMPLE
  .\scenarios\hubfix_phase3_sessions.ps1
  .\scenarios\hubfix_phase3_sessions.ps1 -Runs 1 -Levels xhigh -Configs P4
#>
param(
    [int] $Runs = 2,
    [string[]] $Levels = @("default", "xhigh"),
    [string[]] $Configs = @("P0", "P3", "P4", "P5"),
    [string] $Server = "root@111.119.197.251",
    [string] $IdentityFile = "$env:USERPROFILE\.ssh\maas_ecs",
    [int] $FirstRun = 1,
    [string] $Prefix = "p3-",   # label prefix (the P4-vs-P0 test uses pc-)
    [string] $Prompts = "",     # passed on to hubfix_win_session.ps1
    [switch] $NoGit
)
$ErrorActionPreference = "Stop"
$session = Join-Path $PSScriptRoot "hubfix_win_session.ps1"

function Set-Gateway([string] $gw, [string] $workers) {
    $envs = if ($gw -eq "default") { "" } else { "LAB_GATEWAY_PROFILE=$gw APISIX_WORKER_PROCESSES=$workers" }
    $cmd = "cd /opt/maas-lab && export $envs; " +
           "make -s render-check >/dev/null 2>&1 || { echo RENDER-FAILED; exit 6; }; " +
           "make -s up MODE=B >/dev/null 2>&1; make -s keyorder-up >/dev/null 2>&1; sleep 4; " +
           "curl -s -o /dev/null -w 'apisix %{http_code}' -X POST localhost:9080/v1/messages -d '{}'"
    if ($gw -eq "default") { $cmd = $cmd -replace "export ; ", "" }
    $out = & ssh -o BatchMode=yes -i $IdentityFile $Server $cmd
    if ($LASTEXITCODE -ne 0 -or $out -notmatch "apisix 401") { throw "gateway $gw w$workers did not come up: $out" }
    Write-Host "gateway: $gw w$workers ($out)"
}

try {
    for ($r = $FirstRun; $r -lt $FirstRun + $Runs; $r++) {
        foreach ($level in $Levels) {
            foreach ($cfg in $Configs) {
                Set-Gateway $cfg "4"
                $client = if ($cfg -eq "P0") { "glm" } else { "sonnet" }
                $effort = ""
                if ($level -eq "xhigh") { $effort = if ($client -eq "glm") { "max" } else { "xhigh" } }
                & $session -Label "$Prefix$cfg-$level-r$r" -Client $client -Gateway $cfg -Workers 4 -Effort $effort `
                           -Prompts $Prompts -NoGit:$NoGit
            }
        }
    }
} finally {
    Set-Gateway "default" "auto"
}
