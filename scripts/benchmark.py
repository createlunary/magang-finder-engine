"""Ukur biaya tiap komponen per lowongan: connector, LLM lokal, embedding.

    .venv\\Scripts\\python scripts\\benchmark.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from pipeline import config, db, local_extract  # noqa: E402
from pipeline.models import LowonganMentah  # noqa: E402
from pipeline.sumber import connector  # noqa: E402
from rag import store  # noqa: E402


def waktu(label, fn, n=1):
    hasil = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        hasil.append(time.perf_counter() - t)
    print(f"{label:48} {' / '.join(f'{x:5.1f}s' for x in hasil)}")
    return hasil


c = config.settings()["connector"]
print("== connector")
waktu("start Python + import sr.py (tanpa browser)",
      lambda: subprocess.run([c["python"], c["sr_py"], "situs"], capture_output=True))
url = "https://id.jobstreet.com/id/job/94807805"
sel = next(s for s in config.sumber() if s["nama"] == "jobstreet")["selector_detail"]
waktu("ambil 1 halaman detail (proses + browser baru)",
      lambda: connector.ambil(url, selector=sel, jsonld=True), n=3)
print(f"{'jeda sopan antar halaman (setting)':48} {c['jeda_detik']:5.1f}s")

print("\n== LLM lokal (Qwen)")
with db.koneksi() as con:
    rows = con.execute("SELECT sumber, url, teks_mentah FROM lowongan WHERE relevan = 1 "
                       "ORDER BY id DESC LIMIT 3").fetchall()
s = config.settings()["local_llm"]
for r in rows:
    m = LowonganMentah(r["sumber"], r["url"], r["teks_mentah"])
    body = {"model": s["model"], "stream": False, "format": local_extract.Ekstraksi.model_json_schema(),
            "options": {"temperature": 0, "num_ctx": s["num_ctx"]},
            "messages": [{"role": "system", "content": local_extract.SISTEM},
                         {"role": "user", "content": m.teks[: s["maks_karakter_input"]]}]}
    t = time.perf_counter()
    d = httpx.post(f"{s['host']}/api/chat", json=body, timeout=300).json()
    total = time.perf_counter() - t
    ns = 1e9
    print(f"ekstraksi {len(m.teks):6,} kar: total {total:5.1f}s | muat model {d['load_duration']/ns:4.1f}s"
          f" | prompt {d['prompt_eval_count']:5} tok {d['prompt_eval_duration']/ns:4.1f}s"
          f" | keluaran {d['eval_count']:4} tok {d['eval_duration']/ns:5.1f}s"
          f" ({d['eval_count']/(d['eval_duration']/ns):4.1f} tok/s)")

print("\n== embedding (bge-m3)")
waktu("embed 1 dokumen", lambda: store.embed(["Backend Developer Intern | PT X | Yogyakarta"]), n=3)

print("\n== Ollama")
print(json.dumps(httpx.get(f"{s['host']}/api/ps").json(), indent=1)[:900])
