"""Evaluasi saringan judul terhadap keputusan Qwen yang membaca isi lengkap.

    .venv\\Scripts\\python scripts\\eval_saring_judul.py

Yang paling penting: berapa magang IT relevan yang IKUT TERBUANG (false negative),
karena lowongan itu tidak akan pernah dibuka. Membuka halaman non-IT (false
positive) hanya membuang beberapa detik.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, local_extract  # noqa: E402

with db.koneksi() as con:
    # Hanya lowongan magang: di sini `relevan` = keputusan "IT atau bukan" dari isi lengkap.
    rows = con.execute("SELECT judul, relevan FROM lowongan WHERE jenis = 'magang' "
                       "AND duplikat_dari IS NULL").fetchall()

judul = [r["judul"] for r in rows]
t = time.perf_counter()
buka = local_extract.saring_judul(judul)
detik = time.perf_counter() - t

fn = [j for j, b, r in zip(judul, buka, rows) if not b and r["relevan"]]
dibuang = sum(not b for b in buka)
relevan = sum(r["relevan"] for r in rows)
print(f"{len(judul)} judul dinilai dalam {detik:.1f} s ({detik / max(1, len(judul)) * 1000:.0f} ms/judul)")
print(f"dibuang (tidak dibuka): {dibuang}/{len(judul)} = {dibuang / len(judul):.0%}")
print(f"magang IT relevan ikut terbuang: {len(fn)}/{relevan}")
for j in fn:
    print("   ✗", j)
print("contoh yang dibuang:", [j for j, b in zip(judul, buka) if not b][:12])
print("contoh yang dibuka tapi ternyata bukan IT:",
      [j for j, b, r in zip(judul, buka, rows) if b and not r["relevan"]][:10])
