"""Login panel kontrol lokal lewat akun Google, dengan situs showcase sebagai jembatan.

Google hanya mengizinkan redirect OAuth ke `localhost` atau domain publik ber-HTTPS —
tidak ke alamat WiFi seperti http://192.168.1.4:3000. Jadi login Google terjadi di situs
showcase (Vercel), yang lalu mengirim *tiket* bertanda tangan HMAC ke panel lokal:

    panel lokal ──▶ showcase /api/masuk ──▶ Google ──▶ showcase /api/masuk/callback
         ▲                                                   │ email ada di daftar izin?
         └──────────── #tiket=… (umur 2 menit) ◀─────────────┘
    panel lokal ── POST /auth/tukar {tiket} ──▶ API ──▶ token sesi (30 hari)

Kedua sisi berbagi MF_JEMBATAN_SECRET. API tidak pernah bicara dengan Google, dan
tidak ada port komputer ini yang dibuka ke internet.

Penjaga akses (`Penjaga`) menolak semua klien di luar jaringan lokal, lalu mewajibkan
token sesi untuk setiap endpoint selain /health dan /auth/*.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time

from pipeline import config

UMUR_TIKET = 120                  # detik; tiket hanya dipakai sekali, segera setelah login
UMUR_SESI = 30 * 24 * 3600
TERBUKA = {"/health", "/auth/tukar", "/auth/status"}

_tiket_terpakai: dict[str, float] = {}


def _berkas_cabut():
    return config.path("db").parent / "sesi_dicabut_sejak"


def dicabut_sejak() -> float:
    """Token sesi yang terbit sebelum waktu ini ditolak (lihat cabut_semua)."""
    try:
        return float(_berkas_cabut().read_text().strip())
    except (OSError, ValueError):
        return 0.0


def cabut_semua() -> None:
    """Keluarkan semua perangkat sekaligus — mis. HP hilang atau token dicurigai bocor."""
    _berkas_cabut().write_text(str(time.time()))


def _rahasia() -> bytes | None:
    s = config.env("MF_JEMBATAN_SECRET")
    return s.encode() if s else None


def _izin() -> set[str]:
    return {e.strip().lower() for e in (config.env("MF_EMAIL_IZIN") or "").split(",") if e.strip()}


def aktif() -> bool:
    """Login baru berlaku setelah rahasia jembatan dan daftar email diisi di .env."""
    return bool(_rahasia() and _izin())


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def tanda(muatan: dict) -> str:
    badan = _b64(json.dumps(muatan, separators=(",", ":")).encode())
    return f"{badan}.{_b64(hmac.new(_rahasia(), badan.encode(), hashlib.sha256).digest())}"


def baca(token: str, jenis: str) -> dict | None:
    """Muatan token kalau tanda tangan, jenis, masa berlaku, dan email-nya sah."""
    kunci = _rahasia()
    if not kunci or token.count(".") != 1:
        return None
    badan, ttd = token.split(".")
    if not hmac.compare_digest(_b64(hmac.new(kunci, badan.encode(), hashlib.sha256).digest()), ttd):
        return None
    try:
        m = json.loads(_unb64(badan))
    except ValueError:
        return None
    if m.get("typ") != jenis or m.get("exp", 0) < time.time():
        return None
    if str(m.get("email", "")).lower() not in _izin():
        return None
    if jenis == "sesi" and m.get("iat", 0) < dicabut_sejak():
        return None
    return m


def tukar(tiket: str) -> dict | None:
    """Tiket dari situs showcase → token sesi. Tiap tiket hanya bisa ditukar sekali."""
    m = baca(tiket, "tiket")
    if not m or m.get("jti") in _tiket_terpakai:
        return None
    sekarang = time.time()
    for jti, exp in list(_tiket_terpakai.items()):
        if exp < sekarang:
            del _tiket_terpakai[jti]
    _tiket_terpakai[m["jti"]] = m["exp"]
    exp = int(sekarang + UMUR_SESI)
    return {"token": tanda({"typ": "sesi", "email": m["email"], "iat": sekarang, "exp": exp,
                            "jti": secrets.token_hex(8)}),
            "email": m["email"], "exp": exp}


def ip_lokal(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


async def _jawab(send, status: int, detail: str):
    badan = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(badan)).encode())]})
    await send({"type": "http.response.body", "body": badan})


def host_sah(header_host: str) -> bool:
    """Header Host harus alamat lokal (localhost / IP privat), bukan nama domain.

    Menangkal DNS rebinding: situs jahat yang dibuka di browser komputer ini bisa
    mengarahkan domainnya sendiri ke 127.0.0.1/192.168.x.x lalu memanggil API ini
    sebagai "situs yang sama". Browser tetap mengirim Host = domain penyerang itu.
    """
    h = header_host.strip().lower()
    if h.startswith("["):                                   # IPv6: [::1]:8000
        h = h[1:h.find("]")] if "]" in h else ""
    else:
        h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return h == "localhost" or ip_lokal(h)


class Penjaga:
    """Middleware ASGI murni — bukan BaseHTTPMiddleware, supaya aliran SSE chat dan
    deteksi putusnya koneksi browser (yang menghentikan proses Claude) tetap utuh."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        host = (scope.get("client") or ("", 0))[0]
        if not ip_lokal(host):
            return await _jawab(send, 403, "Panel kontrol hanya bisa diakses dari jaringan lokal")
        if not host_sah(dict(scope["headers"]).get(b"host", b"").decode("latin-1")):
            return await _jawab(send, 403, "Host tidak dikenal")
        if scope["method"] == "OPTIONS" or scope["path"] in TERBUKA:
            return await self.app(scope, receive, send)
        if not aktif():
            # Login belum disiapkan: hanya komputer ini sendiri yang boleh masuk.
            if ipaddress.ip_address(host).is_loopback:
                return await self.app(scope, receive, send)
            return await _jawab(send, 403, "Login belum disiapkan — isi MF_JEMBATAN_SECRET dan MF_EMAIL_IZIN di .env")
        otor = dict(scope["headers"]).get(b"authorization", b"").decode()
        if not otor.startswith("Bearer ") or not baca(otor[7:], "sesi"):
            return await _jawab(send, 401, "Perlu login")
        return await self.app(scope, receive, send)
