"""FastAPI untuk website Magang Finder.

Kontrak endpoint mengikuti README frontend (magang-finder-web). Jalankan:

    .venv\\Scripts\\python -m uvicorn api.main:app --port 8000

Run pipeline tidak dijalankan di dalam proses API: POST /runs memulai
`mf.py run` sebagai proses terpisah, lalu progresnya dibaca dari
logs/progres.json — sama seperti run terjadwal.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pipeline import config, db, progres

from . import auth, konfig

app = FastAPI(title="Magang Finder API", version="0.1.0")
# Urutan penting: middleware yang ditambahkan terakhir membungkus paling luar. CORS harus
# di luar Penjaga, supaya penolakan 401/403 tetap membawa header CORS dan terbaca browser.
app.add_middleware(auth.Penjaga)
app.add_middleware(
    CORSMiddleware,
    # Panel kontrol dibuka dari komputer ini atau perangkat lain di WiFi yang sama.
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+"
                       r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?",
    allow_methods=["*"], allow_headers=["*"],
)

StatusUI = Literal["baru", "dilamar", "diterima", "ditolak"]
# Status di DB yang lebih rinci daripada di UI; "dikirim" dipakai notifikasi Telegram.
STATUS_KE_UI = {"baru": "baru", "dikirim": "baru", "dilihat": "baru",
                "dilamar": "dilamar", "diterima": "diterima", "ditolak": "ditolak"}

# Kosakata untuk tag teknologi di kartu lowongan.
TEKNOLOGI = ["Python", "JavaScript", "TypeScript", "Node.js", "React", "Next.js", "Vue", "Nuxt",
             "Angular", "Laravel", "PHP", "Go", "Golang", "Java", "Kotlin", "Swift", "Flutter",
             "Dart", "C++", "C#", ".NET", "Unity", "Unreal Engine", "Docker", "Kubernetes", "SQL",
             "MySQL", "PostgreSQL", "MongoDB", "Redis", "AWS", "GCP", "Azure", "Linux", "Git",
             "PyTorch", "TensorFlow", "Machine Learning", "Deep Learning", "NLP", "LLM",
             "Data Science", "FastAPI", "Django", "Express", "Spring", "REST API", "GraphQL",
             "Figma", "QA", "DevOps", "CI/CD", "Roblox", "Lua"]
_POLA_TEK = [(t, re.compile(rf"(?<![\w.]){re.escape(t)}(?![\w])", re.I)) for t in TEKNOLOGI]


def _iso(ts: str | None) -> str:
    """Waktu lokal tanpa zona di DB → ISO dengan offset, supaya browser tidak salah geser."""
    if not ts:
        return ""
    return datetime.fromisoformat(ts).astimezone().isoformat(timespec="seconds")


# -------------------------------------------------------------------- jobs

def _tags(r) -> list[str]:
    teks = " ".join([r["judul"] or "", r["requirement"] or "", r["deskripsi"] or "",
                     (r["teks_mentah"] or "")[:4000]])
    tags = [t for t, pola in _POLA_TEK if pola.search(teks)]
    if "Golang" in tags and "Go" in tags:
        tags.remove("Go")
    return tags[:6]


def _lokasi(r) -> str:
    tipe, lok = r["tipe_kerja"], r["lokasi"] or ""
    if tipe == "remote":
        return "Remote"
    if tipe == "hybrid" and lok:
        return f"{lok} (hybrid)"
    return lok or "—"


def _job(con, r) -> dict:
    from pipeline.riset import kunci_perusahaan
    must = set(json.loads(r["must_have"] or "[]"))
    riwayat = con.execute(
        """SELECT p.skor, p.dinilai_pada, v.versi FROM penilaian p
           LEFT JOIN profil_versi v ON v.profil_hash = p.profil_hash
           WHERE p.lowongan_id = ? ORDER BY p.id""", (r["id"],)).fetchall()
    return {
        "id": str(r["id"]), "role": r["judul"] or "—", "company": r["perusahaan"] or "—",
        "companyKey": kunci_perusahaan(r["perusahaan"] or ""),
        "source": r["sumber"], "lokasi": _lokasi(r), "status": STATUS_KE_UI.get(r["status"], "baru"),
        "score": r["skor"], "deadline": r["deadline"] or "", "foundAt": _iso(r["ditemukan_pada"]),
        "tags": _tags(r), "reasoning": r["alasan"] or "",
        "mustHave": [{"label": m, "met": m in must} for m in config.profil().get("must_have", [])],
        "redFlags": json.loads(r["red_flags"] or "[]"),
        "scoreHistory": [{"version": f"v{h['versi'] or '?'}", "date": h["dinilai_pada"][:10],
                          "score": h["skor"]} for h in riwayat],
        "notes": r["catatan"] or "", "url": r["url"],
    }


# Penilaian TERBARU tiap lowongan, apa pun versi profilnya: setelah profil
# diubah, skor lama tetap tampil sampai penilaian ulang selesai.
SQL_JOBS = """
SELECT l.*, p.skor, p.alasan, p.red_flags, p.must_have FROM lowongan l
JOIN penilaian p ON p.id = (SELECT MAX(id) FROM penilaian WHERE lowongan_id = l.id)
WHERE l.duplikat_dari IS NULL AND l.status != 'arsip'
"""


def _cocok_semantik(q: str) -> set[int] | None:
    """id lowongan yang maknanya dekat dengan q; None kalau embedding tidak tersedia."""
    try:
        from rag import store
        hasil = store.cari(q, 60)
    except Exception:  # noqa: BLE001 — Ollama mati: jatuh ke pencocokan kata
        return None
    if not hasil:
        return set()
    # Kemiripan bge-m3 antar lowongan sangat rapat (≈0.40–0.63), jadi ambang
    # absolut meloloskan hampir semuanya. Ambil hanya yang dekat dengan hasil terbaik.
    batas = max(0.45, hasil[0]["skor"] - 0.06)
    return {h["id"] for h in hasil if h["skor"] >= batas}


@app.get("/jobs")
def list_jobs(q: str = "", sources: str = "", status: str = "", min_score: int = 0,
              sort: Literal["skor", "deadline"] = "skor"):
    with db.koneksi() as con:
        rows = con.execute(SQL_JOBS).fetchall()
        if sources:
            pilih = set(sources.split(","))
            rows = [r for r in rows if r["sumber"] in pilih]
        if status:
            rows = [r for r in rows if STATUS_KE_UI.get(r["status"]) == status]
        if min_score:
            rows = [r for r in rows if r["skor"] >= min_score]
        if q.strip():
            kata = q.lower().split()
            semantik = _cocok_semantik(q) or set()

            def cocok_kata(r) -> bool:
                # Bukan `alasan`: alasan Claude menyebut minat profil ("minat backend")
                # di hampir setiap lowongan, jadi semua akan ikut cocok.
                hay = " ".join(str(r[k] or "") for k in
                               ("judul", "perusahaan", "lokasi", "requirement", "deskripsi")).lower()
                return all(k in hay for k in kata)
            rows = [r for r in rows if r["id"] in semantik or cocok_kata(r)]
        jobs = [_job(con, r) for r in rows]
    if sort == "deadline":
        jobs.sort(key=lambda j: (j["deadline"] == "", j["deadline"]))
    else:
        jobs.sort(key=lambda j: -j["score"])
    return jobs


@app.get("/companies")
def list_companies():
    """Perusahaan di balik lowongan yang sudah dinilai, dengan latar belakang singkat.

    Latar belakang diambil dari laporan Deep Search bila ada (terverifikasi web),
    kalau tidak dari ringkasan Qwen atas halaman lowongannya.
    """
    from collections import Counter

    from pipeline.riset import kunci_perusahaan

    grup: dict[str, list] = {}
    with db.koneksi() as con:
        for r in con.execute(SQL_JOBS).fetchall():
            k = kunci_perusahaan(r["perusahaan"] or "")
            if k:
                grup.setdefault(k, []).append(r)
        riset = {r["kunci"]: r for r in con.execute("SELECT * FROM riset_perusahaan WHERE laporan IS NOT NULL")}

    hasil = []
    for k, rows in grup.items():
        lap = json.loads(riset[k]["laporan"]) if k in riset else None
        profil = next((r["profil_perusahaan"] for r in rows if r["profil_perusahaan"]), "")
        skor = [r["skor"] for r in rows]
        hasil.append({
            "key": k,
            "name": Counter(r["perusahaan"] for r in rows).most_common(1)[0][0],
            "jobIds": [str(r["id"]) for r in sorted(rows, key=lambda r: -r["skor"])],
            "bestScore": max(skor), "avgScore": round(sum(skor) / len(skor)),
            "locations": sorted({_lokasi(r) for r in rows}),
            "sources": sorted({r["sumber"] for r in rows}),
            "blurb": lap["ringkasan"] if lap else profil,
            "blurbSource": "deep-search" if lap else ("lowongan" if profil else None),
            "credibility": {"score": lap["skor_kredibilitas"], "level": lap["tingkat"]} if lap else None,
        })
    return sorted(hasil, key=lambda c: -c["bestScore"])


def _satu(con, job_id: str):
    r = con.execute(SQL_JOBS + " AND l.id = ?", (job_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Lowongan tidak ditemukan")
    return r


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db.koneksi() as con:
        return _job(con, _satu(con, job_id))


class JobPatch(BaseModel):
    status: StatusUI | None = None
    notes: str | None = Field(None, max_length=5000)


@app.patch("/jobs/{job_id}")
def update_job(job_id: str, patch: JobPatch):
    with db.koneksi() as con:
        r = _satu(con, job_id)
        if patch.status is not None and STATUS_KE_UI.get(r["status"]) != patch.status:
            con.execute("UPDATE lowongan SET status = ? WHERE id = ?", (patch.status, r["id"]))
        if patch.notes is not None:
            con.execute("UPDATE lowongan SET catatan = ? WHERE id = ?", (patch.notes, r["id"]))
        con.commit()
        return _job(con, _satu(con, job_id))


# -------------------------------------------------------------- deep search

def _laporan_out(lap: dict) -> dict:
    """LaporanPerusahaan (Python, bahasa Indonesia) → `CompanyReport` (frontend, camelCase)."""
    return {
        "officialName": lap["nama_resmi"], "summary": lap["ringkasan"],
        "facts": [{"label": f["label"], "value": f["nilai"], "sources": f.get("sumber", [])}
                  for f in lap["fakta"]],
        "digitalFootprint": lap["jejak_digital"], "employeeReviews": lap.get("ulasan_karyawan"),
        "news": lap.get("berita", []), "positiveSignals": lap["sinyal_positif"],
        "negativeSignals": lap["sinyal_negatif"], "credibilityScore": lap["skor_kredibilitas"],
        "level": lap["tingkat"], "scoreReason": lap["alasan_skor"],
        "manualChecks": lap["perlu_dicek_manual"],
        "sources": [{"title": s["judul"], "url": s["url"]} for s in lap["sumber"]],
    }


def _riset_out(r) -> dict:
    """Status riset perusahaan untuk frontend (`CompanyResearch`)."""
    if r is None:
        return {"status": "belum"}
    status = r["status"]
    if status == "berjalan" and not progres.proses_hidup(r["pid"] or -1):
        status = "gagal"                              # prosesnya mati tanpa sempat mencatat
    stat = json.loads(r["statistik"] or "{}")
    return {
        "status": status,
        "company": r["nama"],
        "startedAt": _iso(r["mulai"]),
        "finishedAt": _iso(r["selesai"]),
        "error": r["error"] or ("proses riset berhenti tanpa selesai" if status != r["status"] else None),
        "report": _laporan_out(json.loads(r["laporan"])) if r["laporan"] else None,
        "turns": stat.get("putaran"),
    }


@app.get("/jobs/{job_id}/research")
def get_research(job_id: str):
    from pipeline import riset
    with db.koneksi() as con:
        r = _satu(con, job_id)
        return _riset_out(db.baca_riset(con, riset.kunci_perusahaan(r["perusahaan"] or "")))


@app.post("/jobs/{job_id}/research")
def start_research(job_id: str):
    from pipeline import riset
    with db.koneksi() as con:
        r = _satu(con, job_id)
        kunci = riset.kunci_perusahaan(r["perusahaan"] or "")
        if not kunci:
            raise HTTPException(422, "Lowongan ini tidak mencantumkan nama perusahaan")
        sekarang = _riset_out(db.baca_riset(con, kunci))
    if sekarang["status"] == "berjalan":
        return sekarang                               # sudah diriset (mungkin dari lowongan lain PT yang sama)
    anak = _mulai_proses(["riset", str(r["id"])], f"deep search {r['perusahaan']}", "riset.log")
    with db.koneksi() as con:
        # Tandai berjalan sekarang juga, supaya polling pertama tidak melihat status lama.
        db.mulai_riset(con, kunci, r["perusahaan"], r["id"], pid=anak.pid)
        con.commit()
        return _riset_out(db.baca_riset(con, kunci))


# --------------------------------------------------------------------- chat

def _pesan_out(m) -> dict:
    return {"id": str(m["id"]), "role": m["peran"], "content": m["isi"],
            "createdAt": _iso(m["dibuat_pada"]), "activity": json.loads(m["aktivitas"] or "[]")}


@app.get("/jobs/{job_id}/chat")
def get_chat(job_id: str):
    with db.koneksi() as con:
        r = _satu(con, job_id)
        rows = con.execute("SELECT * FROM chat_pesan WHERE lowongan_id = ? ORDER BY id", (r["id"],)).fetchall()
    return [_pesan_out(m) for m in rows]


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@app.post("/jobs/{job_id}/chat")
def post_chat(job_id: str, body: ChatIn):
    from fastapi.responses import StreamingResponse

    from pipeline import chat

    with db.koneksi() as con:
        lid = _satu(con, job_id)["id"]

    def alir():
        for ev in chat.tanya(lid, body.message.strip()):
            if ev["type"] == "done":
                ev = {"type": "done", "message": _pesan_out(ev["message"])}
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    # Jawaban dialirkan kata per kata; pemutusan koneksi dari browser menghentikan proses Claude.
    return StreamingResponse(alir(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.delete("/jobs/{job_id}/chat")
def clear_chat(job_id: str):
    with db.koneksi() as con:
        r = _satu(con, job_id)
        con.execute("DELETE FROM chat_pesan WHERE lowongan_id = ?", (r["id"],))
        con.commit()
    return {"ok": True}


# ------------------------------------------------------------------ profile

class ProfileIn(BaseModel):
    bidang: list[str]
    skills: list[str]
    skillsFamiliar: list[str] = []
    lokasi: list[str]
    mustHave: list[str]
    redFlags: list[str]


def _profile_out() -> dict:
    with db.koneksi() as con:
        versi = db.catat_versi_profil(con, config.profil_hash())
        con.commit()
        waktu = con.execute("SELECT disimpan_pada FROM profil_versi WHERE versi = ?",
                            (versi,)).fetchone()[0]
    return {"version": versi, "updatedAt": _iso(waktu), **konfig.baca_profil()}


@app.get("/profile")
def get_profile():
    return _profile_out()


@app.put("/profile")
def put_profile(p: ProfileIn):
    lama = config.profil_hash()
    konfig.simpan_profil(p.model_dump())
    if config.profil_hash() != lama and not progres.baca().get("running"):
        # Profil berubah → skor lama basi; nilai ulang di latar belakang.
        _mulai_run(["--hanya-rank", "--tanpa-kirim"], "penilaian ulang setelah profil diubah")
    return _profile_out()


# ------------------------------------------------------------------ sources

def _status_sumber() -> dict[str, tuple[dict, str]]:
    """Statistik per sumber dari run terbaru yang menyentuh sumber itu."""
    hasil: dict[str, tuple[dict, str]] = {}
    with db.koneksi() as con:
        for r in con.execute("SELECT mulai, statistik FROM runs WHERE statistik IS NOT NULL "
                             "ORDER BY id DESC LIMIT 60"):
            stat = json.loads(r["statistik"])
            if "fatal" in stat:
                continue          # run dibatalkan/gagal: angkanya setengah jadi, bukan gambaran sumber
            for nama, s in stat.get("per_sumber", {}).items():
                hasil.setdefault(nama, (s, _iso(r["mulai"])))
    return hasil


@app.get("/sources")
def list_sources():
    status = _status_sumber()
    return [konfig.ke_source(c, status.get(c["nama"])) for c in konfig.konfig_sumber()]


class SourcePatch(BaseModel):
    enabled: bool | None = None
    keywords: str | None = None
    targetedQuery: str | None = None


@app.patch("/sources/{key}")
def update_source(key: str, patch: SourcePatch):
    try:
        cfg = konfig.ubah_sumber(key, patch.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(404, "Sumber tidak ditemukan") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return konfig.ke_source(cfg, _status_sumber().get(key))


# ----------------------------------------------------------------- settings

class SettingsIn(BaseModel):
    threshold: int
    backend: Literal["claude-code", "claude-api"]
    model: Literal["haiku", "sonnet", "opus"]
    pagesPerSource: int
    schedule: str


@app.get("/settings")
def get_settings():
    return konfig.baca_settings()


@app.put("/settings")
def put_settings(s: SettingsIn):
    lama = konfig.baca_settings()
    try:
        hasil = konfig.simpan_settings(s.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if hasil["schedule"] != lama["schedule"]:
        from . import jadwal
        jadwal.perbarui(hasil["schedule"])
    return hasil


# --------------------------------------------------------------------- runs

def _run_record(con, r, aktif: dict) -> dict | None:
    s = json.loads(r["statistik"] or "{}")
    berjalan = r["selesai"] is None
    if berjalan and aktif.get("running") and aktif.get("run_id") == r["id"]:
        return None                                   # run aktif ditampilkan lewat /runs/current
    gagal = "fatal" in s or berjalan
    tok = s.get("token", {})
    rekomendasi = s.get("rekomendasi")
    if rekomendasi is None and r["selesai"]:
        # Run lama belum mencatat statistik ini; hitung dari penilaian di rentang waktunya.
        rekomendasi = con.execute(
            "SELECT COUNT(*) FROM penilaian WHERE lolos = 1 AND dinilai_pada BETWEEN ? AND ?",
            (r["mulai"], r["selesai"])).fetchone()[0]
    rec = {
        "id": f"r{r['id']}", "startedAt": _iso(r["mulai"]), "durationSec": int(s.get("detik", 0)),
        "found": s.get("baru", 0) + s.get("duplikat", 0), "passedLocal": s.get("relevan", 0),
        "recommended": rekomendasi or 0, "coverage": None, "precision": None,
        "tokens": int(sum(tok.values())), "status": "gagal" if gagal else "berhasil",
    }
    if gagal:
        rec["error"] = s.get("fatal", "run berhenti tanpa selesai")
    return rec


def _runs(batas: int = 100) -> list[dict]:
    aktif = progres.baca()
    with db.koneksi() as con:
        rows = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (batas,)).fetchall()
        return [x for x in (_run_record(con, r, aktif) for r in rows) if x]


@app.get("/runs")
def list_runs():
    return _runs()


def _mulai_run(argumen: list[str], alasan: str) -> None:
    anak = _mulai_proses(["run", *argumen], alasan, "web-run.log")
    # Tulis status sementara: tanpa ini, polling pertama frontend bisa melihat
    # "tidak berjalan" sebelum proses anak sempat menulis progresnya sendiri.
    progres._tulis({"running": True, "pid": anak.pid, "run_id": None, "stage": "scrape",
                    "stageIndex": 0, "log": [{"t": datetime.now().strftime("%H:%M:%S"),
                                              "msg": f"memulai: {alasan}", "level": "info"}]})


def _mulai_proses(argumen: list[str], alasan: str, nama_log: str) -> subprocess.Popen:
    """Jalankan `mf.py <argumen>` terlepas dari proses API."""
    folder = config.path("log")
    folder.mkdir(exist_ok=True)
    keluaran = open(folder / nama_log, "a", encoding="utf-8")  # noqa: SIM115 — dipegang proses anak
    keluaran.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} {alasan}\n")
    keluaran.flush()
    argv = [sys.executable, "mf.py", *argumen]
    opsi = dict(cwd=config.ROOT, stdout=keluaran, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if os.name == "nt":
        # Lepas dari job object proses API: tanpa ini, mematikan/me-restart server API
        # ikut membunuh run yang sedang berjalan. Beberapa host melarang breakaway,
        # jadi coba lagi tanpa flag itu.
        dasar = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            anak = subprocess.Popen(argv, creationflags=dasar | subprocess.CREATE_BREAKAWAY_FROM_JOB, **opsi)
        except OSError:
            anak = subprocess.Popen(argv, creationflags=dasar, **opsi)
    else:
        anak = subprocess.Popen(argv, start_new_session=True, **opsi)
    return anak


@app.post("/runs")
def start_run():
    if progres.baca().get("running"):
        raise HTTPException(409, "Run lain masih berjalan")
    _mulai_run([], "run manual dari website")
    return {"ok": True}


@app.delete("/runs/current")
def stop_run():
    """Hentikan run yang sedang berjalan beserta proses anaknya (connector, Chrome)."""
    p = progres.baca()
    if not p.get("running"):
        raise HTTPException(409, "Tidak ada run yang berjalan")
    pid = p["pid"]
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, 9)
    waktu = datetime.now()
    if p.get("run_id"):
        with db.koneksi() as con:
            r = con.execute("SELECT mulai, statistik FROM runs WHERE id = ?", (p["run_id"],)).fetchone()
            if r:
                s = json.loads(r["statistik"] or "{}")
                s.update(fatal="dibatalkan pengguna",
                         detik=round((waktu - datetime.fromisoformat(r["mulai"])).total_seconds()))
                db.selesai_run(con, p["run_id"], s)
                con.commit()
    p.update(running=False, stage=None, stageIndex=4)
    p["log"].append({"t": waktu.strftime("%H:%M:%S"), "msg": "RUN DIBATALKAN oleh pengguna",
                     "level": "warn"})
    progres._tulis(p)
    return {"ok": True}


@app.get("/runs/current")
def current_run():
    p = progres.baca()
    return {"running": bool(p.get("running")), "stage": p.get("stage"),
            "stageIndex": p.get("stageIndex", 4), "log": p.get("log", [])[-120:]}


# ---------------------------------------------------------------- dashboard

@app.get("/dashboard/summary")
def summary():
    runs = _runs(200)
    jam, menit = map(int, konfig.baca_settings()["schedule"].split(":"))
    berikut = datetime.now().replace(hour=jam, minute=menit, second=0, microsecond=0)
    if berikut <= datetime.now():
        berikut += timedelta(days=1)

    awal = date.today() - timedelta(days=6)                 # "Tren 7 hari" di dashboard
    per_hari = {(awal + timedelta(days=i)).isoformat(): {"found": 0, "recommended": 0}
                for i in range(7)}
    for r in runs:
        hari = r["startedAt"][:10]
        if hari in per_hari:
            per_hari[hari]["found"] += r["found"]
            per_hari[hari]["recommended"] += r["recommended"]

    kosong = {"id": "r0", "startedAt": "", "durationSec": 0, "found": 0, "passedLocal": 0,
              "recommended": 0, "coverage": None, "precision": None, "tokens": 0,
              "status": "berhasil"}
    # Laporan utama = run pengumpulan terakhir; run "penilaian ulang" (setelah profil
    # diubah) tidak menemukan apa pun, jadi headline-nya akan selalu "0 sinyal".
    # Run yang dibatalkan pengguna juga bukan laporan yang berarti.
    utama = next((r for r in runs if r["found"] or
                  (r["status"] == "gagal" and r.get("error") != "dibatalkan pengguna")),
                 runs[0] if runs else kosong)
    return {"lastRun": utama,
            "nextRunAt": berikut.astimezone().isoformat(timespec="seconds"),
            "trend": [{"date": d, **v} for d, v in per_hari.items()]}


@app.get("/health")
def health():
    return {"ok": True, "profil": config.profil_hash()}


# --------------------------------------------------------------------- auth

class TiketIn(BaseModel):
    tiket: str = Field(min_length=10, max_length=2000)


@app.post("/auth/tukar")
def tukar_tiket(body: TiketIn):
    sesi = auth.tukar(body.tiket)
    if not sesi:
        raise HTTPException(401, "Tiket login tidak sah, kedaluwarsa, sudah dipakai, atau email tidak diizinkan")
    return sesi


@app.get("/auth/status")
def status_login():
    """Dipakai halaman /masuk: apakah login wajib (sudah disiapkan) di komputer ini."""
    return {"loginAktif": auth.aktif()}


@app.get("/auth/saya")
def saya():
    """Lolos Penjaga = token sesi masih sah."""
    return {"ok": True}
