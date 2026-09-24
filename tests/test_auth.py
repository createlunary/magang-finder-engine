"""Login jembatan (api/auth.py) dan penjaga akses di API."""

import os
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from api import auth
from api.main import app

ENV = {"MF_JEMBATAN_SECRET": "rahasia-uji-" + "x" * 32, "MF_EMAIL_IZIN": "saya@contoh.com"}


def tiket(email="saya@contoh.com", umur=60, jti="t1", jenis="tiket"):
    return auth.tanda({"typ": jenis, "email": email, "exp": int(time.time() + umur), "jti": jti})


@mock.patch.dict(os.environ, ENV)
class TestToken(unittest.TestCase):
    def setUp(self):
        auth._tiket_terpakai.clear()

    def test_tiket_sah_ditukar_sekali(self):
        t = tiket()
        sesi = auth.tukar(t)
        self.assertIsNotNone(sesi)
        self.assertIsNotNone(auth.baca(sesi["token"], "sesi"))
        self.assertIsNone(auth.tukar(t), "tiket yang sama tidak boleh dipakai dua kali")

    def test_ditolak(self):
        self.assertIsNone(auth.tukar(tiket(umur=-1)), "kedaluwarsa")
        self.assertIsNone(auth.tukar(tiket(email="orang@lain.com", jti="t2")), "email di luar daftar izin")
        self.assertIsNone(auth.tukar(tiket(jenis="sesi", jti="t3")), "jenis token salah")
        badan, ttd = tiket(jti="t4").split(".")
        self.assertIsNone(auth.tukar(badan + "." + ttd[:-2] + "AA"), "tanda tangan diubah")
        with mock.patch.dict(os.environ, {"MF_JEMBATAN_SECRET": "rahasia-lain-" + "y" * 32}):
            palsu = tiket(jti="t5")
        self.assertIsNone(auth.tukar(palsu), "ditandatangani rahasia lain")

    def test_ip_lokal(self):
        for ip in ("127.0.0.1", "192.168.1.4", "10.0.0.7", "172.20.1.1", "::1"):
            self.assertTrue(auth.ip_lokal(ip), ip)
        for ip in ("8.8.8.8", "103.162.60.230", "", "bukan-ip"):
            self.assertFalse(auth.ip_lokal(ip), ip)


def klien(ip: str) -> TestClient:
    return TestClient(app, client=(ip, 50000))


class TestPenjaga(unittest.TestCase):
    @mock.patch.dict(os.environ, ENV)
    def test_wajib_login_di_lan(self):
        c = klien("192.168.1.9")
        self.assertEqual(c.get("/health").status_code, 200)
        self.assertEqual(c.get("/settings").status_code, 401)
        auth._tiket_terpakai.clear()
        r = c.post("/auth/tukar", json={"tiket": tiket(jti="p1")})
        self.assertEqual(r.status_code, 200)
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        self.assertEqual(c.get("/settings", headers=h).status_code, 200)
        self.assertEqual(c.get("/settings", headers={"Authorization": "Bearer ngasal"}).status_code, 401)

    @mock.patch.dict(os.environ, ENV)
    def test_tolak_ip_publik(self):
        self.assertEqual(klien("8.8.8.8").get("/health").status_code, 403)

    @mock.patch.dict(os.environ, {"MF_JEMBATAN_SECRET": "", "MF_EMAIL_IZIN": ""})
    def test_tanpa_login_hanya_komputer_ini(self):
        self.assertEqual(klien("127.0.0.1").get("/settings").status_code, 200)
        self.assertEqual(klien("192.168.1.9").get("/settings").status_code, 403)

    @mock.patch.dict(os.environ, ENV)
    def test_cors_lan_pada_penolakan(self):
        r = klien("192.168.1.9").get("/settings", headers={"Origin": "http://192.168.1.4:3000"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://192.168.1.4:3000")


if __name__ == "__main__":
    unittest.main()
