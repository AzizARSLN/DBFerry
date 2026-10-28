"""DBFerry icin olay (log) veri yolu.

Is surecleri bu yola olay yayinlar; CLI dogrudan yazdirir, web arayuzu
Server-Sent Events uzerinden canli olarak tuketir.
"""

import queue
import threading
import time

# Olay seviyeleri -----------------------------------------------------------------
PLAN = "plan"          # Yapilacaklar listesi
STAGE = "stage"        # Asama basligi (orn. "3/8 ...")
INFO = "info"
OK = "ok"
WARN = "warn"
ERROR = "error"
CMD = "cmd"            # Calistirilan uzak komut
OUT = "out"            # Komutun standart cikitisi
ERR = "err"            # Komutun hata cikitisi
PROGRESS = "progress"  # Aktarim yuzdesi
RESULT = "result"      # Is sonucu ozeti

LEVELS = (PLAN, STAGE, INFO, OK, WARN, ERROR, CMD, OUT, ERR, PROGRESS, RESULT)

# Web arayuzunde renklendirme icin esleme
CSS_CLASS = {
    PLAN: "ev-plan",
    STAGE: "ev-stage",
    INFO: "ev-info",
    OK: "ev-ok",
    WARN: "ev-warn",
    ERROR: "ev-error",
    CMD: "ev-cmd",
    OUT: "ev-out",
    ERR: "ev-err",
    PROGRESS: "ev-progress",
    RESULT: "ev-result",
}


class EventBus(object):
    """Olaylari hem bellekte tutar hem de canli abonelere dagitir."""

    def __init__(self, keep=8000):
        self._events = []
        self._subs = []
        self._lock = threading.Lock()
        self._keep = keep
        self._seq = 0

    def publish(self, level, message, stage=None, **extra):
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "ts": time.time(),
                "level": level,
                "stage": stage,
                "message": message,
            }
            if extra:
                ev.update(extra)
            self._events.append(ev)
            if len(self._events) > self._keep:
                del self._events[: len(self._events) - self._keep]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return ev

    def subscribe(self):
        """(gecmis_olaylar, canli_kuyruk) dondurur. Kilit altinda alinir, kayip olmaz."""
        q = queue.Queue(maxsize=20000)
        with self._lock:
            snapshot = list(self._events)
            self._subs.append(q)
        return snapshot, q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def snapshot(self):
        with self._lock:
            return list(self._events)

    def clear(self):
        with self._lock:
            self._events = []


class Interrupted(RuntimeError):
    """Kullanici islemi iptal etti."""
