"""Adapter sumber lowongan.

Tiap adapter punya satu fungsi `daftar(cfg, stat, interaktif)` yang membaca
halaman daftar / API dan mengembalikan `(url, judul, muat)` per lowongan. `muat()`
baru mengambil halaman detail — dipanggil orkestrator SETELAH URL yang sudah
dikenal dibuang, supaya halaman lama tidak pernah diambil ulang.
"""

from __future__ import annotations

from collections.abc import Callable

from . import api, connector

ADAPTER = {
    "connector": connector.daftar,
    "jooble": api.jooble,
    "adzuna": api.adzuna,
    "careerjet": api.careerjet,
    "dealls": api.dealls,
    "kalibrr": api.kalibrr,
    "techinasia": api.techinasia,
}


def daftar(cfg: dict, stat: dict, interaktif: bool = False) -> list[tuple[str, str, Callable]]:
    return ADAPTER[cfg["jenis"]](cfg, stat, interaktif=interaktif)
