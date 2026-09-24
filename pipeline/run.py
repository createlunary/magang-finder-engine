"""Orkestrator satu run, empat fase berurutan (lihat docs/diagrams/02_alur_kerja_detail.png):

1. scrape     — halaman daftar tiap sumber aktif → kandidat URL
2. dedup      — buang URL yang sudah ada di store, batasi jatah per sumber
3. local_llm  — ambil halaman detail, ekstraksi + filter kasar Qwen, dedup lintas situs, simpan
4. ranking    — Claude menilai kandidat yang belum dinilai dengan profil saat ini
lalu notifikasi dan catatan run.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

from rag import store

from . import config, db, local_extract, rank
from .models import LowonganMentah, kanonik
from .progres import Progres
from .sumber import daftar

log = logging.getLogger("magang")


def _proses(con, m: LowonganMentah, stat: dict, p: Progres) -> None:
    try:
        e = local_extract.ekstrak(m)
    except Exception as ex:  # noqa: BLE001 — tidak disimpan, jadi dicoba lagi run berikutnya
        stat.setdefault("error", []).append(f"ekstrak {m.url}: {ex}")
        p.catat(f"  ! ekstraksi gagal: {m.url}", "warn")
        return
    relevan = local_extract.lolos_filter_kasar(e)
    dokumen = store.teks_dokumen(e.judul, e.perusahaan, e.lokasi, e.deskripsi_singkat)

    # Tanpa nama perusahaan, dua "Programmer" dari tempat berbeda akan tampak
    # identik — dedup lintas situs hanya dilakukan kalau perusahaannya diketahui.
    sidik, dup, vektor = None, None, None
    if e.perusahaan:
        sidik = store.sidik_jari(e.judul, e.perusahaan)
        dup = db.cari_sidik(con, sidik)
        if dup is None:
            dup, vektor = store.cari_duplikat(dokumen, e.perusahaan)
    if vektor is None and dup is None:
        vektor = store.embed([dokumen])[0]

    lid = db.simpan_lowongan(con, m, e, sidik, relevan, duplikat_dari=dup)
    con.commit()
    if dup is not None:
        stat["duplikat"] = stat.get("duplikat", 0) + 1
        p.catat(f"  = duplikat #{dup}: {e.judul} — {e.perusahaan}")
        return
    store.simpan(lid, dokumen, vektor, {"judul": e.judul, "perusahaan": e.perusahaan,
                                        "lokasi": e.lokasi, "sumber": m.sumber,
                                        "relevan": int(relevan), "url": m.url})
    stat["baru"] = stat.get("baru", 0) + 1
    stat["relevan"] = stat.get("relevan", 0) + int(relevan)
    p.catat(f"  {'✓' if relevan else '·'} {e.judul} — {e.perusahaan or '?'}")


def _selang_seling(per_sumber: dict[str, list]) -> list[tuple[str, tuple]]:
    """JobStreet, Glints, Kalibrr, JobStreet, … — supaya jeda sopan satu situs
    terpakai untuk mengambil situs lain, bukan ditunggu kosong."""
    antrean, sisa = [], {n: list(v) for n, v in per_sumber.items() if v}
    while sisa:
        for nama in list(sisa):
            antrean.append((nama, sisa[nama].pop(0)))
            if not sisa[nama]:
                del sisa[nama]
    return antrean


def _ambil_di_latar(antrean: list[tuple[str, tuple]], jeda: dict[str, float]):
    """Hasilkan (nama_sumber, LowonganMentah | Exception) sambil halaman berikutnya
    sudah diambil di latar belakang.

    Pengambilan sengaja SATU per satu: connector memakai satu profil Chrome
    (sesi login), dan dua proses yang membukanya bersamaan akan saling menutup
    browser. Yang berjalan bersamaan hanya pengambilan (jaringan) dengan
    ekstraksi Qwen (GPU) di thread utama.
    """
    q: queue.Queue = queue.Queue(maxsize=2)       # cukup untuk menjaga GPU tetap sibuk
    berhenti = threading.Event()

    def pengambil():
        terakhir: dict[str, float] = {}
        for nama, (_url, _judul, muat) in antrean:
            if berhenti.is_set():
                break
            tunggu = terakhir.get(nama, 0.0) + jeda.get(nama, 0) - time.monotonic()
            if tunggu > 0:
                time.sleep(tunggu)
            try:
                hasil = muat()
            except Exception as ex:  # noqa: BLE001 — diteruskan ke thread utama untuk dicatat
                hasil = ex
            terakhir[nama] = time.monotonic()
            q.put((nama, hasil))
        q.put(None)

    t = threading.Thread(target=pengambil, name="pengambil-detail", daemon=True)
    t.start()
    try:
        while (item := q.get()) is not None:
            yield item
    finally:
        berhenti.set()


def _aktif(nama_sumber: list[str] | None) -> list[dict]:
    if nama_sumber:
        return [c for c in config.sumber() if c["nama"] in nama_sumber]
    return [c for c in config.sumber() if c.get("aktif")]


def jalankan(nama_sumber: list[str] | None = None, *, interaktif: bool = False,
             tanpa_rank: bool = False, tanpa_kirim: bool = False,
             tanpa_kumpul: bool = False) -> dict:
    stat: dict = {"per_sumber": {}}
    t0 = time.time()
    with db.koneksi() as con:
        run_id = db.mulai_run(con)
        con.commit()
        p = Progres(run_id)
        try:
            if not tanpa_kumpul:
                _kumpulkan(con, _aktif(nama_sumber), stat, p, interaktif)
            if not tanpa_rank:
                _nilai(con, stat, p)
            _kirim(con, stat, p, tanpa_kirim)
        except Exception as ex:
            stat["fatal"] = f"{type(ex).__name__}: {ex}"
            p.catat(f"RUN GAGAL — {stat['fatal']}", "warn")
            raise
        finally:
            from .sumber.connector import tutup_layanan
            tutup_layanan()          # lepaskan profil Chrome untuk pemakai connector lain
            stat["detik"] = round(time.time() - t0)
            db.selesai_run(con, run_id, stat)
            p.selesai()
    return stat


def _kumpulkan(con, sumber: list[dict], stat: dict, p: Progres, interaktif: bool) -> None:
    # 1. scrape: halaman daftar
    p.tahap("scrape")
    kandidat: dict[str, list] = {}
    for cfg in sumber:
        s = stat["per_sumber"].setdefault(cfg["nama"], {})
        mulai = time.time()
        try:
            kandidat[cfg["nama"]] = daftar(cfg, s, interaktif=interaktif)
        except Exception as ex:  # noqa: BLE001 — satu sumber gagal tidak menghentikan run
            s.setdefault("error", []).append(str(ex))
            kandidat[cfg["nama"]] = []
        s["detik"] = round(time.time() - mulai)
        pesan = f"SCRAPE {cfg['nama']} — {s.get('tautan', 0)} tautan, {len(kandidat[cfg['nama']])} kandidat"
        p.catat(pesan)
        for err in s.get("error", []):
            p.catat(f"  ! {cfg['nama']}: {err[:160]}", "warn")

    # 2. dedup: buang yang sudah dikenal (termasuk yang muncul di dua sumber sekaligus),
    #    lalu saring judul yang jelas bukan IT — keduanya SEBELUM halaman detail diambil.
    p.tahap("dedup")
    baru: dict[str, list[tuple[str, str, Callable]]] = {}
    lihat: set[str] = set()
    for cfg in sumber:
        s = stat["per_sumber"][cfg["nama"]]
        baru[cfg["nama"]] = []
        for url, judul, muat in kandidat[cfg["nama"]]:
            k = kanonik(url)
            if k in lihat or db.url_sudah_ada(con, k):
                s["sudah_dikenal"] = s.get("sudah_dikenal", 0) + 1
                continue
            lihat.add(k)
            baru[cfg["nama"]].append((url, judul, muat))
    total = sum(len(v) for v in kandidat.values())
    jumlah_baru = sum(len(v) for v in baru.values())
    p.catat(f"DEDUP — {total} kandidat, {total - jumlah_baru} sudah dikenal, {jumlah_baru} baru")

    if jumlah_baru and config.settings()["local_llm"].get("saring_judul", True):
        semua = [(nama, item) for nama, v in baru.items() for item in v]
        buka = local_extract.saring_judul([item[1] for _, item in semua])
        dibuang = [(nama, item[1]) for (nama, item), b in zip(semua, buka) if not b]
        for nama in baru:
            baru[nama] = [item for (n, item), b in zip(semua, buka) if n == nama and b]
        for nama, _ in dibuang:
            s = stat["per_sumber"][nama]
            s["dibuang_judul"] = s.get("dibuang_judul", 0) + 1
        stat["dibuang_judul"] = len(dibuang)
        contoh = ", ".join(j for _, j in dibuang[:4])
        p.catat(f"SARING JUDUL — {len(dibuang)} dari {jumlah_baru} jelas bukan IT, tidak dibuka"
                + (f" (mis. {contoh})" if contoh else ""))

    maks_bawaan = config.settings()["connector"]["maks_detail_per_sumber"]
    for cfg in sumber:
        v, maks = baru[cfg["nama"]], cfg.get("maks_detail", maks_bawaan)
        if len(v) > maks:
            stat["per_sumber"][cfg["nama"]]["ditunda"] = len(v) - maks   # diambil run berikutnya
            baru[cfg["nama"]] = v[:maks]
    antrean = _selang_seling(baru)

    # 3. local_llm: ambil detail + ekstraksi + filter kasar + dedup lintas situs
    p.tahap("local_llm")
    model = config.settings()["local_llm"]["model"]
    p.catat(f"LOCAL LLM {model} — {len(antrean)} lowongan")
    jeda = {c["nama"]: (config.settings()["connector"]["jeda_detik"] if c["jenis"] == "connector" else 0)
            for c in sumber}
    for i, (nama, hasil) in enumerate(_ambil_di_latar(antrean, jeda), 1):
        s = stat["per_sumber"][nama]
        if isinstance(hasil, Exception):
            s.setdefault("error", []).append(f"detail: {hasil}")
            p.catat(f"  ! [{i}/{len(antrean)}] {nama}: halaman detail gagal", "warn")
            continue
        _proses(con, hasil, s, p)

    for kunci in ("tautan", "baru", "relevan", "duplikat"):
        stat[kunci] = sum(s.get(kunci, 0) for s in stat["per_sumber"].values())
    p.catat(f"LOCAL LLM — {stat['baru']} baru, {stat['relevan']} relevan, "
            f"{stat['duplikat']} duplikat")


def _nilai(con, stat: dict, p: Progres) -> None:
    # 4. ranking
    p.tahap("ranking")
    ph = config.profil_hash()
    db.catat_versi_profil(con, ph)
    kandidat = db.kandidat_belum_dinilai(con, ph)
    stat["dinilai"] = stat["rekomendasi"] = 0
    if not kandidat:
        p.catat("RANKING — tidak ada kandidat baru")
        return
    masalah = rank.siap()
    if masalah:
        stat.setdefault("error", []).append(f"ranking dilewati: {masalah}")
        p.catat(f"RANKING dilewati — {masalah}", "warn")
        return
    model = rank.nama_model()
    ambang = config.settings()["large_llm"]["ambang_skor"]
    p.catat(f"RANKING {model} — {len(kandidat)} kandidat")
    for pn in rank.nilai(kandidat, stat):
        db.simpan_penilaian(con, pn, model, ph, ambang)
        stat["dinilai"] += 1
        stat["rekomendasi"] += int(pn.skor >= ambang)
    con.commit()
    for err in stat.get("error", []):
        if err.startswith("rank"):
            p.catat(f"  ! {err[:200]}", "warn")
    p.catat(f"RANKING — {stat['dinilai']} dinilai, {stat['rekomendasi']} di atas ambang {ambang}")


def _kirim(con, stat: dict, p: Progres, tanpa_kirim: bool) -> None:
    hasil = db.belum_dikirim(con, config.profil_hash())
    stat["lolos"] = len(hasil)
    if tanpa_kirim or not config.env("TELEGRAM_BOT_TOKEN"):
        return
    from notifier import telegram_bot
    try:
        n = telegram_bot.kirim_digest(hasil, stat)
        db.tandai(con, [r["id"] for r in hasil[:n]], "dikirim")
        stat["dikirim"] = n
        p.catat(f"NOTIFY — {n} lowongan terkirim ke Telegram")
    except Exception as ex:  # noqa: BLE001
        stat.setdefault("error", []).append(f"telegram: {ex}")
        p.catat(f"NOTIFY gagal — {ex}", "warn")
