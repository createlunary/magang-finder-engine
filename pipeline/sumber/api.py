"""Sumber lewat API: Jooble, Adzuna, Careerjet (resmi) dan Dealls, Kalibrr, Tech in Asia (API publik situsnya).

API mengembalikan snippet pendek, bukan halaman penuh — cukup untuk filter
kasar dan ranking, dan jauh lebih murah daripada membuka halaman satu per satu.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator

import httpx

from .. import config
from ..models import LowonganMentah


def _bersih(teks: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", teks or "")).strip()


def _html_baris(h: str | None) -> str:
    """HTML <ul><li>/<p> → teks dengan satu butir per baris."""
    return _bersih(re.sub(r"</(li|p)>|<br\s*/?>", "\n", h or "")).replace("\n ", "\n")


def _butuh(*nama: str) -> list[str]:
    nilai = [config.env(n) for n in nama]
    if not all(nilai):
        raise RuntimeError(f"isi {', '.join(nama)} di .env")
    return nilai


def _kata_kunci(cfg: dict) -> list[str]:
    return cfg.get("kata_kunci") or config.settings()["kata_kunci"]


def _sebagai_daftar(hasil: Iterator[LowonganMentah], stat) -> list[tuple[str, str, Callable]]:
    """API sudah mengembalikan isi lowongan, jadi fungsi muat cukup mengembalikannya."""
    daftar, lihat = [], set()
    for m in hasil:
        stat["tautan"] = stat.get("tautan", 0) + 1
        if m.url_kanonik not in lihat:
            lihat.add(m.url_kanonik)
            daftar.append((m.url, m.judul, lambda m=m: m))
    return daftar


_JOOBLE_BUNTUT = re.compile(r"\n(?:Tampilkan lebih banyak|Lowongan dimuat|Laporkan pekerjaan ini|Pekerjaan serupa)")


def jooble(cfg: dict, stat: dict, interaktif: bool = False, **_) -> list[tuple[str, str, Callable]]:
    """Daftar dari API Jooble (per kata kunci × lokasi), isi dari halaman detail Jooble.

    API hanya memberi cuplikan ±300 karakter, terlalu tipis untuk LLM. Halaman detail
    (/jdp/…) memuat deskripsi penuh + JSON-LD JobPosting, tapi dilindungi Cloudflare —
    jadi dibuka lewat connector. Kalau gagal, cuplikan API tetap dipakai.
    """
    from ..local_extract import POLA_MAGANG
    from . import connector

    (key,) = _butuh("JOOBLE_API_KEY")
    lokasi = cfg.get("lokasi") or [""]
    lokasi = [lokasi] if isinstance(lokasi, str) else lokasi
    hanya_magang = config.settings()["local_llm"].get("hanya_magang", True)

    cuplikan: dict[str, LowonganMentah] = {}
    for loc in lokasi:
        for k in _kata_kunci(cfg):
            # Key Jooble terikat domain negara: key dari jooble.org (AS) ditolak id.jooble.org.
            r = httpx.post(f"https://{cfg.get('domain', 'id.jooble.org')}/api/{key}", timeout=30,
                           json={"keywords": k, "location": loc, "page": "1"})
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                stat["tautan"] = stat.get("tautan", 0) + 1
                m = LowonganMentah(
                    sumber="jooble", url=j["link"], judul=_bersih(j.get("title")),
                    perusahaan=_bersih(j.get("company")), lokasi=_bersih(j.get("location")),
                    diposting_pada=(j.get("updated") or "")[:10],
                    teks="\n".join(filter(None, [_bersih(j.get("title")), _bersih(j.get("company")),
                                                 _bersih(j.get("location")), _bersih(j.get("type")),
                                                 _bersih(j.get("salary")), _bersih(j.get("snippet"))])))
                # Pencarian Jooble longgar ("intern" ikut mencocokkan "internal").
                if hanya_magang and not POLA_MAGANG.search(f"{m.judul}\n{m.teks}"):
                    continue
                cuplikan.setdefault(m.url_kanonik, m)

    def muat(m: LowonganMentah) -> LowonganMentah:
        try:
            md = connector.ambil(m.url, selector="main", jsonld=True, cepat=True, interaktif=interaktif)
        except connector.ConnectorError as e:
            stat.setdefault("error", []).append(f"detail {m.url}: {e} (pakai cuplikan API)")
            return m
        halaman, jp = connector.pisah_jsonld(md)
        if jp and jp.get("description"):
            stat["jsonld"] = stat.get("jsonld", 0) + 1
            # Deskripsi JSON-LD, bukan `main`: `main` ikut memuat daftar "pekerjaan serupa".
            teks = "\n".join(filter(None, [m.judul, m.perusahaan, m.lokasi, _html_baris(jp["description"])]))
            extra = {"jobposting": jp}
        else:
            # Halaman /desc/ tanpa JSON-LD: pakai teksnya sampai sebelum daftar lowongan lain.
            teks = _JOOBLE_BUNTUT.split(halaman.split("\n---\n", 1)[-1], 1)[0].strip()
            if len(teks) <= len(m.teks):
                return m
            extra = {}
        return LowonganMentah(sumber="jooble", url=m.url, judul=m.judul, perusahaan=m.perusahaan,
                              lokasi=m.lokasi, diposting_pada=m.diposting_pada, teks=teks, extra=extra)

    return [(m.url, m.judul, lambda m=m: muat(m)) for m in cuplikan.values()]


def adzuna(cfg: dict, stat: dict, **_) -> list[tuple[str, str, Callable]]:
    app_id, app_key = _butuh("ADZUNA_APP_ID", "ADZUNA_APP_KEY")

    def semua():
        for negara in cfg.get("negara", ["sg"]):
            for k in _kata_kunci(cfg):
                r = httpx.get(f"https://api.adzuna.com/v1/api/jobs/{negara}/search/1", timeout=30,
                              params={"app_id": app_id, "app_key": app_key, "what": k,
                                      "results_per_page": 50, "content-type": "application/json"})
                r.raise_for_status()
                for j in r.json().get("results", []):
                    perusahaan = (j.get("company") or {}).get("display_name", "")
                    lokasi = (j.get("location") or {}).get("display_name", "")
                    yield LowonganMentah(
                        sumber="adzuna", url=j["redirect_url"], judul=_bersih(j.get("title")),
                        perusahaan=perusahaan, lokasi=lokasi,
                        diposting_pada=(j.get("created") or "")[:10],
                        teks=f"{_bersih(j.get('title'))}\n{perusahaan}\n{lokasi}\n"
                             f"{_bersih(j.get('description'))}")
    return _sebagai_daftar(semua(), stat)


def careerjet(cfg: dict, stat: dict, **_) -> list[tuple[str, str, Callable]]:
    (affid,) = _butuh("CAREERJET_AFFID")

    def semua():
        for k in _kata_kunci(cfg):
            r = httpx.get("http://public.api.careerjet.net/search", timeout=30, params={
                "locale_code": cfg.get("locale", "id_ID"), "keywords": k, "affid": affid,
                "user_ip": "127.0.0.1", "user_agent": "magang-finder/0.1", "pagesize": 50})
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                yield LowonganMentah(
                    sumber="careerjet", url=j["url"], judul=_bersih(j.get("title")),
                    perusahaan=_bersih(j.get("company")), lokasi=_bersih(j.get("locations")),
                    diposting_pada=j.get("date", ""),
                    teks="\n".join(filter(None, [_bersih(j.get("title")), _bersih(j.get("company")),
                                                 _bersih(j.get("locations")), _bersih(j.get("salary")),
                                                 _bersih(j.get("description"))])))
    return _sebagai_daftar(semua(), stat)


# ------------------------------------------------------------------- Dealls
# Dealls memberi 404 ke browser otomasi, tapi halaman daftarnya sendiri memanggil API
# publik tanpa login ini. Satu panggilan daftar + satu panggilan detail per lowongan.
_DEALLS = "https://api.sejutacita.id/v1"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) magang-finder/0.1"}
_PENGATURAN_DEALLS = {"onSite": "onsite", "hybrid": "hybrid", "remote": "remote"}


def _dealls_detail(j: dict) -> LowonganMentah:
    d = httpx.get(f"{_DEALLS}/job-portal/job/{j['id']}", headers=_UA, timeout=30)
    d.raise_for_status()
    d = d.json()["data"]["result"]
    co = d.get("company") or {}
    kota = (j.get("city") or {}).get("name", "")
    gaji = d.get("salaryRange") or {}
    teks = "\n".join(filter(None, [
        d.get("role"), co.get("name"), kota, f"Pengaturan kerja: {j.get('workplaceType', '')}",
        f"Jenis: {', '.join(d.get('employmentTypes') or [])}",
        f"Gaji: {gaji.get('start')} - {gaji.get('end')}" if gaji.get("start") else "",
        "Skill: " + ", ".join(s["name"] for s in j.get("skills") or []),
        "Tanggung jawab:\n" + _html_baris(d.get("responsibilities")),
        "Persyaratan:\n" + _html_baris(d.get("requirements")),
        _html_baris(d.get("description")),
        "Tentang perusahaan:\n" + _html_baris(co.get("description")) if co.get("description") else "",
    ]))
    tipe = _PENGATURAN_DEALLS.get(j.get("workplaceType", ""))
    return LowonganMentah(
        sumber="dealls", url=f"https://dealls.com/loker/{j['slug']}~{(j.get('company') or {}).get('slug', '')}",
        judul=j.get("role", ""), perusahaan=co.get("name", ""), lokasi=kota,
        diposting_pada=(j.get("publishedAt") or "")[:10], teks=teks,
        extra={"konteks": {k: v for k, v in (("jenis", "magang"), ("tipe_kerja", tipe)) if v}})


def dealls(cfg: dict, stat: dict, **_) -> list[tuple[str, str, Callable]]:
    """Semua lowongan magang Dealls (±100–150), disaring kota di sini supaya Qwen
    tidak membuka lowongan Jakarta satu per satu. Remote selalu lolos."""
    kota = [k.lower() for k in cfg.get("kota", [])]
    daftar, hal = [], 1
    while True:
        r = httpx.get(f"{_DEALLS}/explore-job/job", headers=_UA, timeout=30, params={
            "published": "true", "status": "active", "limit": 50, "page": hal,
            "employmentTypes": "internship", "sortParam": "mostRelevant"})
        r.raise_for_status()
        data = r.json()["data"]
        for j in data["docs"]:
            stat["tautan"] = stat.get("tautan", 0) + 1
            nama_kota = ((j.get("city") or {}).get("name") or "").lower()
            if kota and j.get("workplaceType") != "remote" and not any(k in nama_kota for k in kota):
                continue
            url = f"https://dealls.com/loker/{j['slug']}~{(j.get('company') or {}).get('slug', '')}"
            daftar.append((url, j.get("role", ""), lambda j=j: _dealls_detail(j)))
        if hal >= data.get("totalPages", 1) or hal >= cfg.get("maks_halaman", 6):
            break
        hal += 1
    return daftar


# ------------------------------------------------------------------ Kalibrr
# API publik halaman job board Kalibrr. Respons daftar sudah memuat deskripsi,
# kualifikasi, dan profil perusahaan — tidak ada halaman detail yang perlu dibuka.
_KALIBRR = "https://www.kalibrr.com/kjs/job_board/search"


def _kota_kalibrr(j: dict) -> str:
    return ((j.get("google_location") or {}).get("address_components") or {}).get("city") or ""


def _kalibrr_mentah(j: dict) -> LowonganMentah:
    co = j.get("company") or {}
    kota = _kota_kalibrr(j)
    tipe = "remote" if j.get("is_work_from_home") else "hybrid" if j.get("is_hybrid") else ""
    teks = "\n".join(filter(None, [
        j.get("name"), j.get("company_name"), kota, f"Jenis: {j.get('tenure', '')}",
        f"Pengaturan kerja: {tipe}" if tipe else "",
        f"Batas lamaran: {(j.get('application_end_date') or '')[:10]}",
        "Deskripsi:\n" + _html_baris(j.get("description")),
        "Kualifikasi:\n" + _html_baris(j.get("qualifications")),
        "Tentang perusahaan:\n" + _bersih(co.get("description")) if co.get("description") else "",
    ]))
    return LowonganMentah(
        sumber="kalibrr", url=f"https://www.kalibrr.id/id-ID/c/{co.get('code', '')}/jobs/{j['id']}/{j.get('slug', '')}",
        judul=j.get("name", ""), perusahaan=j.get("company_name", ""), lokasi=kota,
        diposting_pada=(j.get("activation_date") or "")[:10], teks=teks,
        extra={"konteks": {"tipe_kerja": tipe}} if tipe else {})


def kalibrr(cfg: dict, stat: dict, **_) -> list[tuple[str, str, Callable]]:
    """Seluruh lowongan Kalibrr Indonesia (±1.200, 100 per panggilan), lalu disaring:
    kota target atau WFH, dan — kalau hanya_magang — judul/isi yang terlihat magang."""
    from ..local_extract import terlihat_magang

    kota = [k.lower() for k in cfg.get("kota", [])]
    hanya_magang = config.settings()["local_llm"].get("hanya_magang", True)
    daftar, off = [], 0
    while off < cfg.get("maks_lowongan", 3000):
        r = httpx.get(_KALIBRR, headers=_UA, timeout=30,
                      params={"limit": 100, "offset": off, "country": "Indonesia", "text": ""})
        r.raise_for_status()
        data = r.json()
        for j in data["jobs"]:
            stat["tautan"] = stat.get("tautan", 0) + 1
            if kota and not j.get("is_work_from_home") and not any(k in _kota_kalibrr(j).lower() for k in kota):
                continue
            m = _kalibrr_mentah(j)
            if hanya_magang and not terlihat_magang(m.judul, m.teks):
                continue
            daftar.append((m.url, m.judul, lambda m=m: m))
        off += len(data["jobs"])
        if not data["jobs"] or off >= data.get("count", 0):
            break
    return daftar


# ------------------------------------------------------------ Tech in Asia
# Halaman /jobs/search mencari lewat Algolia dengan search key publik (hanya-baca) yang
# tertanam di halamannya. Seluruh index ±300 lowongan dan tiap record memuat deskripsi
# lengkap, jadi satu panggilan cukup.
_TIA = "https://219wx3mpv4-dsn.algolia.net/1/indexes/job_postings/query"
_TIA_H = {"x-algolia-application-id": "219WX3MPV4", "x-algolia-api-key": "b528008a75dc1c4402bfe0d8db8b3f8e",
          "Referer": "https://www.techinasia.com/", "Origin": "https://www.techinasia.com"}
_PENGATURAN_TIA = {"on-site": "onsite", "hybrid": "hybrid", "remote": "remote"}


def _tia_mentah(j: dict) -> LowonganMentah:
    co = j.get("company") or {}
    kota = (j.get("city") or {}).get("name", "")
    tipe = "remote" if j.get("is_remote") else _PENGATURAN_TIA.get(j.get("work_arrangement", ""), "")
    jenis = (j.get("job_type") or {}).get("name", "")
    gaji = f"Gaji: {j['salary_min']} - {j['salary_max']}" if (j.get("salary_min") or 0) > 1 else ""
    teks = "\n".join(filter(None, [
        j.get("title"), co.get("name"), kota, f"Jenis: {jenis}", f"Pengaturan kerja: {tipe}" if tipe else "",
        gaji, f"Pengalaman: {j.get('experience', '')} tahun" if j.get("experience") else "",
        "Skill: " + ", ".join(s["name"] for s in j.get("job_skills") or []),
        _html_baris(j.get("description")),
    ]))
    konteks = {k: v for k, v in (("tipe_kerja", tipe), ("jenis", "magang" if jenis == "Internship" else "")) if v}
    return LowonganMentah(
        sumber="techinasia", url=f"https://www.techinasia.com/jobs/{j['id']}", judul=j.get("title", ""),
        perusahaan=co.get("name", ""), lokasi=kota, diposting_pada=(j.get("published_at") or "")[:10],
        teks=teks, extra={"konteks": konteks} if konteks else {})


def techinasia(cfg: dict, stat: dict, **_) -> list[tuple[str, str, Callable]]:
    """Seluruh index, disaring kota target atau remote, dan — kalau hanya_magang —
    tipe Internship atau judul/isi yang terlihat magang."""
    from ..local_extract import terlihat_magang

    kota = [k.lower() for k in cfg.get("kota", [])]
    hanya_magang = config.settings()["local_llm"].get("hanya_magang", True)
    r = httpx.post(_TIA, headers=_TIA_H, timeout=30, json={"params": "query=&hitsPerPage=1000"})
    r.raise_for_status()
    daftar = []
    for j in r.json()["hits"]:
        stat["tautan"] = stat.get("tautan", 0) + 1
        remote = j.get("is_remote") or j.get("work_arrangement") == "remote"
        nama_kota = ((j.get("city") or {}).get("name") or "").lower()
        if kota and not remote and not any(k in nama_kota for k in kota):
            continue
        m = _tia_mentah(j)
        magang = (j.get("job_type") or {}).get("name") == "Internship"
        if hanya_magang and not magang and not terlihat_magang(m.judul, m.teks):
            continue
        daftar.append((m.url, m.judul, lambda m=m: m))
    return daftar
