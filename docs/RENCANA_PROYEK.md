# Rencana Proyek: Magang Finder — Pencarian Magang Otomatis Hybrid (Local + Large LLM)

## 1. Ringkasan

Proyek ini mengembangkan custom browser search connector (RAG) yang sudah ada menjadi
alat pencarian lowongan magang yang otomatis: menjangkau situs-situs yang tidak
terindex search biasa (berbasis login, anti-bot, atau portal tertutup), menyaring dan
menstrukturkan hasilnya, lalu mencocokkannya ke profil skill/minat pribadi sebelum
dikirim sebagai digest terurut.

Selain jadi alat pakai sehari-hari, proyek ini didesain juga sebagai **portofolio**:
menunjukkan kemampuan membangun pipeline scraping + RAG + orchestration multi-model
(bukan sekadar wrapper API), lengkap dengan pertimbangan cost-efficiency dan reasoning.

## 2. Latar Belakang & Tujuan

- Banyak lowongan magang relevan (software dev, backend, ML/NLP) tidak muncul di
  hasil pencarian umum karena berada di balik login atau proteksi anti-bot.
- Connector yang sudah dibangun (dengan RAG untuk efisiensi token) bisa menjangkau
  situs-situs itu — tinggal diarahkan jadi alat pencarian spesifik.
- **Tujuan utama:** memperbesar cakupan (coverage) dan kecepatan (freshness) pencarian
  magang dibanding pencarian manual/search biasa.
- **Tujuan sekunder:** menghasilkan portofolio yang bisa dijelaskan secara teknis saat
  ditanya "ini kerjaan dari mana" — jawabannya: software/pipeline buatan sendiri.

## 3. Prinsip Desain

1. **Pisahkan tugas murah dari tugas mahal.** Model kecil (local) menangani tugas
   bervolume tinggi (parsing, dedup, filter kasar). Model besar (Claude) hanya dipanggil
   untuk tugas yang butuh reasoning halus (mencocokkan requirement ke profil), di atas
   data yang sudah diperkecil — supaya biaya API tetap masuk akal.
2. **Idempotent & dedup-first.** Setiap konten yang di-scrape dicek dulu ke RAG store
   sebelum diproses lebih lanjut, supaya tidak reprocessing data lama.
3. **Auth/session terisolasi.** Logika login/session per situs dipisah dari logika
   ekstraksi, supaya gampang di-maintain kalau satu situs berubah struktur halamannya.
4. **Bisa diaudit.** Setiap hasil ranking dari large LLM harus menyertakan alasan
   singkat (bukan cuma skor), supaya keputusan bisa dicek ulang dan bisa jadi bahan
   cerita portofolio ("sistem ini menjelaskan kenapa suatu lowongan direkomendasikan").

## 4. Arsitektur Sistem

![Arsitektur Sistem](diagrams/01_arsitektur_sistem.png)

Komponen utama:

| Komponen | Peran | Contoh Implementasi |
|---|---|---|
| Custom Browser Search Connector | Akses situs login/anti-bot, ambil konten mentah | Yang sudah kamu bangun |
| RAG Store | Simpan & dedup konten, embedding untuk retrieval | Vector DB (Chroma/Qdrant) + embedding model |
| Local LLM | Ekstraksi field, filter kasar, klasifikasi relevansi | Ollama (Llama/Qwen) di RTX 4060 8GB VRAM |
| Large LLM | Ranking akhir & penilaian kecocokan profil | Claude API |
| profile.json | Kriteria matching (skill, minat, preferensi) | File konfigurasi lokal |
| Output/Notifier | Kirim hasil ke user | Telegram bot / email digest / dashboard sederhana |

## 5. Alur Kerja Detail

![Alur Kerja Detail](diagrams/02_alur_kerja_detail.png)

Ringkasan langkah:

1. **Trigger** — dijalankan terjadwal (mis. tiap pagi via cron) atau manual.
2. **Loop per situs sumber** — cek validitas sesi/login, refresh kalau perlu.
3. **Scrape & ekstraksi mentah** — ambil field: judul, perusahaan, deadline,
   requirement, link.
4. **Dedup terhadap RAG store** — skip kalau sudah pernah dilihat.
5. **Local LLM** — bersihkan teks, strukturkan ke format konsisten, filter kasar
   (relevan bidang informatika/software/ML atau tidak).
6. **Simpan ke RAG store** — hasil terstruktur jadi basis data pencarian & histori.
7. **Setelah semua situs selesai** — kumpulkan kandidat tersaring jadi satu batch.
8. **Large LLM** — cocokkan tiap kandidat ke `profile.json`, beri skor + alasan singkat.
9. **Threshold filter** — hanya yang di atas ambang batas yang disimpan sebagai hasil akhir.
10. **Notifikasi** — kirim digest terurut ke user (Telegram/email/dashboard).
11. **Logging** — catat setiap run (jumlah ditemukan, jumlah lolos, waktu proses) untuk
    evaluasi & metrik.

## 6. Desain profile.json (Matching Criteria)

Contoh isi ada di `config/profile_contoh.json`. Idenya: file ini yang dibaca large LLM
sebagai konteks saat menilai kecocokan, bukan hardcode di prompt — supaya gampang
diupdate tanpa mengubah kode.

Field yang disarankan:
- `bidang_minat`: daftar bidang (mis. backend development, game development, ML/NLP)
- `skill_teknis`: bahasa/framework/tools yang dikuasai
- `preferensi`: remote/onsite/hybrid, lokasi, durasi minimum
- `must_have` vs `nice_to_have`: requirement yang wajib vs bonus
- `red_flags`: kriteria yang bikin lowongan otomatis didiskualifikasi (mis. unpaid tanpa
  kompensasi, requirement tidak jelas)

## 7. Tech Stack yang Disarankan

- **Orchestration:** Python (mudah integrasi dengan connector, Ollama API, dan Claude API)
- **Scheduler:** cron (native) atau `APScheduler` kalau mau tetap di dalam satu proses Python
- **Vector DB:** ChromaDB (ringan, jalan lokal, cocok untuk skala personal project)
- **Local LLM runtime:** Ollama (sudah terpasang) — model kecil-menengah (7B–14B quantized)
  cukup untuk ekstraksi & klasifikasi kasar
- **Large LLM:** Claude API (reasoning ranking final)
- **Notifikasi:** Telegram Bot API (paling cepat disetup) atau email (SMTP)
- **Penyimpanan hasil:** SQLite (histori run, status lowongan: baru/dilihat/dilamar)

## 8. Rencana Tahapan (Roadmap)

**Fase 1 — Fondasi Data (minggu 1)**
- Finalisasi daftar situs sumber (prioritaskan yang paling relevan dulu, jangan semua sekaligus)
- Pastikan connector bisa scrape tiap situs sumber secara stabil
- Setup RAG store + skema field terstruktur

**Fase 2 — Local Processing (minggu 2)**
- Bangun prompt/pipeline ekstraksi & klasifikasi kasar di local LLM
- Uji dedup logic dan skema penyimpanan RAG

**Fase 3 — Ranking & Matching (minggu 3)**
- Susun `profile.json` awal
- Bangun prompt ranking di Claude API (skor + alasan)
- Uji threshold, kalibrasi supaya tidak terlalu ketat/longgar

**Fase 4 — Otomasi & Notifikasi (minggu 4)**
- Setup scheduler + notifikasi (Telegram/email)
- Tambah logging & dashboard sederhana (opsional: halaman HTML statis dari SQLite)

**Fase 5 — Dokumentasi Portofolio (minggu 5)**
- Tulis README teknis: masalah, arsitektur, keputusan desain (kenapa hybrid model)
- Siapkan demo (video pendek atau screenshot alur end-to-end)
- Bersihkan kode, pisahkan config sensitif (kredensial) dari repo publik

## 9. Risiko & Mitigasi

| Risiko | Dampak | Mitigasi |
|---|---|---|
| Scraping situs berbasis login melanggar ToS (mis. LinkedIn) | Akun bisa kena flag/banned | Batasi frekuensi, prioritaskan situs yang lebih longgar soal automation, pertimbangkan akun terpisah untuk testing, jangan didemo publik pakai akun utama |
| Struktur halaman situs berubah | Ekstraksi gagal/salah | Isolasi logika per-situs, tambahkan validasi field & alert kalau field kosong terus-menerus |
| Local LLM salah klasifikasi (lolos filter kasar padahal tidak relevan) | Large LLM memproses data tidak relevan → biaya naik | Kalibrasi prompt filter kasar, evaluasi berkala dengan sample manual |
| Large LLM memberi skor tidak konsisten | Hasil ranking kurang bisa dipercaya | Minta model selalu sertakan alasan, log skor dari waktu ke waktu untuk cek konsistensi |
| Biaya API Claude membengkak | Cost tidak sustainable | Pastikan batching benar-benar terjadi setelah filter local, cache hasil yang sudah dinilai |

## 10. Metrik Keberhasilan

- **Coverage:** jumlah lowongan relevan ditemukan per minggu dibanding pencarian manual
- **Precision:** dari lowongan yang direkomendasikan, berapa persen benar-benar relevan
  (cek manual berkala)
- **Freshness:** rata-rata jeda waktu antara lowongan diposting dan terdeteksi sistem
- **Time saved:** estimasi waktu yang dihemat dibanding scrolling manual tiap situs

## 11. Struktur Repo (untuk Portofolio/GitHub)

```
magang-finder/
├── README.md                  # penjelasan proyek + arsitektur (untuk pembaca luar)
├── connector/                 # browser search connector (sudah ada)
├── rag/                       # vector store, embedding, dedup logic
├── pipeline/
│   ├── scraper.py
│   ├── local_extract.py       # panggil local LLM (Ollama)
│   ├── rank.py                 # panggil large LLM (Claude API)
│   └── scheduler.py
├── config/
│   ├── sumber_situs.yaml      # daftar & konfigurasi situs sumber
│   └── profile.json           # kriteria matching (di-gitignore kalau berisi data pribadi)
├── notifier/
│   └── telegram_bot.py
├── db/
│   └── hasil.sqlite
└── docs/
    └── arsitektur.md          # versi ringkas diagram + penjelasan keputusan desain
```

## 12. Pengembangan Lanjutan (Opsional, setelah versi dasar jalan)

- Feedback loop: tandai lowongan yang dilamar/diterima, pakai untuk memperhalus
  `profile.json` atau bobot ranking dari waktu ke waktu
- Multi-user: buka alat ini untuk teman satu jurusan dengan profil masing-masing
- Ekspansi sumber: tambah grup Discord/Telegram magang sebagai sumber tambahan
- A/B antara beberapa large model (Claude vs alternatif lain) untuk membandingkan
  kualitas ranking — sejalan dengan ide pipeline multi-model yang pernah dipikirkan

---
*Dokumen ini adalah rencana awal — sesuaikan skala tahapan dengan waktu yang tersedia.*
