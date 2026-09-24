# Daftarkan run harian ke Windows Task Scheduler (padanan cron di Windows).
#   powershell -ExecutionPolicy Bypass -File scripts\jadwalkan.ps1            # jam dari config/settings.yaml
#   powershell -ExecutionPolicy Bypass -File scripts\jadwalkan.ps1 -Jam 06:30
#   powershell -ExecutionPolicy Bypass -File scripts\jadwalkan.ps1 -Hapus
# Setelah terdaftar, mengubah "Jadwal harian" di website ikut mengubah task ini.
param(
    [string]$Jam = "",
    [switch]$Hapus
)

$Nama = "MagangFinder"
$Root = Split-Path $PSScriptRoot -Parent

if ($Hapus) {
    Unregister-ScheduledTask -TaskName $Nama -Confirm:$false
    "Jadwal $Nama dihapus."
    return
}

if (-not $Jam) {
    $baris = Select-String -Path (Join-Path $Root "config\settings.yaml") -Pattern '^jadwal:\s*"?(\d{2}:\d{2})'
    $Jam = if ($baris) { $baris.Matches[0].Groups[1].Value } else { "07:00" }
}

# pythonw.exe: berjalan tanpa jendela konsol. Log ada di logs\run-*.log,
# progres langsung terlihat di dashboard website.
$aksi = New-ScheduledTaskAction -Execute (Join-Path $Root ".venv\Scripts\pythonw.exe") `
        -Argument "mf.py run" -WorkingDirectory $Root
$pemicu = New-ScheduledTaskTrigger -Daily -At $Jam
# StartWhenAvailable: kalau PC mati di jam itu, run dikejar begitu PC menyala.
$opsi = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Nama -Action $aksi -Trigger $pemicu -Settings $opsi `
    -Description "Magang Finder: cari, saring, nilai, dan kirim lowongan magang" -Force | Out-Null
"Terjadwal: $Nama tiap hari jam $Jam (tanpa jendela)"
