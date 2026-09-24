"""Snapshot publik untuk situs showcase (baca-saja, di Vercel).

Situs showcase tidak pernah terhubung ke komputer ini. Setelah ada perubahan (run
selesai, Deep Search selesai), data yang boleh publik diekspor ke satu file JSON dan
di-push ke branch `showcase-data` di GitHub; situs membacanya dari sana.

Yang IKUT: lowongan + skor + alasan penilaian, profil singkat perusahaan, laporan
Deep Search, statistik sumber, dan angka ringkas tiap run.
Yang TIDAK ikut: catatan pribadi, status lamaran, riwayat chat, profil & skill,
pengaturan, pesan error run, dan log.

Bentuk tiap bagian sama dengan respons endpoint API, jadi frontend memakai tipe yang sama.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from pipeline import config, db

VERSI = 1


def bangun() -> dict:
    from pipeline import riset

    from . import main

    jobs = main.list_jobs()
    for j in jobs:
        j["notes"] = ""
        j["status"] = "baru"
        j["mustHave"] = []            # label must-have berasal dari profil pribadi

    laporan: dict[str, dict] = {}
    with db.koneksi() as con:
        per_kunci = {r["kunci"]: r for r in con.execute(
            "SELECT * FROM riset_perusahaan WHERE status = 'selesai' AND laporan IS NOT NULL")}
    for j in jobs:
        r = per_kunci.get(riset.kunci_perusahaan(j["company"]))
        if r:
            o = main._riset_out(r)
            o.pop("error", None)
            laporan[j["id"]] = o

    runs = [{k: v for k, v in r.items() if k != "error"} for r in main._runs()]
    ringkas = main.summary()
    ringkas["lastRun"].pop("error", None)
    ringkas.pop("nextRunAt", None)                 # jadwal pribadi; showcase tidak menjadwalkan apa pun

    sumber = []
    for s in main.list_sources():
        sumber.append({k: v for k, v in s.items() if k not in ("keywords", "targetedQuery")}
                      | {"keywords": "", "targetedQuery": ""})

    return {
        "versi": VERSI,
        "dibuat": datetime.now().astimezone().isoformat(timespec="seconds"),
        "threshold": main.get_settings()["threshold"],
        "jobs": jobs,
        "companies": main.list_companies(),
        "research": laporan,
        "sources": sumber,
        "runs": runs,
        "summary": ringkas,
    }


def _git(folder: Path, *argumen: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(folder), *argumen], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def sinkron(pesan: str = "perbarui snapshot") -> str:
    """Tulis snapshot ke folder repo data lalu push. Mengembalikan ringkasan untuk log.

    Tidak pernah melempar exception ke pemanggil: gagal sinkron tidak boleh
    menggagalkan run yang sudah selesai.
    """
    cfg = config.settings().get("showcase") or {}
    if not cfg.get("aktif"):
        return "showcase nonaktif"
    folder = Path(cfg["repo_data"])
    if not (folder / ".git").exists():
        return f"showcase: {folder} belum berupa repo git"
    try:
        isi = bangun()
    except Exception as e:  # noqa: BLE001
        return f"showcase: gagal membangun snapshot ({e})"

    berkas = folder / "snapshot.json"
    lama = json.loads(berkas.read_text(encoding="utf-8")) if berkas.exists() else {}
    berubah = ({k: v for k, v in lama.items() if k != "dibuat"}
               != {k: v for k, v in isi.items() if k != "dibuat"})
    if berubah:
        berkas.write_text(json.dumps(isi, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        _git(folder, "add", "snapshot.json")
        c = _git(folder, "commit", "-m", pesan)
        if c.returncode != 0:
            return f"showcase: commit gagal ({(c.stderr or c.stdout).strip()[:200]})"
    # Push juga saat tidak berubah: commit dari sinkron sebelumnya mungkin gagal di-push.
    p = _git(folder, "push", "origin", cfg.get("branch", "showcase-data"))
    if p.returncode != 0:
        return f"showcase: push gagal ({p.stderr.strip()[:200]}) — akan dicoba lagi di sinkron berikutnya"
    if not berubah:
        return "showcase: tidak ada perubahan"
    return f"showcase: {len(isi['jobs'])} lowongan, {len(isi['research'])} laporan riset dipublikasikan"
