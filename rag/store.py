"""Vector store lowongan: embedding bge-m3 via Ollama, disimpan di ChromaDB.

Dedup punya tiga lapis, dari yang termurah:
1. URL kanonik (SQLite) — dicek sebelum halaman detail diambil.
2. Sidik jari judul+perusahaan (SQLite) — lowongan sama di situs berbeda.
3. Kemiripan makna (di sini) — judul ditulis sedikit berbeda, isi sama.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

import chromadb
import httpx

from pipeline import config

_client = None


def _koleksi():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(config.path("chroma")))
    return _client.get_or_create_collection("lowongan", metadata={"hnsw:space": "cosine"})


def _normal(teks: str) -> str:
    teks = unicodedata.normalize("NFKD", teks or "").encode("ascii", "ignore").decode()
    teks = re.sub(r"\b(pt|cv|tbk|inc|ltd|persero)\b\.?", " ", teks.lower())
    return re.sub(r"[^a-z0-9]+", " ", teks).strip()


def sidik_jari(judul: str, perusahaan: str) -> str:
    return hashlib.sha1(f"{_normal(judul)}|{_normal(perusahaan)}".encode()).hexdigest()[:16]


def embed(teks: list[str]) -> list[list[float]]:
    s = config.settings()
    r = httpx.post(f"{s['local_llm']['host']}/api/embed",
                   json={"model": s["embedding"]["model"], "input": teks}, timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"]


def teks_dokumen(judul: str, perusahaan: str, lokasi: str, deskripsi: str) -> str:
    return f"{judul} | {perusahaan} | {lokasi}\n{deskripsi}"


def cari_duplikat(dokumen: str, perusahaan: str) -> tuple[int | None, list[float]]:
    """Kembalikan (id lowongan yang mirip atau None, embedding dokumen ini)."""
    vektor = embed([dokumen])[0]
    kol = _koleksi()
    if kol.count() == 0:
        return None, vektor
    hasil = kol.query(query_embeddings=[vektor], n_results=3)
    ambang = config.settings()["embedding"]["ambang_duplikat"]
    for id_, jarak, meta in zip(hasil["ids"][0], hasil["distances"][0], hasil["metadatas"][0]):
        # Syarat perusahaan sama mencegah dua posisi "Backend Intern" dari
        # perusahaan berbeda dianggap satu lowongan.
        if 1 - jarak >= ambang and _normal(meta.get("perusahaan", "")) == _normal(perusahaan):
            return int(id_), vektor
    return None, vektor


def simpan(lowongan_id: int, dokumen: str, vektor: list[float], meta: dict) -> None:
    _koleksi().upsert(ids=[str(lowongan_id)], embeddings=[vektor], documents=[dokumen],
                      metadatas=[{k: (v if v is not None else "") for k, v in meta.items()}])


def cari(query: str, n: int = 10) -> list[dict]:
    """Pencarian semantik atas semua lowongan yang pernah dilihat (untuk `mf.py cari`)."""
    hasil = _koleksi().query(query_embeddings=embed([query]), n_results=n)
    return [{"id": int(i), "skor": round(1 - d, 3), **m}
            for i, d, m in zip(hasil["ids"][0], hasil["distances"][0], hasil["metadatas"][0])]
