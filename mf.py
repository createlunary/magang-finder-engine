"""Magang Finder — CLI.

    python mf.py run                       # satu run penuh (dipakai scheduler)
    python mf.py run --sumber glints --interaktif
    python mf.py uji-sumber jobstreet      # cek adapter tanpa menulis ke DB
    python mf.py daftar                    # hasil yang lolos ambang
    python mf.py tandai 12 15 dilamar      # ubah status lowongan
    python mf.py cari "backend golang remote"
    python mf.py statistik                 # metrik dari log run
    python mf.py telegram-id               # cari chat id setelah kirim /start ke bot
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

from pipeline import config, db

STATUS = ["baru", "dikirim", "dilihat", "dilamar", "ditolak", "diterima", "arsip"]


def _logging() -> None:
    folder = config.path("log")
    folder.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(folder / f"run-{datetime.now():%Y%m%d}.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)   # satu baris per panggilan Ollama terlalu ramai


def cmd_run(a) -> int:
    from pipeline.run import jalankan

    _logging()
    stat = jalankan(a.sumber.split(",") if a.sumber else None, interaktif=a.interaktif,
                    tanpa_rank=a.tanpa_rank, tanpa_kirim=a.tanpa_kirim,
                    tanpa_kumpul=a.hanya_rank)
    ringkas = {k: v for k, v in stat.items() if k != "per_sumber"}
    print(json.dumps(ringkas, ensure_ascii=False, indent=1))
    for nama, s in stat["per_sumber"].items():
        print(f"  {nama:10} tautan={s.get('tautan', 0):3} baru={s.get('baru', 0):3} "
              f"relevan={s.get('relevan', 0):3} error={len(s.get('error', []))} {s.get('detik', 0)}s")
    _sinkron_showcase("run selesai")
    return 0


def _sinkron_showcase(pesan: str) -> None:
    """Perbarui situs showcase publik (lihat api/showcase.py). Gagal tidak menggagalkan perintah."""
    from api import showcase
    logging.getLogger().info(showcase.sinkron(pesan))


def cmd_cabut_sesi(a) -> int:
    """Keluarkan semua perangkat dari panel kontrol (HP hilang, token dicurigai bocor)."""
    from api import auth
    auth.cabut_semua()
    print("semua sesi panel dicabut — setiap perangkat harus login ulang dengan Google")
    return 0


def cmd_showcase(a) -> int:
    from api import showcase

    if a.ekspor:
        from pathlib import Path
        Path(a.ekspor).write_text(json.dumps(showcase.bangun(), ensure_ascii=False), encoding="utf-8")
        print(f"snapshot ditulis ke {a.ekspor}")
        return 0
    if a.lihat:
        isi = showcase.bangun()
        print(json.dumps({k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in isi.items()},
                         ensure_ascii=False, indent=1))
        return 0
    _logging()
    _sinkron_showcase("sinkron manual")
    return 0


def cmd_uji_sumber(a) -> int:
    from pipeline.sumber import connector, daftar

    cfg = next((s for s in config.sumber() if s["nama"] == a.nama), None)
    if not cfg:
        print(f"sumber '{a.nama}' tidak ada di config/sumber_situs.yaml")
        return 2
    if cfg["jenis"] == "connector":
        from pipeline.local_extract import POLA_MAGANG
        pertama: tuple[str, dict] | None = None
        pencarian = connector.daftar_pencarian(cfg)
        for url, konteks in (pencarian if a.semua else pencarian[:1]):
            md = connector.ambil(url, selector=cfg.get("selector_daftar"), tunggu=cfg.get("tunggu"),
                                 interaktif=a.interaktif)
            semua = connector.tautan_detail(md, url, cfg["pola_detail"])
            links = (semua if konteks.get("jenis") == "magang"
                     else connector.tautan_detail(md, url, cfg["pola_detail"], POLA_MAGANG))
            print(f"{len(semua):3} tautan, {len(links):3} diambil  {konteks or ''}\n    {url}")
            if links and not pertama:
                pertama = (links[0], konteks)
        if not (a.detail and pertama):
            return 0 if pertama else 1
        from pipeline.models import LowonganMentah
        teks, jp = connector.pisah_jsonld(connector.ambil(
            pertama[0], selector=cfg.get("selector_detail"), interaktif=a.interaktif, jsonld=True))
        extra = {"konteks": pertama[1]} if pertama[1] else {}
        if jp:
            extra["jobposting"] = jp
        m = LowonganMentah(sumber=cfg["nama"], url=pertama[0], teks=teks, extra=extra)
        print("JSON-LD JobPosting:", {k: jp.get(k) for k in ("employmentType", "validThrough")}
              if jp else "tidak ada")
    else:
        stat: dict = {}
        hasil = daftar(cfg, stat)
        print(f"{len(hasil)} lowongan, stat: {stat}")
        if not hasil:
            return 1
        m = hasil[0][2]()
    print(f"\ndetail: {m.url} ({len(m.teks):,} karakter)")
    if a.detail:
        from pipeline import local_extract
        e = local_extract.ekstrak(m)
        print(e.model_dump_json(indent=1))
        print("lolos filter kasar:", local_extract.lolos_filter_kasar(e))
    return 0


def cmd_saring_ulang(a) -> int:
    """Terapkan ulang aturan deterministik ke lowongan tersimpan, tanpa LLM/jaringan."""
    from pipeline.local_extract import _KOSONG, _LABEL_PENGATURAN, terlihat_magang

    hanya_magang = config.settings()["local_llm"]["hanya_magang"]
    berubah = 0
    with db.koneksi() as con:
        for r in con.execute("SELECT id, judul, perusahaan, lokasi, tipe_kerja, jenis, relevan, "
                             "teks_mentah FROM lowongan").fetchall():
            perusahaan = "" if (r["perusahaan"] or "").lower() in _KOSONG else r["perusahaan"]
            lokasi = "" if (r["lokasi"] or "").lower() in _KOSONG else r["lokasi"]
            label = _LABEL_PENGATURAN.get((r["lokasi"] or "").strip().lower())
            if label:
                lokasi = ""
                if r["tipe_kerja"] == "tidak_disebut":
                    con.execute("UPDATE lowongan SET tipe_kerja=? WHERE id=?", (label, r["id"]))
            jenis = r["jenis"]
            if jenis == "magang" and not terlihat_magang(r["judul"], r["teks_mentah"]):
                jenis = "lainnya"
            relevan = int(bool(r["relevan"]) and (jenis == "magang" or not hanya_magang))
            baru = (perusahaan, lokasi, jenis, relevan)
            if baru != (r["perusahaan"], r["lokasi"], r["jenis"], r["relevan"]):
                con.execute("UPDATE lowongan SET perusahaan=?, lokasi=?, jenis=?, relevan=?, "
                            "sidik_jari = CASE WHEN ? = '' THEN NULL ELSE sidik_jari END WHERE id=?",
                            (*baru, perusahaan, r["id"]))
                berubah += 1
        n = con.execute("SELECT COUNT(*) FROM lowongan WHERE relevan = 1").fetchone()[0]
    print(f"{berubah} lowongan diperbarui; kandidat relevan sekarang: {n}")
    return 0


def cmd_daftar(a) -> int:
    with db.koneksi() as con:
        rows = con.execute(
            """SELECT l.id, l.status, p.skor, l.judul, l.perusahaan, l.lokasi, l.sumber, l.url
               FROM lowongan l JOIN penilaian p ON p.lowongan_id = l.id AND p.profil_hash = ?
               WHERE p.skor >= ? AND (? IS NULL OR l.status = ?)
               ORDER BY p.skor DESC LIMIT ?""",
            (config.profil_hash(), a.min_skor, a.status, a.status, a.n)).fetchall()
    for r in rows:
        print(f"#{r['id']:<5} {r['skor']:>3} {r['status']:8} {r['judul'][:45]:45} "
              f"{(r['perusahaan'] or '')[:25]:25} {r['sumber']}")
        if a.url:
            print(f"       {r['url']}")
    return 0


def cmd_tandai(a) -> int:
    with db.koneksi() as con:
        db.tandai(con, a.id, a.status)
    print(f"{len(a.id)} lowongan → {a.status}")
    return 0


def cmd_cari(a) -> int:
    from rag import store
    for h in store.cari(a.query, a.n):
        print(f"#{h['id']:<5} {h['skor']:.3f} {h['judul'][:50]:50} {h['perusahaan'][:25]:25} {h['sumber']}")
    return 0


def cmd_statistik(a) -> int:
    with db.koneksi() as con:
        runs = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (a.n,)).fetchall()
        total = con.execute(
            """SELECT COUNT(*) n, SUM(relevan) relevan,
                      SUM(duplikat_dari IS NOT NULL) duplikat,
                      SUM(status IN ('dilamar','diterima','ditolak')) dilamar
               FROM lowongan""").fetchone()
    print(f"total lowongan: {total['n']}  relevan: {total['relevan']}  "
          f"duplikat: {total['duplikat']}  dilamar: {total['dilamar']}")
    for r in runs:
        s = json.loads(r["statistik"] or "{}")
        print(f"run #{r['id']} {r['mulai']}  tautan={s.get('tautan', 0)} baru={s.get('baru', 0)} "
              f"relevan={s.get('relevan', 0)} dinilai={s.get('dinilai', 0)} lolos={s.get('lolos', 0)} "
              f"token={s.get('token', {})} {s.get('detik', '?')}s")
    return 0


def cmd_telegram_id(a) -> int:
    from notifier import telegram_bot
    chats = telegram_bot.cari_chat_id()
    if not chats:
        print("Belum ada pesan. Buka bot-mu di Telegram, kirim /start, lalu jalankan lagi.")
        return 1
    for c in chats:
        print(f"TELEGRAM_CHAT_ID={c['chat_id']}   ({c['nama']})")
    return 0


def _diagnosa_claude(rank) -> int:
    """Tambahkan flag satu per satu untuk menemukan langkah yang menggantung."""
    import json as _json
    import time

    exe = rank.claude_exe()
    skema = _json.dumps({"type": "object", "properties": {"jawab": {"type": "string"}},
                         "required": ["jawab"], "additionalProperties": False})
    langkah = [
        ("versi CLI", [exe, "--version"], None, 30),
        ("prompt paling sederhana", [exe, "-p", "Balas satu kata: ok"], None, 120),
        ("+ prompt lewat stdin", [exe, "-p"], "Balas satu kata: ok", 120),
        ("+ output json, tanpa sesi", [exe, "-p", "--output-format", "json",
                                       "--no-session-persistence"], "Balas satu kata: ok", 120),
        ("+ semua tool dimatikan", [exe, "-p", "--output-format", "json", "--no-session-persistence",
                                    "--tools", ""], "Balas satu kata: ok", 120),
        ("+ model opus", [exe, "-p", "--output-format", "json", "--no-session-persistence",
                          "--tools", "", "--model", "opus"], "Balas satu kata: ok", 120),
        ("+ json-schema", [exe, "-p", "--output-format", "json", "--no-session-persistence",
                           "--tools", "", "--model", "opus", "--json-schema", skema],
         "Isi field jawab dengan: ok", 120),
    ]
    for nama, argv, masukan, batas in langkah:
        print(f"[..] {nama}", end="", flush=True)
        t = time.time()
        try:
            p = rank.jalankan(argv, masukan, batas)
        except TimeoutError as e:
            print(f"\r[MACET] {nama} — {e}")
            print("\nLangkah di atas yang menggantung. Kirim tangkapan layar ini ke Claude.")
            return 1
        keluar = (p.stdout or p.stderr).strip().replace("\n", " ")
        tanda = "OK" if p.returncode == 0 else f"exit {p.returncode}"
        print(f"\r[{tanda}] {nama} ({time.time() - t:.0f}s): {keluar[:160]}")
        if p.returncode != 0:
            return 1
    print("\nSemua langkah lolos.")
    return 0


def cmd_lengkapi_perusahaan(a) -> int:
    """Isi profil_perusahaan untuk lowongan relevan lama dengan Qwen lokal (tanpa kuota Claude)."""
    from pipeline import local_extract

    with db.koneksi() as con:
        rows = con.execute("SELECT id, perusahaan, teks_mentah FROM lowongan WHERE relevan = 1 "
                           "AND duplikat_dari IS NULL AND profil_perusahaan IS NULL").fetchall()
    print(f"{len(rows)} lowongan belum punya profil perusahaan")
    for i, r in enumerate(rows, 1):
        try:
            profil = local_extract.ringkas_perusahaan(r["perusahaan"] or "", r["teks_mentah"] or "")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] #{r['id']} gagal: {e}")
            continue
        with db.koneksi() as con:
            con.execute("UPDATE lowongan SET profil_perusahaan = ? WHERE id = ?", (profil, r["id"]))
            con.commit()
        print(f"  [{i}/{len(rows)}] {r['perusahaan']}: {profil[:90] or '(halaman tidak menjelaskan)'}")
    return 0


def cmd_riset(a) -> int:
    """Deep Search: riset kredibilitas perusahaan pemberi lowongan."""
    from pipeline import riset

    _logging()
    try:
        riset.riset(a.id)
    except Exception as e:  # noqa: BLE001 — status gagal sudah tercatat di DB
        print(f"riset gagal: {e}")
        return 1
    with db.koneksi() as con:
        r = con.execute("SELECT perusahaan FROM lowongan WHERE id = ?", (a.id,)).fetchone()
        hasil = db.baca_riset(con, riset.kunci_perusahaan(r["perusahaan"]))
    lap = json.loads(hasil["laporan"])
    print(f"{lap['nama_resmi']}: kredibilitas {lap['skor_kredibilitas']} ({lap['tingkat']})")
    print(lap["ringkasan"])
    for f in lap["fakta"]:
        print(f"  - {f['label']}: {f['nilai']}")
    print(f"{len(lap['sumber'])} sumber · {hasil['statistik']}")
    _sinkron_showcase(f"deep search {lap['nama_resmi']}")
    return 0


def cmd_tes_claude(a) -> int:
    """Nilai satu lowongan contoh lewat backend yang aktif — cek login & format keluaran."""
    import time

    from pipeline import rank

    masalah = rank.siap()
    if masalah:
        print(f"backend belum siap: {masalah}")
        return 1
    if a.diagnosa:
        return _diagnosa_claude(rank)
    contoh = {"id": 1, "judul": "Backend Engineer Intern", "perusahaan": "PT Contoh Teknologi",
              "lokasi": "Yogyakarta (hybrid)", "tipe_kerja": "hybrid", "durasi_bulan": 6,
              "kompensasi": "berbayar", "gaji": "Rp2.500.000/bulan", "deadline": None,
              "bidang": '["backend"]', "requirement": '["Python", "REST API", "SQL"]',
              "deskripsi": "Membangun API internal bersama mentor senior.", "sumber": "contoh"}
    print(f"menilai 1 lowongan contoh dengan {rank.nama_model()} …")
    stat: dict = {}
    t = time.time()
    hasil = rank.nilai([contoh], stat)
    print(f"{time.time() - t:.0f} detik, token={stat.get('token')}, "
          f"biaya setara=${stat.get('biaya_setara_usd', 0)}")
    for p in hasil:
        print(p.model_dump_json(indent=1))
    for e in stat.get("error", []):
        print("ERROR:", e)
    return 0 if hasil else 1


def cmd_tes_telegram(a) -> int:
    from notifier import telegram_bot
    telegram_bot.kirim_teks("✅ Magang Finder terhubung.")
    print("terkirim")
    return 0


def main() -> int:
    if sys.stdout is None:
        # Dijalankan lewat pythonw.exe (run terjadwal tanpa jendela): tidak ada konsol.
        # Semua yang penting sudah tertulis ke logs/run-*.log dan logs/progres.json.
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="mf", description="Magang Finder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="satu run penuh")
    p.add_argument("--sumber", help="nama dipisah koma; default semua yang aktif")
    p.add_argument("--interaktif", action="store_true", help="izinkan panel login connector")
    p.add_argument("--tanpa-rank", action="store_true")
    p.add_argument("--tanpa-kirim", action="store_true")
    p.add_argument("--hanya-rank", action="store_true",
                   help="lewati pengumpulan; nilai kandidat yang sudah ada lalu kirim")
    p.set_defaults(f=cmd_run)

    p = sub.add_parser("uji-sumber", help="uji satu adapter tanpa menulis ke DB")
    p.add_argument("nama")
    p.add_argument("--detail", action="store_true", help="ambil 1 detail + ekstraksi LLM lokal")
    p.add_argument("--semua", action="store_true", help="uji semua pencarian, bukan hanya yang pertama")
    p.add_argument("--interaktif", action="store_true")
    p.set_defaults(f=cmd_uji_sumber)

    sub.add_parser("saring-ulang", help="terapkan ulang aturan filter ke data tersimpan"
                   ).set_defaults(f=cmd_saring_ulang)

    p = sub.add_parser("daftar", help="lowongan yang sudah dinilai")
    p.add_argument("--min-skor", type=int, default=0)
    p.add_argument("--status", choices=STATUS)
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--url", action="store_true")
    p.set_defaults(f=cmd_daftar)

    p = sub.add_parser("tandai", help="ubah status lowongan")
    p.add_argument("id", type=int, nargs="+")
    p.add_argument("status", choices=STATUS)
    p.set_defaults(f=cmd_tandai)

    p = sub.add_parser("cari", help="pencarian semantik atas lowongan yang pernah dilihat")
    p.add_argument("query")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(f=cmd_cari)

    p = sub.add_parser("statistik", help="metrik run")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(f=cmd_statistik)

    sub.add_parser("telegram-id").set_defaults(f=cmd_telegram_id)
    sub.add_parser("tes-telegram").set_defaults(f=cmd_tes_telegram)

    sub.add_parser("lengkapi-perusahaan", help="ringkas profil perusahaan lowongan lama (Qwen lokal)"
                   ).set_defaults(f=cmd_lengkapi_perusahaan)

    p = sub.add_parser("riset", help="Deep Search: riset kredibilitas perusahaan sebuah lowongan")
    p.add_argument("id", type=int, help="id lowongan (#id di website/Telegram)")
    p.set_defaults(f=cmd_riset)
    sub.add_parser("cabut-sesi", help="keluarkan semua perangkat dari panel kontrol").set_defaults(f=cmd_cabut_sesi)
    p = sub.add_parser("showcase", help="ekspor data publik ke situs showcase lalu push")
    p.add_argument("--lihat", action="store_true", help="tampilkan ringkasan isi snapshot tanpa menulis")
    p.add_argument("--ekspor", metavar="FILE", help="tulis snapshot ke FILE saja (tanpa git), mis. untuk pratinjau")
    p.set_defaults(f=cmd_showcase)
    p = sub.add_parser("tes-claude", help="uji backend ranking dengan 1 lowongan contoh")
    p.add_argument("--diagnosa", action="store_true",
                   help="uji flag CLI satu per satu untuk menemukan yang menggantung")
    p.set_defaults(f=cmd_tes_claude)

    a = ap.parse_args()
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
