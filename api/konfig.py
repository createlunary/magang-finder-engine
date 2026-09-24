"""Baca/tulis konfigurasi pipeline dari website, dipetakan ke bentuk data frontend.

YAML ditulis dengan ruamel.yaml (round-trip) supaya komentar penjelas di
config/*.yaml tidak hilang saat disimpan dari website. Semua penulisan lewat
file sementara + os.replace: run yang sedang membaca tidak pernah melihat file
setengah jadi.
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq

from pipeline import config

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096          # URL panjang jangan dipotong jadi beberapa baris
_yaml.indent(mapping=2, sequence=4, offset=2)   # gaya file asli: "  - item" di bawah kunci

SETTINGS = config.CONFIG_DIR / "settings.yaml"
SUMBER = config.CONFIG_DIR / "sumber_situs.yaml"
PROFIL = config.CONFIG_DIR / "profile.json"

LABEL_SUMBER = {
    "jobstreet": "JobStreet", "glints": "Glints", "kalibrr": "Kalibrr",
    "karirhub": "Karirhub (Kemnaker)", "dealls": "Dealls", "karir": "Karir.com",
    "topkarir": "TopKarir", "urbanhire": "Urbanhire", "linkedin": "LinkedIn",
    "jooble": "Jooble", "adzuna": "Adzuna", "careerjet": "Careerjet",
    "lokerid": "Loker.id", "kitalulus": "KitaLulus", "techinasia": "Tech in Asia",
}

MODEL_API = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}


def _tulis_atomik(path: Path, teks: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(teks, encoding="utf-8")
    os.replace(tmp, path)


def _muat_yaml(path: Path):
    return _yaml.load(path.read_text(encoding="utf-8"))


def _simpan_yaml(path: Path, data) -> None:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    _tulis_atomik(path, buf.getvalue())
    config.settings.cache_clear()
    config.sumber.cache_clear()


# ------------------------------------------------------------------ settings

def baca_settings() -> dict:
    s = _muat_yaml(SETTINGS)
    ll = s["large_llm"]
    if ll["backend"] == "claude-code":
        model = ll["claude_code"]["model"]
    else:
        model = next((k for k, v in MODEL_API.items() if v == ll["model"]), "opus")
    return {
        "threshold": int(ll["ambang_skor"]),
        "backend": "claude-code" if ll["backend"] == "claude-code" else "claude-api",
        "model": model,
        "pagesPerSource": int(s["connector"]["maks_detail_per_sumber"]),
        "schedule": str(s.get("jadwal", "07:00")),
    }


def simpan_settings(baru: dict) -> dict:
    if not 0 <= baru["threshold"] <= 100:
        raise ValueError("threshold harus 0-100")
    if baru["model"] not in MODEL_API:
        raise ValueError(f"model tidak dikenal: {baru['model']}")
    if not 1 <= baru["pagesPerSource"] <= 200:
        raise ValueError("pagesPerSource harus 1-200")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", baru["schedule"]):
        raise ValueError("schedule harus HH:MM")

    s = _muat_yaml(SETTINGS)
    ll = s["large_llm"]
    ll["ambang_skor"] = baru["threshold"]
    ll["backend"] = "claude-code" if baru["backend"] == "claude-code" else "api"
    ll["claude_code"]["model"] = baru["model"]
    ll["model"] = MODEL_API[baru["model"]]
    s["connector"]["maks_detail_per_sumber"] = baru["pagesPerSource"]
    s["jadwal"] = baru["schedule"]
    _simpan_yaml(SETTINGS, s)
    return baca_settings()


# ------------------------------------------------------------------- sumber

def _sumber_mentah():
    return _muat_yaml(SUMBER)


def konfig_sumber() -> list[dict]:
    return [dict(c) for c in _sumber_mentah()["sumber"]]


def ke_source(cfg: dict, status_run: dict | None) -> dict:
    """Satu entri sumber_situs.yaml + statistik run terakhirnya → `Source` frontend."""
    kunci = cfg.get("kata_kunci") or config.settings()["kata_kunci"]
    if cfg.get("pencarian"):
        target = "\n".join(e if isinstance(e, str) else e["url"] for e in cfg["pencarian"])
    else:
        target = cfg.get("url_cari", "")
    if status_run and not cfg.get("aktif") and cfg.get("catatan"):
        # Sumber nonaktif: alasan dinonaktifkan lebih berguna daripada error lamanya.
        status, terakhir, pesan = "ok", status_run[1], cfg["catatan"]
    elif status_run:
        s, waktu = status_run
        errors = s.get("error", [])
        gagal = bool(errors) and not s.get("tautan")
        pesan = (errors[0][:140] if gagal else
                 f"{s.get('tautan', 0)} tautan · {s.get('baru', 0)} baru · {s.get('relevan', 0)} relevan"
                 + (f" · {len(errors)} peringatan" if errors else ""))
        status, terakhir = ("error" if gagal else "ok"), waktu
    else:
        status, terakhir, pesan = "ok", "", cfg.get("catatan") or "belum pernah dijalankan"
    stats = None
    if status_run:
        s = status_run[0]
        # Corong satu run: tautan di halaman daftar → disaring (judul/sudah dikenal/ditunda)
        # → dibuka & baru → relevan. Dipakai visual "stasiun bumi" di halaman Sumber.
        stats = {"links": s.get("tautan", 0),
                 "filtered": s.get("disaring_judul", 0) + s.get("dibuang_judul", 0),
                 "known": s.get("sudah_dikenal", 0), "deferred": s.get("ditunda", 0),
                 "new": s.get("baru", 0), "relevant": s.get("relevan", 0),
                 "seconds": s.get("detik", 0), "warnings": len(s.get("error", []))}
    return {
        "key": cfg["nama"], "name": LABEL_SUMBER.get(cfg["nama"], cfg["nama"]),
        "enabled": bool(cfg.get("aktif")), "keywords": ", ".join(kunci),
        "targetedQuery": target, "status": status, "lastRunAt": terakhir, "lastMessage": pesan,
        "stats": stats,
    }


def ubah_sumber(nama: str, patch: dict) -> dict:
    data = _sumber_mentah()
    cfg = next((c for c in data["sumber"] if c["nama"] == nama), None)
    if cfg is None:
        raise KeyError(nama)
    if "enabled" in patch:
        cfg["aktif"] = bool(patch["enabled"])
    if "keywords" in patch:
        kunci = [k.strip() for k in str(patch["keywords"]).split(",") if k.strip()]
        if not kunci:
            raise ValueError("kata kunci tidak boleh kosong")
        lama = cfg.get("kata_kunci")
        if list(lama or config.settings()["kata_kunci"]) != kunci:
            baru = CommentedSeq(kunci)
            # Pertahankan gaya penulisan: [a, b] tetap satu baris.
            if lama is None or (isinstance(lama, CommentedSeq) and lama.fa.flow_style()):
                baru.fa.set_flow_style()
            if lama is None and "aktif" in cfg:
                # Kunci baru di akhir blok akan jatuh setelah baris kosong pemisah antar sumber.
                cfg.insert(list(cfg).index("aktif") + 1, "kata_kunci", baru)
            else:
                cfg["kata_kunci"] = baru
    if "targetedQuery" in patch and cfg.get("jenis") == "connector":
        urls = [u.strip() for u in str(patch["targetedQuery"]).splitlines() if u.strip()]
        if not urls:
            raise ValueError("minimal satu URL pencarian")
        for u in urls:
            if not u.startswith(("http://", "https://")):
                raise ValueError(f"bukan URL: {u[:60]}")
        # Konteks (lokasi/remote/jenis) dipertahankan untuk URL yang tidak berubah.
        lama = {(e if isinstance(e, str) else e["url"]): e for e in (cfg.get("pencarian") or [])}
        sekarang = list(lama) if cfg.get("pencarian") else [cfg.get("url_cari", "")]
        if urls == sekarang:
            pass                                    # tidak berubah: jangan sentuh strukturnya
        elif len(urls) == 1 and not cfg.get("pencarian"):
            cfg["url_cari"] = urls[0]
        else:
            cfg["pencarian"] = [lama.get(u) if isinstance(lama.get(u), dict) else {"url": u}
                                for u in urls]
    _simpan_yaml(SUMBER, data)
    return dict(cfg)


# ------------------------------------------------------------------- profil
# Chip di halaman Profil ↔ teks yang dibaca Claude di profile.json.

BIDANG = {
    "Backend Dev": "backend development",
    "Game Dev": "game development (Unreal Engine 5)",
    "ML / NLP": "machine learning / NLP",
    "Web Dev": "web development",
    "Data Analyst": "data analyst",
}
LOKASI_LAIN = "Kota lain"


def _profil_mentah() -> dict:
    return json.loads(PROFIL.read_text(encoding="utf-8")) if PROFIL.exists() else config.profil()


def baca_profil() -> dict:
    p = _profil_mentah()
    sk = p.get("skill_teknis", {})
    teks_ke_chip = {v.lower(): k for k, v in BIDANG.items()}
    pref = p.get("preferensi", {})
    lokasi = ["Remote" if l.lower() == "remote" else l for l in pref.get("lokasi", [])]
    if not pref.get("lokasi_ketat", True):
        lokasi.append(LOKASI_LAIN)
    return {
        "bidang": [teks_ke_chip[b.lower()] for b in p.get("bidang_minat", []) if b.lower() in teks_ke_chip],
        "skills": sk.get("dikuasai") or (sk.get("bahasa_pemrograman", []) + sk.get("framework_tools", [])),
        "skillsFamiliar": sk.get("pernah_dipakai", []),
        "lokasi": lokasi,
        "mustHave": p.get("must_have", []),
        "redFlags": p.get("red_flags", []),
    }


def simpan_profil(baru: dict) -> None:
    p = _profil_mentah()
    chip_dikenal = {v.lower() for v in BIDANG.values()}
    # Bidang yang tidak punya chip (mis. "software engineering umum") dipertahankan.
    lain = [b for b in p.get("bidang_minat", []) if b.lower() not in chip_dikenal]
    p["bidang_minat"] = [BIDANG[b] for b in baru["bidang"] if b in BIDANG] + lain

    sk = p.setdefault("skill_teknis", {})
    sk.pop("bahasa_pemrograman", None)
    sk.pop("framework_tools", None)
    sk["dikuasai"] = baru["skills"]
    sk["pernah_dipakai"] = baru.get("skillsFamiliar", sk.get("pernah_dipakai", []))
    sk.setdefault("catatan_skill", "`pernah_dipakai` = sudah pernah memakai, belum mahir")

    pref = p.setdefault("preferensi", {})
    kota = [l for l in baru["lokasi"] if l != LOKASI_LAIN]
    pref["lokasi"] = ["remote" if l.lower() == "remote" else l for l in kota]
    pref["lokasi_ketat"] = LOKASI_LAIN not in baru["lokasi"]
    daftar = " atau ".join(pref["lokasi"]) or "lokasi mana pun"
    pref["catatan_lokasi"] = (
        f"Hanya bersedia {daftar}. Onsite/hybrid di kota lain adalah ketidakcocokan besar."
        if pref["lokasi_ketat"] else
        f"Mengutamakan {daftar}, tapi bersedia pindah ke kota lain untuk magang yang cocok.")

    p["must_have"] = baru["mustHave"]
    p["red_flags"] = baru["redFlags"]
    _tulis_atomik(PROFIL, json.dumps(p, ensure_ascii=False, indent=2) + "\n")
