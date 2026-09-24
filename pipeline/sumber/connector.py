"""Sumber lewat custom browser search connector (sr.py).

Connector dipanggil sebagai proses terpisah dengan interpreternya sendiri: ia
yang memegang Playwright, sesi login, dan pengaman akun. Proyek ini cukup
membaca markdown yang dihasilkannya.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

from .. import config
from ..models import LowonganMentah

# Exit code sr.py ambil → arti, supaya log run bisa menjelaskan kenapa sumber kosong.
KODE = {3: "verifikasi bot / gagal", 4: "butuh login", 5: "diblokir pengaman akun",
        6: "dibatasi laju (429)", 7: "tidak punya akun"}


class ConnectorError(RuntimeError):
    pass


class _Layanan:
    """Satu proses `sr.py layani` untuk seluruh run: Chrome dinyalakan sekali.

    Dipakai berurutan (satu permintaan pada satu waktu): connector memakai satu
    profil Chrome, jadi layanan ini sekaligus menjadi satu-satunya pemegang
    profil selama run. Tutup di akhir run supaya profil bebas lagi untuk
    `sr.py ambil` biasa.
    """

    STATUS = {"butuh-login": "butuh login", "diblokir": "diblokir pengaman akun", "gagal": "gagal"}

    def __init__(self):
        self.p: subprocess.Popen | None = None
        self.q: queue.Queue | None = None
        self.lock = threading.Lock()
        self.n = 0

    def _mulai(self) -> None:
        c = config.settings()["connector"]
        folder = config.path("log")
        folder.mkdir(exist_ok=True)
        log = open(folder / "connector-layani.log", "a", encoding="utf-8")  # noqa: SIM115
        self.p = subprocess.Popen(
            [c["python"], c["sr_py"], "layani"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.q = queue.Queue()
        threading.Thread(target=self._baca, args=(self.p, self.q), daemon=True).start()

    @staticmethod
    def _baca(p: subprocess.Popen, q: queue.Queue) -> None:
        # Dibaca di thread sendiri supaya penantian jawaban bisa diberi batas waktu.
        for baris in p.stdout:
            q.put(baris)
        q.put(None)

    def minta(self, req: dict, batas: float) -> str:
        with self.lock:
            if self.p is None or self.p.poll() is not None:
                self._mulai()
            self.n += 1
            self.p.stdin.write(json.dumps({**req, "id": self.n}, ensure_ascii=False) + "\n")
            self.p.stdin.flush()
            try:
                baris = self.q.get(timeout=batas)
            except queue.Empty:
                self._matikan()
                raise ConnectorError(f"timeout {batas:.0f}s") from None
            if baris is None:
                self._matikan()
                raise ConnectorError("layanan connector berhenti tiba-tiba")
        jawab = json.loads(baris)
        if jawab["status"] != "ok":
            raise ConnectorError(f"{self.STATUS.get(jawab['status'], jawab['status'])}: {jawab.get('pesan', '')}")
        return jawab["teks"]

    def _matikan(self) -> None:
        if self.p and self.p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)], capture_output=True)
        self.p = None

    def tutup(self) -> None:
        """Tutup rapi (EOF di stdin) supaya Chrome sempat menulis cookie ke profil."""
        with self.lock:
            if self.p and self.p.poll() is None:
                try:
                    self.p.stdin.close()
                    self.p.wait(timeout=30)
                except Exception:  # noqa: BLE001
                    self._matikan()
            self.p = None


_LAYANAN = _Layanan()
atexit.register(_LAYANAN.tutup)
tutup_layanan = _LAYANAN.tutup


def ambil(url: str, *, selector: str | None = None, tunggu: str | None = None,
          interaktif: bool = False, jsonld: bool = False, cepat: bool = False) -> str:
    """Ambil satu halaman sebagai markdown.

    `cepat`: baca begitu `selector` muncul, tanpa menunggu jaringan sepi atau
    menggulir halaman (~8 s → ~1 s per halaman detail; isi terbukti identik).
    """
    c = config.settings()["connector"]
    if c.get("pakai_layanan", True) and not interaktif:
        req = {"url": url, "selector": selector, "jsonld": jsonld, "wait_for": tunggu}
        if cepat and selector:
            req.update(wait_for=tunggu or selector, cepat=True)
        return _LAYANAN.minta(req, c["timeout_detik"])
    # Mode interaktif (panel login bisa muncul) memakai `sr.py ambil` biasa.
    tutup_layanan()          # bebaskan profil Chrome dulu
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "halaman.md"
        argv = [c["python"], c["sr_py"], "ambil", url, "--no-images", "--tanpa-indeks",
                "--max-chars", "0", "--out", str(out)]
        if jsonld:
            argv.append("--jsonld")
        if selector:
            argv += ["--selector", selector]
        if tunggu:
            argv += ["--wait-for", tunggu]
        if not interaktif:
            # Run terjadwal tidak ditunggui siapa pun; panel login yang muncul
            # hanya akan menggantung sampai timeout. Laporkan "butuh login" saja.
            argv += ["--no-ui", "--hide-window"]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=c["timeout_detik"])
        except subprocess.TimeoutExpired as e:
            raise ConnectorError(f"timeout {c['timeout_detik']}s") from e
        if p.returncode != 0:
            alasan = KODE.get(p.returncode, f"exit {p.returncode}")
            detail = (p.stderr or "").strip().splitlines()[-1:] or [""]
            raise ConnectorError(f"{alasan}: {detail[0][:200]}")
        return out.read_text(encoding="utf-8") if out.exists() else ""


_BLOK_JSONLD = re.compile(r"\n*## Data terstruktur \(JSON-LD\)\s*```json\n(.*?)\n```\s*", re.S)


def pisah_jsonld(md: str) -> tuple[str, dict | None]:
    """Keluarkan blok JSON-LD dari markdown; kembalikan (teks, JobPosting atau None)."""
    m = _BLOK_JSONLD.search(md)
    if not m:
        return md, None
    teks = md[:m.start()] + md[m.end():]
    try:
        objek = json.loads(m.group(1))
    except ValueError:
        return teks, None
    for o in objek:
        jenis = o.get("@type")
        if jenis == "JobPosting" or (isinstance(jenis, list) and "JobPosting" in jenis):
            return teks, o
    return teks, None


def _slug(teks: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", teks.lower()).strip("-")


def _teks_per_url(md: str, basis: str, pola: str) -> dict[str, list[str]]:
    """url detail (absolut, tanpa query/fragmen) → semua teks tautan yang menunjuknya, urut kemunculan."""
    hasil: dict[str, list[str]] = {}
    for teks, href in re.findall(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)", md):
        if not re.search(pola, href):
            continue
        p = urlparse(urljoin(basis, href))
        hasil.setdefault(urlunparse((p.scheme, p.netloc, p.path, "", "", "")), []).append(teks)
    return hasil


# Teks tautan yang bukan judul lowongan ("View Post", ikon kosong, dst).
_BUKAN_JUDUL = re.compile(r"^(view post|lihat|lihat detail|apply|lamar|detail|selengkapnya)?$", re.I)


def judul_kartu(teks: list[str]) -> str:
    """Judul lowongan dari teks-teks tautan di kartu daftar: yang terpanjang dan bukan tombol."""
    bersih = [re.sub(r"[#*_`]+", " ", t).strip() for t in teks]
    bersih = [re.sub(r"\s+", " ", t) for t in bersih if not _BUKAN_JUDUL.match(t)]
    return max(bersih, key=len, default="")[:160]


def tautan_detail(md: str, basis: str, pola: str, pola_judul: re.Pattern | None = None) -> list[str]:
    """Ambil tautan detail dari markdown, relatif → absolut, tanpa query/fragmen.

    Dengan `pola_judul`, hanya tautan yang salah satu teks tautannya (biasanya
    judul lowongan di kartu) cocok yang diambil — penyaringan paling murah,
    sebelum halaman detail dibuka sama sekali.
    """
    per_url = _teks_per_url(md, basis, pola)
    if pola_judul is None:
        return list(per_url)
    return [u for u, teks in per_url.items() if any(pola_judul.search(t) for t in teks)]


def daftar_pencarian(cfg: dict) -> list[tuple[str, dict]]:
    """Semua URL halaman daftar untuk satu sumber, masing-masing dengan konteksnya.

    `pencarian` (list) memungkinkan beberapa pencarian bertarget per sumber, mis.
    Yogyakarta dan remote. Konteks seperti `tipe_kerja: remote` ikut dibawa ke
    lowongan, karena sebagian situs hanya menyebutnya di filter pencarian,
    bukan di halaman detail. `url_cari` (string) tetap didukung.
    """
    kata_kunci = cfg.get("kata_kunci") or config.settings()["kata_kunci"]
    entri = cfg.get("pencarian") or [{"url": cfg["url_cari"]}]
    hasil: list[tuple[str, dict]] = []
    for e in entri:
        e = {"url": e} if isinstance(e, str) else e
        konteks = {k: v for k, v in e.items() if k != "url"}
        pakai_kunci = "{q" in e["url"]
        for k in (kata_kunci if pakai_kunci else [None]):
            url = e["url"].format(q=quote_plus(k), q_slug=_slug(k)) if k else e["url"]
            if url not in (u for u, _ in hasil):
                hasil.append((url, konteks))
    return hasil


def daftar(cfg: dict, stat: dict, interaktif: bool = False) -> list[tuple[str, str, Callable]]:
    """Fase scrape: baca halaman daftar, kembalikan (url, judul_kartu, fungsi_muat) per lowongan.

    Halaman detail belum diambil di sini — orkestrator lebih dulu membuang URL
    yang sudah dikenal (fase dedup), baru memanggil fungsi_muat untuk sisanya.
    """
    s = config.settings()["connector"]

    # Kalau hanya magang yang dicari, kartu yang judulnya tidak menyebut magang/
    # intern tidak perlu dibuka. Nonaktifkan per sumber dengan `saring_judul: false`.
    from ..local_extract import POLA_MAGANG
    saring = (config.settings()["local_llm"]["hanya_magang"] and cfg.get("saring_judul", True))
    pola_judul = POLA_MAGANG if saring else None

    konteks_per_url: dict[str, dict] = {}
    judul_per_url: dict[str, str] = {}
    for url, konteks in daftar_pencarian(cfg):
        try:
            md = ambil(url, selector=cfg.get("selector_daftar"), tunggu=cfg.get("tunggu"),
                       interaktif=interaktif)
        except ConnectorError as e:
            stat.setdefault("error", []).append(f"daftar {url}: {e}")
            continue
        per_url = _teks_per_url(md, url, cfg["pola_detail"])
        # Filter situs yang sudah menjamin jenisnya magang membuat saringan judul tidak perlu.
        if pola_judul and konteks.get("jenis") != "magang":
            links = [u for u, teks in per_url.items() if any(pola_judul.search(t) for t in teks)]
        else:
            links = list(per_url)
        stat["tautan"] = stat.get("tautan", 0) + len(per_url)
        stat["disaring_judul"] = stat.get("disaring_judul", 0) + len(per_url) - len(links)
        for u in links:
            # Lowongan yang muncul di beberapa pencarian mengumpulkan semua konteksnya.
            konteks_per_url.setdefault(u, {}).update(konteks)
            judul_per_url.setdefault(u, judul_kartu(per_url[u]))
        time.sleep(s["jeda_detik"])

    # Jeda sopan antar halaman detail diatur orkestrator (per situs), bukan di sini.
    def muat(url: str) -> LowonganMentah:
        md = ambil(url, selector=cfg.get("selector_detail"), interaktif=interaktif, jsonld=True,
                   cepat=True)
        teks, jp = pisah_jsonld(md)
        extra = {"konteks": konteks_per_url[url]} if konteks_per_url[url] else {}
        if jp:
            stat["jsonld"] = stat.get("jsonld", 0) + 1
            extra["jobposting"] = jp
        return LowonganMentah(sumber=cfg["nama"], url=url, judul=judul_per_url[url], teks=teks,
                              extra=extra)

    return [(u, judul_per_url[u], lambda u=u: muat(u)) for u in konteks_per_url]
