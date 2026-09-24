"""Bentuk data yang mengalir di pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field

# Parameter pelacak yang tidak mengubah lowongan yang ditunjuk.
_PARAM_PELACAK = re.compile(r"^(utm_|ref|source|trk|fbclid|gclid|from|tracking)", re.I)


def kanonik(url: str) -> str:
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query) if not _PARAM_PELACAK.match(k)]
    return urlunparse((p.scheme.lower(), p.netloc.lower().removeprefix("www."),
                       p.path.rstrip("/"), "", urlencode(sorted(query)), ""))


@dataclass
class LowonganMentah:
    """Keluaran adapter sumber, sebelum disentuh LLM."""
    sumber: str
    url: str
    teks: str                       # isi halaman detail / snippet API, apa adanya
    judul: str = ""
    perusahaan: str = ""
    lokasi: str = ""
    diposting_pada: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def url_kanonik(self) -> str:
        return kanonik(self.url)


class Ekstraksi(BaseModel):
    """Skema keluaran LLM lokal (dipaksakan lewat `format` Ollama).

    Field kategori memakai Literal supaya skema JSON memuat `enum` — tanpa itu
    model 7B mengarang nilai sendiri ("trainee", nominal gaji, dst).
    """
    judul: str
    perusahaan: str
    lokasi: str
    tipe_kerja: Literal["remote", "onsite", "hybrid", "tidak_disebut"]
    jenis: Literal["magang", "fulltime", "parttime", "kontrak", "lainnya"]
    durasi_bulan: int | None = None
    kompensasi: Literal["berbayar", "sertifikat", "tidak_ada", "tidak_disebut"]
    gaji: str | None = Field(None, description="nominal gaji/uang saku apa adanya kalau disebut")
    deadline: str | None = Field(None, description="YYYY-MM-DD kalau disebut")
    bidang: list[str] = Field(default_factory=list)
    requirement: list[str] = Field(default_factory=list)
    deskripsi_singkat: str = ""
    profil_perusahaan: str = Field("", description="1-2 kalimat tentang perusahaannya (bidang, produk, sejak kapan) "
                                                    "bila halaman menjelaskannya; kosong bila tidak")
    relevan_it: bool = Field(description="bidang informatika / software / data / ML / game")
    alasan_filter: str = ""


class Penilaian(BaseModel):
    """Satu hasil ranking dari large LLM."""
    id: int
    skor: int = Field(ge=0, le=100)
    kecocokan: Literal["tinggi", "sedang", "rendah"]
    alasan: str
    must_have_terpenuhi: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)


class DaftarPenilaian(BaseModel):
    penilaian: list[Penilaian]
