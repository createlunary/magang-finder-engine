"""Memuat settings.yaml, sumber_situs.yaml, profile.json, dan .env dari satu tempat."""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")


@lru_cache
def settings() -> dict:
    return yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))


@lru_cache
def sumber() -> list[dict]:
    data = yaml.safe_load((CONFIG_DIR / "sumber_situs.yaml").read_text(encoding="utf-8"))
    return data["sumber"]


def profil_path() -> Path:
    # profile.json berisi data pribadi dan di-gitignore; contoh dipakai kalau belum ada.
    p = CONFIG_DIR / "profile.json"
    return p if p.exists() else CONFIG_DIR / "profile_contoh.json"


def profil() -> dict:
    return json.loads(profil_path().read_text(encoding="utf-8"))


def profil_hash() -> str:
    """Penilaian lama dianggap basi kalau profil berubah."""
    teks = json.dumps(profil(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(teks.encode()).hexdigest()[:12]


def path(nama: str) -> Path:
    # MF_DATA_DIR mengarahkan DB & vector store ke folder lain — untuk benchmark
    # dan uji yang tidak boleh menyentuh data asli.
    alt = os.environ.get("MF_DATA_DIR")
    if alt and nama in ("db", "chroma"):
        p = Path(alt) / Path(settings()["paths"][nama]).name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    p = ROOT / settings()["paths"][nama]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def env(nama: str) -> str:
    return os.environ.get(nama, "").strip()
