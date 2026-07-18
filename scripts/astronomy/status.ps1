# Quick health for Astronomy + AIOps dual stack
Write-Host "=== AIOps ===" -ForegroundColor Cyan
@(
  "http://localhost:3000/api/health",
  "http://localhost:8001/health",
  "http://localhost:8002/health",
  "http://localhost:8003/health"
) | ForEach-Object {
  try {
    $r = Invoke-WebRequest $_ -UseBasicParsing -TimeoutSec 3
    Write-Host "OK  $_  $($r.StatusCode)"
  } catch {
    Write-Host "ERR $_"
  }
}
Write-Host "=== Astronomy ===" -ForegroundColor Cyan
@("http://localhost:8080", "http://localhost:4000") | ForEach-Object {
  try {
    $r = Invoke-WebRequest $_ -UseBasicParsing -TimeoutSec 3
    Write-Host "OK  $_  $($r.StatusCode)"
  } catch {
    Write-Host "ERR $_"
  }
}
Write-Host "=== Prom sample (service labels) ===" -ForegroundColor Cyan
try {
  $q = [uri]::EscapeDataString('count by (service_name) ({__name__=~".+"})')
  $u = "http://localhost:9090/api/v1/query?query=$q"
  (Invoke-RestMethod $u).data.result | Select-Object -First 15 | ForEach-Object {
    Write-Host ($_.metric.service_name)
  }
} catch {
  Write-Host "Prometheus query failed: $_"
}
