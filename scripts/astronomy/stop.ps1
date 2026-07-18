# Stop Astronomy Shop demo containers (keeps AIOps stack running).
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$DemoDir = Join-Path $Root "third_party\opentelemetry-demo"
$BridgeCompose = Join-Path $Root "integrations\astronomy-shop\compose.aiops-bridge.yaml"

if (-not (Test-Path (Join-Path $DemoDir "compose.yaml"))) {
  Write-Host "Demo dir not found; nothing to stop."
  exit 0
}

Push-Location $DemoDir
docker compose --env-file .env --env-file .env.override `
  -f compose.yaml -f compose.extras.yaml `
  -f $BridgeCompose `
  down --remove-orphans
Pop-Location
Write-Host "Astronomy Shop stopped. AIOps stack left running."
