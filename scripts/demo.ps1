<#
.SYNOPSIS
  One entry point for the local demo. Both modes replay demo_data/demo_1hz.csv.

.EXAMPLE
  ./scripts/demo.ps1 up        # mode 1: edge AI in Docker (simulator + TabM gateway), print URLs
  ./scripts/demo.ps1 esp32     # mode 2: edge AI on the ESP32-S3 in Wokwi (no simulator)
  ./scripts/demo.ps1 ui        # rebuild only the Kitchen UI after editing static files
  ./scripts/demo.ps1 status    # health + CPU/RAM of every container
  ./scripts/demo.ps1 logs      # follow the application logs
  ./scripts/demo.ps1 check     # tests, lint, dashboards in sync, compose config
  ./scripts/demo.ps1 down      # stop (data volumes are kept)
#>
param(
  [ValidateSet("up", "esp32", "ui", "status", "logs", "check", "dashboards", "down", "clean-images")]
  [string]$Command = "up"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
# Both profiles, for commands that look at or stop everything whatever the mode.
$AllProfiles = @("--profile", "demo", "--profile", "esp32")

function Show-Urls {
  Write-Host ""
  Write-Host "Bếp công nghiệp (UI)   http://localhost:8080" -ForegroundColor Green
  Write-Host "Grafana · Pipeline      http://localhost:3000/d/aiot-pipeline-operations"
  Write-Host "Grafana · Khí gas       http://localhost:3000/d/aiot-gas-monitoring"
  Write-Host "Prometheus alerts       http://localhost:9090/alerts"
}

# `up --wait` returns once every container is healthy; this checks that data really flows.
function Wait-FirstPrediction([string]$NodeId, [int]$Seconds) {
  $started = Get-Date
  while (((Get-Date) - $started).TotalSeconds -lt $Seconds) {
    try {
      $state = Invoke-RestMethod -TimeoutSec 3 http://localhost:8080/api/state
      $prediction = $state.latest.prediction
      if ($prediction -and $prediction.node_id -eq $NodeId) {
        $after = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
        Write-Host ("Dự đoán đầu tiên của {0}: {1} · CO {2:N1} ppm ({3}/{4}) sau {5} s" -f `
            $NodeId, $prediction.alarm_level.ToUpper(), $prediction.co_ppm, `
            $prediction.model_name, $prediction.model_version, $after) -ForegroundColor Green
        return $true
      }
    } catch { }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function Start-Stack([string]$Mode, [string[]]$Extra = @()) {
  $started = Get-Date
  docker compose --profile $Mode up -d --build --wait --wait-timeout 300 @Extra
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose up thất bại; xem: ./scripts/demo.ps1 status"
  }
  $took = [math]::Round(((Get-Date) - $started).TotalSeconds)
  Write-Host "Mọi service đã healthy sau $took s." -ForegroundColor Green
}

switch ($Command) {
  "up" {
    # The screen follows whichever node streams (here the simulator's virtual-gas-01).
    Remove-Item Env:DEMO_NODE_ID -ErrorAction SilentlyContinue
    Start-Stack "demo"
    if (-not (Wait-FirstPrediction "virtual-gas-01" 30)) {
      Write-Warning "Chưa thấy dự đoán; xem: docker compose logs simulator edge-inference"
    }
    Show-Urls
  }
  "esp32" {
    # The ESP32 replays the same CSV as its own node: stop the simulated one so the screen and
    # Grafana show the board only.
    docker compose rm --stop --force simulator | Out-Null
    # Pin the screen to the board: until Wokwi connects it says it waits for esp32-edge-01.
    $env:DEMO_NODE_ID = "esp32-edge-01"
    Start-Stack "esp32"
    $firmware = "wokwi/esp32-replay/esp32-replay"
    if (-not (Test-Path "$firmware/.pio/build/esp32s3/firmware_merged.bin")) {
      Write-Warning "Chưa có firmware: mở $firmware trong VS Code và bấm PlatformIO: Build (esp32s3)."
    }
    Show-Urls
    Write-Host ""
    Write-Host "Tiếp theo: VS Code → mở $firmware/diagram.json → Wokwi: Start Simulator." -ForegroundColor Yellow
    Write-Host "Board (node esp32-edge-01) nối Mosquitto qua host.wokwi.internal:1883;" -ForegroundColor Yellow
    Write-Host "màn hình :8080 tự chuyển sang board khi gói tin đầu tiên tới." -ForegroundColor Yellow
  }
  "ui" {
    docker compose $AllProfiles up -d --build --wait demo-ui
    Write-Host "Nhấn Ctrl+F5 trên trình duyệt để tải lại JS/CSS." -ForegroundColor Yellow
  }
  "status" {
    docker compose $AllProfiles ps --format "table {{.Service}}\t{{.Status}}"
    Write-Host ""
    $names = docker compose $AllProfiles ps -q
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" $names
  }
  "logs" {
    docker compose $AllProfiles logs -f --tail 50 simulator edge-inference mqtt-kafka-bridge safety-controller influx-sink demo-ui
  }
  "check" {
    uv run pytest
    uv run ruff check .
    uv run ruff format --check .
    docker compose --profile demo config --quiet
    docker compose --profile esp32 config --quiet
    Write-Host "Mọi kiểm tra đều đạt." -ForegroundColor Green
  }
  "dashboards" {
    uv run python infra/grafana/build_dashboards.py
    Write-Host "Grafana tự nạp lại dashboard sau tối đa 30 giây."
  }
  "down" {
    docker compose $AllProfiles down
  }
  "clean-images" {
    # Images built before all Python services shared aiot-gas-pipeline:local, plus build cache.
    docker image ls --format "{{.Repository}}" | Where-Object { $_ -like "aiot-gas-monitoring-*" } |
      ForEach-Object { docker image rm $_ }
    docker builder prune -f
  }
}
