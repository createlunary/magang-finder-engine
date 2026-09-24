"""Sinkronkan jam run harian di Windows Task Scheduler dengan settings.yaml.

Hanya MENGUBAH task yang sudah ada. Mendaftarkan task baru adalah keputusan
pengguna, lewat scripts/jadwalkan.ps1.
"""

from __future__ import annotations

import os
import subprocess

NAMA_TASK = "MagangFinder"


def ada() -> bool:
    if os.name != "nt":
        return False
    return subprocess.run(["schtasks", "/Query", "/TN", NAMA_TASK],
                          capture_output=True).returncode == 0


def perbarui(jam: str) -> bool:
    """True kalau task terdaftar dan jamnya berhasil diubah."""
    if not ada():
        return False
    return subprocess.run(["schtasks", "/Change", "/TN", NAMA_TASK, "/ST", jam],
                          capture_output=True).returncode == 0
