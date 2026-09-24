"""Kirim digest lowongan terurut ke Telegram."""

from __future__ import annotations

import html
import json

import httpx

from pipeline import config

API = "https://api.telegram.org/bot{token}/{metode}"
BATAS_PESAN = 4000   # batas Telegram 4096, sisakan ruang


def _panggil(metode: str, **data) -> dict:
    token = config.env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum diisi di .env")
    r = httpx.post(API.format(token=token, metode=metode), json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def kirim_teks(teks: str) -> None:
    chat_id = config.env("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID belum diisi di .env (jalankan `python mf.py telegram-id`)")
    _panggil("sendMessage", chat_id=chat_id, text=teks, parse_mode="HTML",
             link_preview_options={"is_disabled": True})


def _item(r) -> str:
    e = html.escape
    baris = [f"<b>{r['skor']}</b> · <a href=\"{e(r['url'])}\">{e(r['judul'] or '-')}</a>",
             f"🏢 {e(r['perusahaan'] or '-')} · 📍 {e(r['lokasi'] or '-')} · {e(r['sumber'])}"]
    if r["deadline"]:
        baris.append(f"⏰ deadline {e(r['deadline'])}")
    baris.append(f"💬 {e(r['alasan'] or '')}")
    flags = json.loads(r["red_flags"] or "[]")
    if flags:
        baris.append(f"⚠️ {e('; '.join(flags))}")
    baris.append(f"<code>#{r['id']}</code>")
    return "\n".join(baris)


def kirim_digest(baris_hasil: list, statistik: dict) -> int:
    """Kirim item teratas; kembalikan jumlah item yang terkirim."""
    maks = config.settings()["telegram"]["maks_item_digest"]
    item = baris_hasil[:maks]
    kepala = (f"🎯 <b>Magang Finder</b> — {len(baris_hasil)} lowongan cocok"
              f" (baru ditemukan: {statistik.get('baru', 0)})")
    # Kegagalan ranking tidak boleh senyap: kandidat menumpuk tanpa dinilai.
    gagal_rank = [e for e in statistik.get("error", []) if e.startswith("rank")]
    if gagal_rank:
        kepala += f"\n⚠️ {html.escape(gagal_rank[0][:300])}"
    if not item:
        kirim_teks(kepala + "\nTidak ada lowongan baru di atas ambang batas.")
        return 0

    pesan, buf = [], kepala
    for teks in map(_item, item):
        if len(buf) + len(teks) + 2 > BATAS_PESAN:
            pesan.append(buf)
            buf = ""
        buf += "\n\n" + teks
    pesan.append(buf)
    for p in pesan:
        kirim_teks(p.strip())
    return len(item)


def cari_chat_id() -> list[dict]:
    """Baca pesan terakhir yang dikirim ke bot untuk menemukan chat id pengguna."""
    hasil = _panggil("getUpdates").get("result", [])
    chat = {}
    for u in hasil:
        c = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if c:
            chat[c["id"]] = c.get("username") or c.get("title") or c.get("first_name")
    return [{"chat_id": k, "nama": v} for k, v in chat.items()]
