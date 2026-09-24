"""Jalankan API dari folder mana pun (dipakai launcher & untuk pengembangan).

    python scripts/api_dev.py          # hanya komputer ini (127.0.0.1)
    python scripts/api_dev.py --lan    # juga perangkat lain di WiFi yang sama
    python scripts/api_dev.py --reload # muat ulang otomatis saat kode berubah

Mode --lan mendengarkan di semua antarmuka, tapi api.auth.Penjaga tetap menolak klien
di luar jaringan lokal dan mewajibkan login.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0" if "--lan" in sys.argv else "127.0.0.1", port=8000,
                reload="--reload" in sys.argv, reload_dirs=["api", "pipeline"])
