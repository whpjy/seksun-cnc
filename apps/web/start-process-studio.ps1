param(
  [string]$HarnessTarget = 'http://127.0.0.1:3081',
  [string]$CncTarget = 'http://127.0.0.1:3001',
  [string]$HarnessContainer = 'seksun-cnc-harness-1'
)
$ErrorActionPreference = 'Stop'
$env:PROCESS_HARNESS_TARGET = $HarnessTarget
$env:PROCESS_CNC_TARGET = $CncTarget
if (-not $env:PROCESS_HARNESS_TOKEN) {
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $launchLogs = docker logs $HarnessContainer 2>&1 | Out-String
    $dockerExitCode = $LASTEXITCODE
  } finally { $ErrorActionPreference = $previousPreference }
  if ($dockerExitCode -ne 0) { throw 'Cannot read Harness container logs. Set PROCESS_HARNESS_TOKEN or check the container name.' }
  $tokens = [regex]::Matches($launchLogs, 'http://[^\s]+[?&]token=([a-zA-Z0-9_-]+)')
  if ($tokens.Count -eq 0) { throw 'No current Harness launch token found. Set PROCESS_HARNESS_TOKEN before starting.' }
  $env:PROCESS_HARNESS_TOKEN = $tokens[$tokens.Count - 1].Groups[1].Value
}
Set-Location -LiteralPath $PSScriptRoot
node node_modules/vite/bin/vite.js --config vite.process.config.ts
