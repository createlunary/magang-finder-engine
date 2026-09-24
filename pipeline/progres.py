"""Status run yang sedang berjalan, ditulis ke logs/progres.json.

Ditulis oleh proses run (dari CLI, scheduler, maupun yang dipicu website) dan
dibaca oleh API. File dipakai — bukan memori bersama — supaya run terjadwal
yang tidak dimulai lewat website pun tetap bisa dipantau dari dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

from . import config

TAHAP = ["scrape", "dedup", "local_llm", "ranking"]
MAKS_LOG = 300

log = logging.getLogger("magang")


def path():
    return config.path("log") / "progres.json"


def _tulis(data: dict) -> None:
    p = path()
    p.parent.mkdir(exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # Di Windows os.replace gagal kalau pembaca sedang membuka file; ulangi sebentar.
    for _ in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.05)


def proses_hidup(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        kode = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(kode))
        k32.CloseHandle(h)
        return bool(ok) and kode.value == 259     # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def baca() -> dict:
    """Status terakhir. Run yang prosesnya sudah mati ditandai tidak berjalan."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False, "stage": None, "stageIndex": 4, "log": []}
    if data.get("running") and not proses_hidup(data.get("pid", -1)):
        data["running"] = False
        data["stage"] = None
        data["log"].append({"t": datetime.now().strftime("%H:%M:%S"),
                            "msg": "run berhenti tanpa selesai (proses mati)", "level": "warn"})
    return data


class Progres:
    def __init__(self, run_id: int | None = None):
        self.data = {"running": True, "pid": os.getpid(), "run_id": run_id,
                     "mulai": datetime.now().isoformat(timespec="seconds"),
                     "stage": None, "stageIndex": 0, "log": []}
        _tulis(self.data)

    def tahap(self, stage: str) -> None:
        self.data["stage"] = stage
        self.data["stageIndex"] = TAHAP.index(stage)
        _tulis(self.data)

    def catat(self, msg: str, level: str = "info") -> None:
        (log.warning if level == "warn" else log.info)(msg)
        self.data["log"].append({"t": datetime.now().strftime("%H:%M:%S"), "msg": msg,
                                 "level": level})
        self.data["log"] = self.data["log"][-MAKS_LOG:]
        _tulis(self.data)

    def selesai(self) -> None:
        self.data.update(running=False, stage=None, stageIndex=len(TAHAP))
        _tulis(self.data)
