<#
.SYNOPSIS
  Nyalakan / matikan panel kontrol Magang Finder sesuai kebutuhan (tidak otomatis saat Windows menyala).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\panel.ps1 mulai      # API + web di latar, buka browser
  powershell -ExecutionPolicy Bypass -File scripts\panel.ps1 berhenti   # matikan keduanya
  powershell -ExecutionPolicy Bypass -File scripts\panel.ps1 status

.NOTES
  Run pipeline yang sedang berjalan TIDAK ikut dimatikan oleh "berhenti" — run adalah proses
  terpisah; hentikan lewat tombol "Hentikan" di dashboard.
  Web dibangun ulang (npm run build) hanya kalau kodenya berubah sejak build terakhir.
#>
param([ValidateSet("mulai", "berhenti", "status")][string]$Aksi = "mulai")

$ErrorActionPreference = "Stop"
$Mesin = Split-Path -Parent $PSScriptRoot
$Web = "C:\Projects\magang-finder-web\magang-finder-web"
$Log = Join-Path $Mesin "logs"
New-Item -ItemType Directory -Force $Log | Out-Null

function Pid-Port([int]$Port) {
  $k = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($k) { $k.OwningProcess } else { $null }
}

function Tunggu-Port([int]$Port, [int]$Detik) {
  for ($i = 0; $i -lt $Detik; $i++) {
    if (Pid-Port $Port) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

function Ip-Lan {
  # Adaptor yang punya gateway = yang tersambung ke router (WiFi/LAN), bukan adaptor
  # virtual Hyper-V/WSL (172.x) yang juga ber-IP privat.
  (Get-NetIPConfiguration -ErrorAction SilentlyContinue |
    Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
    Select-Object -First 1).IPv4Address.IPAddress
}

function Perlu-Build {
  $id = Join-Path $Web ".next\BUILD_ID"
  if (-not (Test-Path $id)) { return $true }
  $waktu = (Get-Item $id).LastWriteTime
  $sumber = @("app", "components", "lib", "hooks") | ForEach-Object { Get-ChildItem (Join-Path $Web $_) -Recurse -File }
  $sumber += @("next.config.ts", ".env.local", "package.json") | ForEach-Object { Get-Item (Join-Path $Web $_) -ErrorAction SilentlyContinue }
  return [bool]($sumber | Where-Object { $_.LastWriteTime -gt $waktu } | Select-Object -First 1)
}

switch ($Aksi) {
  "mulai" {
    if (-not (Pid-Port 8000)) {
      Write-Host "Menyalakan API (port 8000)..."
      Start-Process -FilePath (Join-Path $Mesin ".venv\Scripts\python.exe") `
        -ArgumentList "scripts\api_dev.py", "--lan" -WorkingDirectory $Mesin -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Log "panel-api.log") -RedirectStandardError (Join-Path $Log "panel-api.err.log") | Out-Null
    } else { Write-Host "API sudah menyala." }

    if (-not (Pid-Port 3000)) {
      if (Perlu-Build) {
        Write-Host "Kode web berubah sejak build terakhir - membangun ulang (1-2 menit)..."
        Push-Location $Web
        try { & npm run build *> (Join-Path $Log "panel-build.log") } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw "Build web gagal - lihat logs\panel-build.log" }
      }
      Write-Host "Menyalakan panel web (port 3000)..."
      Start-Process -FilePath "node" -ArgumentList "node_modules\next\dist\bin\next", "start", "-H", "0.0.0.0", "-p", "3000" `
        -WorkingDirectory $Web -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Log "panel-web.log") -RedirectStandardError (Join-Path $Log "panel-web.err.log") | Out-Null
    } else { Write-Host "Panel web sudah menyala." }

    if ((Tunggu-Port 8000 30) -and (Tunggu-Port 3000 60)) {
      $ip = Ip-Lan
      Write-Host ""
      Write-Host "Panel menyala."
      Write-Host "  Komputer ini : http://localhost:3000"
      if ($ip) { Write-Host "  HP (WiFi sama): http://${ip}:3000" }
      Start-Process "http://localhost:3000"
    } else {
      Write-Host "Panel belum menjawab - lihat logs\panel-api.err.log dan logs\panel-web.err.log"
      exit 1
    }
  }
  "berhenti" {
    foreach ($port in 3000, 8000) {
      $p = Pid-Port $port
      if ($p) {
        # /T: ikut matikan proses anak (mis. worker Next.js).
        taskkill /F /T /PID $p | Out-Null
        Write-Host "Port $port dimatikan."
      } else { Write-Host "Port $port memang tidak menyala." }
    }
    Write-Host "Panel mati. Situs showcase di Vercel tetap online."
  }
  "status" {
    foreach ($x in @(@{P = 8000; N = "API"}, @{P = 3000; N = "Panel web"})) {
      Write-Host ("{0,-10} {1}" -f $x.N, $(if (Pid-Port $x.P) { "MENYALA" } else { "mati" }))
    }
  }
}
