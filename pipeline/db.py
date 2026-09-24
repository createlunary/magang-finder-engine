"""SQLite: histori lowongan, penilaian, dan log run."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config
from .models import Ekstraksi, LowonganMentah, Penilaian

SKEMA = """
CREATE TABLE IF NOT EXISTS lowongan (
    id              INTEGER PRIMARY KEY,
    sumber          TEXT NOT NULL,
    url             TEXT NOT NULL,
    url_kanonik     TEXT NOT NULL UNIQUE,
    sidik_jari      TEXT,
    judul           TEXT, perusahaan TEXT, lokasi TEXT,
    tipe_kerja      TEXT, jenis TEXT, durasi_bulan INTEGER, kompensasi TEXT, gaji TEXT,
    deadline        TEXT, bidang TEXT, requirement TEXT, deskripsi TEXT,
    relevan         INTEGER,            -- hasil filter kasar LLM lokal
    alasan_filter   TEXT,
    duplikat_dari   INTEGER REFERENCES lowongan(id),
    teks_mentah     TEXT,
    diposting_pada  TEXT,
    ditemukan_pada  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'baru'   -- baru|dikirim|dilihat|dilamar|ditolak|diterima|arsip
);
CREATE INDEX IF NOT EXISTS ix_lowongan_sidik ON lowongan(sidik_jari);

CREATE TABLE IF NOT EXISTS penilaian (
    id              INTEGER PRIMARY KEY,
    lowongan_id     INTEGER NOT NULL REFERENCES lowongan(id),
    model           TEXT NOT NULL,
    profil_hash     TEXT NOT NULL,
    skor            INTEGER NOT NULL,
    kecocokan       TEXT, alasan TEXT, must_have TEXT, red_flags TEXT,
    lolos           INTEGER NOT NULL,
    dinilai_pada    TEXT NOT NULL,
    UNIQUE (lowongan_id, profil_hash)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    mulai       TEXT NOT NULL,
    selesai     TEXT,
    statistik   TEXT                -- JSON: per sumber, jumlah tiap tahap, token, error
);

-- Deep Search: satu baris per perusahaan (dinormalkan), dipakai semua lowongannya.
CREATE TABLE IF NOT EXISTS riset_perusahaan (
    kunci           TEXT PRIMARY KEY,          -- nama tanpa PT/CV, huruf kecil
    nama            TEXT NOT NULL,
    status          TEXT NOT NULL,             -- berjalan | selesai | gagal
    laporan         TEXT,                      -- JSON LaporanPerusahaan
    statistik       TEXT,                      -- token, biaya setara, putaran
    error           TEXT,
    pid             INTEGER,
    dari_lowongan   INTEGER REFERENCES lowongan(id),
    mulai           TEXT NOT NULL,
    selesai         TEXT
);

-- Chat dengan Claude per lowongan.
CREATE TABLE IF NOT EXISTS chat_pesan (
    id              INTEGER PRIMARY KEY,
    lowongan_id     INTEGER NOT NULL REFERENCES lowongan(id),
    peran           TEXT NOT NULL,             -- user | assistant
    isi             TEXT NOT NULL,
    aktivitas       TEXT,                      -- JSON: pencarian web yang dilakukan
    dibuat_pada     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_lowongan ON chat_pesan(lowongan_id, id);

-- Nomor versi yang mudah dibaca untuk tiap isi profil (penilaian menyimpan hash-nya).
CREATE TABLE IF NOT EXISTS profil_versi (
    profil_hash TEXT PRIMARY KEY,
    versi       INTEGER NOT NULL UNIQUE,
    disimpan_pada TEXT NOT NULL
);
"""

# Kolom yang ditambahkan setelah skema awal: (tabel, kolom, definisi)
MIGRASI = [
    ("lowongan", "catatan", "TEXT NOT NULL DEFAULT ''"),
    ("lowongan", "profil_perusahaan", "TEXT"),      # NULL = belum diringkas, '' = halaman tak menjelaskan
]


def _migrasi(con) -> None:
    for tabel, kolom, definisi in MIGRASI:
        ada = {r[1] for r in con.execute(f"PRAGMA table_info({tabel})")}
        if kolom not in ada:
            con.execute(f"ALTER TABLE {tabel} ADD COLUMN {kolom} {definisi}")
    # Profil yang sudah pernah dipakai menilai tapi belum bernomor: urutkan menurut
    # penilaian pertamanya.
    if con.execute("SELECT COUNT(*) FROM profil_versi").fetchone()[0] == 0:
        for (h,) in con.execute("SELECT profil_hash FROM penilaian GROUP BY profil_hash "
                                "ORDER BY MIN(id)").fetchall():
            catat_versi_profil(con, h)


def sekarang() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def koneksi():
    con = sqlite3.connect(config.path("db"))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.executescript(SKEMA)
        _migrasi(con)
        yield con
        con.commit()
    finally:
        con.close()


def url_sudah_ada(con, url_kanonik: str) -> bool:
    return con.execute("SELECT 1 FROM lowongan WHERE url_kanonik = ?",
                       (url_kanonik,)).fetchone() is not None


def cari_sidik(con, sidik: str) -> int | None:
    r = con.execute("SELECT id FROM lowongan WHERE sidik_jari = ? AND duplikat_dari IS NULL",
                    (sidik,)).fetchone()
    return r["id"] if r else None


def simpan_lowongan(con, m: LowonganMentah, e: Ekstraksi | None, sidik: str | None,
                    relevan: bool, duplikat_dari: int | None = None) -> int:
    d = e.model_dump() if e else {}
    cur = con.execute(
        """INSERT INTO lowongan (sumber, url, url_kanonik, sidik_jari, judul, perusahaan, lokasi,
               tipe_kerja, jenis, durasi_bulan, kompensasi, gaji, deadline, bidang, requirement,
               deskripsi, relevan, alasan_filter, duplikat_dari, teks_mentah, diposting_pada,
               ditemukan_pada, profil_perusahaan)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (m.sumber, m.url, m.url_kanonik, sidik,
         d.get("judul") or m.judul, d.get("perusahaan") or m.perusahaan,
         d.get("lokasi") or m.lokasi, d.get("tipe_kerja"), d.get("jenis"),
         d.get("durasi_bulan"), d.get("kompensasi"), d.get("gaji"), d.get("deadline"),
         json.dumps(d.get("bidang", []), ensure_ascii=False),
         json.dumps(d.get("requirement", []), ensure_ascii=False),
         d.get("deskripsi_singkat"), int(relevan), d.get("alasan_filter"),
         duplikat_dari, m.teks, m.diposting_pada or None, sekarang(),
         d.get("profil_perusahaan") if e else None))
    return cur.lastrowid


def kandidat_belum_dinilai(con, profil_hash: str) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT l.* FROM lowongan l
           WHERE l.relevan = 1 AND l.duplikat_dari IS NULL
             AND (l.deadline IS NULL OR l.deadline >= date('now'))
             AND NOT EXISTS (SELECT 1 FROM penilaian p
                             WHERE p.lowongan_id = l.id AND p.profil_hash = ?)
           ORDER BY l.id""", (profil_hash,)).fetchall()


def simpan_penilaian(con, p: Penilaian, model: str, profil_hash: str, ambang: int) -> None:
    con.execute(
        """INSERT OR REPLACE INTO penilaian (lowongan_id, model, profil_hash, skor, kecocokan,
               alasan, must_have, red_flags, lolos, dinilai_pada)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (p.id, model, profil_hash, p.skor, p.kecocokan, p.alasan,
         json.dumps(p.must_have_terpenuhi, ensure_ascii=False),
         json.dumps(p.red_flags, ensure_ascii=False), int(p.skor >= ambang), sekarang()))


def belum_dikirim(con, profil_hash: str) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT l.*, p.skor, p.kecocokan, p.alasan, p.red_flags FROM lowongan l
           JOIN penilaian p ON p.lowongan_id = l.id AND p.profil_hash = ?
           WHERE p.lolos = 1 AND l.status = 'baru'
           ORDER BY p.skor DESC""", (profil_hash,)).fetchall()


def tandai(con, ids: list[int], status: str) -> None:
    con.executemany("UPDATE lowongan SET status = ? WHERE id = ?", [(status, i) for i in ids])


def catat_versi_profil(con, profil_hash: str) -> int:
    r = con.execute("SELECT versi FROM profil_versi WHERE profil_hash = ?", (profil_hash,)).fetchone()
    if r:
        return r[0]
    versi = con.execute("SELECT COALESCE(MAX(versi), 0) + 1 FROM profil_versi").fetchone()[0]
    con.execute("INSERT INTO profil_versi VALUES (?, ?, ?)", (profil_hash, versi, sekarang()))
    return versi


def mulai_riset(con, kunci: str, nama: str, lowongan_id: int, pid: int | None = None) -> None:
    """`pid` = proses yang mengerjakan riset (bawaan: proses ini sendiri)."""
    pid = pid or os.getpid()
    con.execute(
        """INSERT INTO riset_perusahaan (kunci, nama, status, pid, dari_lowongan, mulai)
           VALUES (?, ?, 'berjalan', ?, ?, ?)
           ON CONFLICT(kunci) DO UPDATE SET status = 'berjalan', pid = excluded.pid,
               error = NULL, dari_lowongan = excluded.dari_lowongan, mulai = excluded.mulai,
               selesai = NULL""",
        (kunci, nama, pid, lowongan_id, sekarang()))


def selesai_riset(con, kunci: str, laporan: dict | None, statistik: dict, error: str | None) -> None:
    # Riset ulang yang gagal tidak menghapus laporan lama yang masih berguna.
    con.execute(
        """UPDATE riset_perusahaan SET status = ?, laporan = COALESCE(?, laporan),
               statistik = ?, error = ?, selesai = ? WHERE kunci = ?""",
        ("gagal" if error else "selesai",
         json.dumps(laporan, ensure_ascii=False) if laporan else None,
         json.dumps(statistik, ensure_ascii=False), error, sekarang(), kunci))


def simpan_laporan_riset(con, kunci: str, laporan: dict) -> None:
    con.execute("UPDATE riset_perusahaan SET laporan = ? WHERE kunci = ?",
                (json.dumps(laporan, ensure_ascii=False), kunci))


def baca_riset(con, kunci: str):
    return con.execute("SELECT * FROM riset_perusahaan WHERE kunci = ?", (kunci,)).fetchone()


def mulai_run(con) -> int:
    return con.execute("INSERT INTO runs (mulai) VALUES (?)", (sekarang(),)).lastrowid


def selesai_run(con, run_id: int, statistik: dict) -> None:
    con.execute("UPDATE runs SET selesai = ?, statistik = ? WHERE id = ?",
                (sekarang(), json.dumps(statistik, ensure_ascii=False), run_id))
