import unittest

from pipeline.models import kanonik
from pipeline.sumber.connector import tautan_detail
from rag.store import sidik_jari


class TestKanonik(unittest.TestCase):
    def test_buang_pelacak_dan_fragmen(self):
        a = kanonik("https://www.Glints.com/id/jobs/x/?utm_source=a&traceInfo=1#top")
        self.assertEqual(a, kanonik("https://glints.com/id/jobs/x?traceInfo=1"))

    def test_query_bermakna_dipertahankan(self):
        self.assertNotEqual(kanonik("https://a.com/job?id=1"), kanonik("https://a.com/job?id=2"))


class TestTautanDetail(unittest.TestCase):
    MD = """
### [Backend Intern](/id/job/94769892?type=standard&ref=search#sol=abc)
di [PT X](/id/PT-X-jobs)
### [Backend Intern lagi](/id/job/94769892?origin=cardTitle)
[ML Intern](https://id.jobstreet.com/id/job/111)
"""

    def test_relatif_jadi_absolut_tanpa_query_dan_unik(self):
        got = tautan_detail(self.MD, "https://id.jobstreet.com/id/magang-jobs", r"/id/job/\d+")
        self.assertEqual(got, ["https://id.jobstreet.com/id/job/94769892",
                               "https://id.jobstreet.com/id/job/111"])


class TestSaringJudul(unittest.TestCase):
    def test_hanya_kartu_magang(self):
        from pipeline.local_extract import POLA_MAGANG
        md = ("## [Backend Intern](/c/a/jobs/1/backend-intern)\n[View Post](/c/a/jobs/1/backend-intern)\n"
              "## [Senior Backend Engineer](/c/b/jobs/2/senior)\n[View Post](/c/b/jobs/2/senior)\n")
        got = tautan_detail(md, "https://www.kalibrr.id/", r"/c/[^/]+/jobs/\d+", POLA_MAGANG)
        self.assertEqual(got, ["https://www.kalibrr.id/c/a/jobs/1/backend-intern"])


class TestSidikJari(unittest.TestCase):
    def test_variasi_penulisan_perusahaan(self):
        self.assertEqual(sidik_jari("Backend Intern", "PT. Maju Jaya Tbk"),
                         sidik_jari("backend  intern", "Maju Jaya"))

    def test_perusahaan_berbeda(self):
        self.assertNotEqual(sidik_jari("Backend Intern", "Maju Jaya"),
                            sidik_jari("Backend Intern", "Mundur Jaya"))


class TestPagarMagang(unittest.TestCase):
    def test_kata_magang_terdeteksi(self):
        from pipeline.local_extract import terlihat_magang
        self.assertTrue(terlihat_magang("Software Developer Intern", ""))
        self.assertTrue(terlihat_magang("Programmer", "Program PKL untuk mahasiswa aktif"))

    def test_lowongan_biasa_bukan_magang(self):
        from pipeline.local_extract import terlihat_magang
        self.assertFalse(terlihat_magang("Full Stack Developer", "Minimal S1, pengalaman 1 tahun"))
        # "international" / "internal" tidak boleh terbaca sebagai "intern"
        self.assertFalse(terlihat_magang("IT Staff", "perusahaan internasional, audit internal"))

    def test_abaikan_url_dan_lowongan_serupa(self):
        from pipeline.local_extract import terlihat_magang
        teks = ("Posisi: [Lulusan Baru](/id-ID/home/w/200-entry-level-or-junior,-apprentice)\n"
                "Deskripsi pekerjaan tetap.\n\n## [IT Security Internship](/id-ID/c/x/jobs/1/y)\n")
        self.assertFalse(terlihat_magang("IT Security", teks))


class TestJsonLd(unittest.TestCase):
    MD = ('# QA Intern\n\nisi\n\n## Data terstruktur (JSON-LD)\n\n```json\n'
          '[{"@type": "JobPosting", "employmentType": "INTERN", "validThrough": "2026-10-23",'
          ' "hiringOrganization": {"name": "byOrange"}}, {"@type": "BreadcrumbList"}]\n```\n')

    def test_pisah_dan_terapkan(self):
        from pipeline.local_extract import _rapikan
        from pipeline.models import Ekstraksi, LowonganMentah
        from pipeline.sumber.connector import pisah_jsonld

        teks, jp = pisah_jsonld(self.MD)
        self.assertNotIn("JSON-LD", teks)
        self.assertEqual(jp["employmentType"], "INTERN")
        e = Ekstraksi(judul="QA", perusahaan="", lokasi="", tipe_kerja="onsite", jenis="fulltime",
                      kompensasi="tidak_disebut", relevan_it=True)
        e = _rapikan(e, LowonganMentah("glints", "https://x", teks, extra={"jobposting": jp}))
        self.assertEqual((e.jenis, e.deadline, e.perusahaan), ("magang", "2026-10-23", "byOrange"))


class TestPencarianBertarget(unittest.TestCase):
    def test_konteks_per_url_dan_tanpa_kata_kunci(self):
        from pipeline.sumber.connector import daftar_pencarian
        cfg = {"kata_kunci": ["magang", "intern"], "pencarian": [
            {"url": "https://x/{q_slug}-jobs/remote", "tipe_kerja": "remote"},
            {"url": "https://x/explore?loc=DIY", "lokasi": "DI Yogyakarta"}]}
        got = daftar_pencarian(cfg)
        self.assertEqual(got, [("https://x/magang-jobs/remote", {"tipe_kerja": "remote"}),
                               ("https://x/intern-jobs/remote", {"tipe_kerja": "remote"}),
                               ("https://x/explore?loc=DIY", {"lokasi": "DI Yogyakarta"})])

    def test_konteks_dan_label_glints(self):
        from pipeline.local_extract import _rapikan
        from pipeline.models import Ekstraksi, LowonganMentah
        e = Ekstraksi(judul="Backend Developer", perusahaan="X", lokasi="Kerja di lokasi / rumah",
                      tipe_kerja="tidak_disebut", jenis="fulltime", kompensasi="tidak_disebut",
                      relevan_it=True)
        m = LowonganMentah("glints", "https://x", "teks tanpa kata kunci",
                           extra={"konteks": {"jenis": "magang", "lokasi": "DI Yogyakarta"}})
        e = _rapikan(e, m)
        self.assertEqual((e.jenis, e.tipe_kerja, e.lokasi), ("magang", "hybrid", "DI Yogyakarta"))


class TestJudulKartu(unittest.TestCase):
    def test_pilih_judul_bukan_tombol(self):
        from pipeline.sumber.connector import judul_kartu
        self.assertEqual(judul_kartu(["", "## Magang Bakti BCA", "View Post"]), "Magang Bakti BCA")


class TestPagarJudulIT(unittest.TestCase):
    def test_kata_it_tidak_pernah_ditanyakan_ke_model(self):
        from pipeline.local_extract import POLA_IT_LUNAK
        for j in ["Specification Engineer Intern", "UI/UX Designer Intern", "Magang IT Support",
                  "Data Analyst Intern", "Automation Project Intern"]:
            self.assertTrue(POLA_IT_LUNAK.search(j), j)
        for j in ["Magang HRGA", "Magang Akuntansi", "Secretary (Magang)", "Magang Videographer"]:
            self.assertFalse(POLA_IT_LUNAK.search(j), j)

    def test_model_gagal_semua_tetap_dibuka(self):
        from unittest import mock
        from pipeline import local_extract
        with mock.patch.object(local_extract.httpx, "post", side_effect=OSError("ollama mati")):
            self.assertEqual(local_extract.saring_judul(["Magang HRGA", "Magang Admin"]), [True, True])


class TestAntreanLatar(unittest.TestCase):
    def test_selang_seling_antar_sumber(self):
        from pipeline.run import _selang_seling
        got = _selang_seling({"a": [1, 2, 3], "b": [4], "c": [5, 6]})
        self.assertEqual([n for n, _ in got], ["a", "b", "c", "a", "c", "a"])

    def test_urutan_error_dan_jeda_per_sumber(self):
        import time
        from pipeline.run import _ambil_di_latar

        def gagal():
            raise RuntimeError("halaman gagal")
        antrean = [("a", ("u1", "", lambda: "A1")), ("b", ("u2", "", gagal)),
                   ("a", ("u3", "", lambda: "A2"))]
        t = time.monotonic()
        got = list(_ambil_di_latar(antrean, {"a": 0.3, "b": 0}))
        # Dua halaman "a" berjarak ≈ jeda (toleransi resolusi timer Windows ~15 ms).
        self.assertGreaterEqual(time.monotonic() - t, 0.27)
        self.assertEqual([n for n, _ in got], ["a", "b", "a"])
        self.assertEqual(got[0][1], "A1")
        self.assertIsInstance(got[1][1], RuntimeError)


class TestRisetPerusahaan(unittest.TestCase):
    def test_kunci_sama_untuk_variasi_nama(self):
        from pipeline.riset import kunci_perusahaan
        self.assertEqual(kunci_perusahaan("PT. Javan Cipta Solusi"), kunci_perusahaan("Javan Cipta Solusi"))
        self.assertEqual(kunci_perusahaan("PT Astra Graphia Tbk"), "astra graphia")
        self.assertNotEqual(kunci_perusahaan("Javan Cipta"), kunci_perusahaan("Javan Cipta Solusi"))

    def test_skema_laporan_datar(self):
        import json
        from pipeline.rank import _skema_datar
        from pipeline.riset import LaporanPerusahaan
        self.assertNotIn("$ref", json.dumps(_skema_datar(LaporanPerusahaan)))


class TestSkemaClaudeCode(unittest.TestCase):
    def test_tanpa_ref(self):
        import json
        from pipeline.models import DaftarPenilaian
        from pipeline.rank import _skema_datar
        s = json.dumps(_skema_datar(DaftarPenilaian))
        self.assertNotIn("$ref", s)
        self.assertIn("alasan", s)


if __name__ == "__main__":
    unittest.main()
