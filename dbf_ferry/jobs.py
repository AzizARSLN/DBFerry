"""DBFerry is orkestrasyonu.

Her is once PLAN uretir (ne yapilacak), kullanici onayindan sonra adim adim
uygular (ne yapiyor) ve sonunda ozet raporlar (ne yapti). Tum adimlar olay
yoluna canli olarak yayinlanir.
"""

import os
import threading
import time

from . import APP_NAME, __version__
from .events import (CMD, ERR, ERROR, EventBus, INFO, Interrupted, OK, OUT,
                     PLAN, PROGRESS, RESULT, STAGE, WARN)
from .localx import LocalError, LocalRemote, start_windows_services, stop_windows_services
from .model import JOB_TYPES, VerifyCheck
from .pg import PgDocker, PgError, start_containers, stop_containers, verify_checks
from .sshx import Remote, SSHError, human_bytes


def _stamp():
    return time.strftime("%Y%m%dT%H%M%S")


def _make_remote(profile, log, should_stop):
    """Profile gore SSH oturumu ya da yerel komut yurutucusu dondurur."""
    if profile.is_local():
        return LocalRemote(profile, log=log, should_stop=should_stop)
    return Remote(profile, log=log, should_stop=should_stop)


class Job(object):
    """Tek bir tasima isini temsil eder; planlar, uygular, raporlar."""

    def __init__(self, spec, store, title=""):
        self.id = time.strftime("%H%M%S") + "-" + os.urandom(2).hex()
        self.spec = spec
        self.store = store
        self.title = title or JOB_TYPES.get(spec.type, spec.type)
        self.bus = EventBus()
        self.status = "hazir"          # hazir | calisiyor | tamam | hata | iptal
        self.started_at = None
        self.finished_at = None
        self.stage = ""
        self.summary_text = ""
        self.result = {}
        self.error = ""
        self._cancel = threading.Event()
        self._finished_evt = threading.Event()
        self._thread = None
        self._remotes = []
        self._steps = []

    # -- log --------------------------------------------------------------

    def emit(self, level, message, **extra):
        if extra.get("stage") is None and self.stage:
            extra["stage"] = self.stage
        return self.bus.publish(level, message, **extra)

    # -- durum ------------------------------------------------------------

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary_text,
            "error": self.error,
            "result": self.result,
            "spec": self.spec.safe(),
            "plan": self._steps,
        }

    def cancel(self):
        self._cancel.set()
        self.emit(WARN, "Iptal istegi alindi, mevcut adim bitince durdurulacak")

    def cancelled(self):
        return self._cancel.is_set()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Interrupted("Islem kullanici tarafindan iptal edildi")

    # -- planlama ---------------------------------------------------------

    def plan(self):
        s = self.spec
        steps = []
        add = lambda key, title, detail: steps.append(
            {"key": key, "title": title, "detail": detail, "status": "bekliyor"})
        tgt = self._target_desc()
        src = self._source_desc()

        if s.type in ("file_to_server", "file_to_local"):
            add("preflight", "Yerel dokum dogrulama", "Dosya butunlugu ve SHA-256: %s" % s.source_dump)
        if s.type == "file_to_server":
            add("connect", "Hedef sunucuya baglan", tgt)
            add("envcheck", "Hedef ortam kontrolu", "Docker konteyneri, PostgreSQL surumu, bos disk alani")
        elif s.type == "file_to_local":
            add("envcheck", "Yerel PostgreSQL kontrolu", tgt)
        elif s.type == "local_to_file":
            add("envcheck", "Yerel PostgreSQL kontrolu", src)
            if s.stop_source_services:
                add("stop_src", "Yerel servisleri durdur", "Tutarli dokum icin bakim penceresi")
            add("dump_src", "Yerelde yedek al", "pg_dump -Fc")
            add("checksum", "SHA-256 ve yan dosya", "Yedek ozeti (.sha256) yazilir")
            if s.stop_source_services:
                add("start_src", "Yerel servisleri baslat", "Sistem normale doner")
        elif s.type == "server_to_server":
            add("connect", "Kaynak ve hedef sunuculara baglan", "%s  ->  %s" % (src, tgt))
            add("envcheck_src", "Kaynak ortam kontrolu", "PostgreSQL erisimi ve veritabani boyutu")
            if s.stop_source_services:
                add("stop_src", "Kaynak servisleri durdur", "Tutarli dokum icin bakim penceresi")
            add("dump_src", "Kaynakta yedek al", "pg_dump -Fc")
            add("pull", "Yedegi yerele indir", "Ara aktarim + SHA-256 dogrulama")
            if s.stop_source_services:
                add("start_src", "Kaynak servisleri baslat", "Kaynak sistem normale doner")
        else:  # server_to_file
            add("connect", "Kaynak sunucuya baglan", src)
            add("envcheck_src", "Kaynak ortam kontrolu", "PostgreSQL erisimi ve veritabani boyutu")
            if s.stop_source_services:
                add("stop_src", "Kaynak servisleri durdur", "Tutarli dokum icin bakim penceresi")
            add("dump_src", "Kaynakta yedek al", "pg_dump -Fc")
            add("pull", "Yedegi yerele indir", "SHA-256 dogrulama ve rapor")

        if s.type not in ("server_to_file", "local_to_file"):
            hedef = "Yerel" if s.type == "file_to_local" else "Hedef"
            if s.take_safety_backup:
                add("safety", "%sin guvenlik yedegi" % hedef, "Mevcut veritabani once yedeklenir")
            if s.stop_services:
                add("stop", "%s servisleri durdur" % hedef, "Yazma trafigi kesilir")
            if s.type in ("file_to_server", "server_to_server"):
                add("upload", "Dokumu hedefe yukle", "SFTP aktarim + SHA-256 karsilastirma")
            if s.mode == "replace":
                add("drop", "Mevcut veritabaniyi sil", "DROP DATABASE + CREATE DATABASE")
            else:
                add("drop", "Yeni veritabani olustur", "Mevcut veritabaniya dokunulmaz")
            add("restore", "pg_restore ile geri yukle", "--no-owner --no-privileges --exit-on-error")
            if s.verify:
                add("verify", "Dogrulama sorgulari", "Migration surumu, tablo sayimi ve ozel kontroller")
            if s.start_services:
                add("start", "%s servisleri baslat" % hedef, "API acilir; worker ayri karar")
        steps.append({"key": "report", "title": "Rapor", "detail": "Ozet ve sonuc", "status": "bekliyor"})
        self._steps = steps
        return steps

    def _target_desc(self):
        p = self._profile_for("hedef")
        if not p:
            return "<hedef secilmedi>"
        db = self.spec.new_db_name if self.spec.mode == "new_db" else (self.spec.target_db or p.db_name)
        if p.is_local():
            return "yerel makine (%s@%s:%s)  DB=%s" % (p.db_user, p.db_host, p.db_port, db)
        return "%s (%s@%s)  DB=%s" % (p.name, p.user, p.host, db)

    def _source_desc(self):
        if self.spec.type == "local_to_file":
            p = self.spec.local_profile(self.spec.source_db)
            return "yerel makine (%s@%s:%s)  DB=%s" % (p.db_user, p.db_host, p.db_port, p.db_name)
        p = self.store.get_profile(self.spec.source_profile)
        if not p:
            return "<kaynak sunucu secilmedi>"
        return "%s (%s@%s)  DB=%s" % (p.name, p.user, p.host, self.spec.source_db or p.db_name)

    # -- calistirma -------------------------------------------------------

    def run_async(self):
        self._thread = threading.Thread(target=self.run, name="dbf-job-%s" % self.id, daemon=True)
        self._thread.start()
        return self

    # -- yikici olmayan denetim -------------------------------------------

    def inspect_async(self):
        self._thread = threading.Thread(target=self.run_inspect, name="dbf-inspect-%s" % self.id,
                                        daemon=True)
        self._thread.start()
        return self

    def run_inspect(self):
        """Hicbir seyi degistirmeden sunuculari ve veritabanlarini denetler."""
        self.started_at = time.time()
        self.status = "calisiyor"
        self.emit(STAGE, "%s v%s - ortam denetimi (salt okunur)" % (APP_NAME, __version__))
        try:
            for label, role in (("KAYNAK", "kaynak"), ("HEDEF", "hedef")):
                prof = self._profile_for(role)
                if not prof:
                    continue
                self.emit(STAGE, "%s: %s" % (label, prof.name))
                r = _make_remote(prof, log=self.emit, should_stop=self.cancelled)
                r.connect()
                self._remotes.append(r)
                engine = PgDocker(r, prof, self.emit)
                engine.ping()
                dbs = engine.list_databases()
                self.emit(INFO, "Veritabanlari (%d): %s" % (len(dbs), ", ".join(dbs) or "-"))
                want = engine.p.db_name
                if engine.database_exists(want):
                    self.emit(OK, "'%s' mevcut. Boyut: %s, sema '%s' tablo sayisi: %s" % (
                        want, engine.size_pretty(want), engine.p.db_schema,
                        engine.table_count(engine.p.db_schema)))
                    mig = engine.migration_version(engine.p.db_schema)
                    if mig:
                        self.emit(INFO, "  migration: %s" % mig)
                else:
                    self.emit(WARN, "'%s' veritabani yok" % want)
                self.emit(INFO, "Bos disk (%s): %s" % (self.spec.upload_dir or "/tmp",
                                                       r.free_space(self.spec.upload_dir or "/tmp")))
                self.emit(INFO, "Durdurulacak servisler: %s" % (", ".join(prof.stop_list()) or "-"))
                self.emit(INFO, "Baslatilacak servisler: %s" % (", ".join(prof.start_list()) or "-"))
            self.status = "tamam"
        except Exception as exc:
            self.status = "hata"
            self.error = str(exc)
            self.emit(ERROR, "Denetim hatasi: %s" % exc)
        finally:
            self._cleanup()
            self.finished_at = time.time()
            self.emit(RESULT, "Denetim bitti (%.1f sn) - hicbir degisiklik yapilmadi"
                      % (self.finished_at - self.started_at))
            self._finished_evt.set()


    def run(self):
        self.started_at = time.time()
        self.status = "calisiyor"
        self.emit(STAGE, "%s v%s - is basladi" % (APP_NAME, __version__))
        self.emit(INFO, "Is tipi : %s" % JOB_TYPES.get(self.spec.type, self.spec.type))
        self.emit(INFO, "Ozet    : %s" % self.spec.summary())
        try:
            steps = self.plan()
            self.emit(PLAN, "Yapilacaklar (%d adim):" % len(steps))
            for i, st in enumerate(steps, 1):
                self.emit(PLAN, "  %2d. %-34s %s" % (i, st["title"], st["detail"]))
            if self.spec.type == "file_to_server":
                self._run_file_to_server()
            elif self.spec.type == "file_to_local":
                self._run_file_to_local()
            elif self.spec.type == "local_to_file":
                self._run_local_to_file()
            elif self.spec.type == "server_to_server":
                self._run_server_to_server()
            else:
                self._run_server_to_file()
            if self.cancelled():
                self.status = "iptal"
            else:
                self.status = "tamam"
        except Interrupted as exc:
            self.status = "iptal"
            self.error = str(exc)
            self.emit(WARN, str(exc))
        except (SSHError, LocalError, PgError, ValueError, OSError) as exc:
            self.status = "hata"
            self.error = str(exc)
            self.emit(ERROR, "IS BASARISIZ: %s" % exc)
        except Exception as exc:  # beklenmedik
            self.status = "hata"
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.emit(ERROR, "Beklenmedik hata: %s" % self.error)
        finally:
            self._cleanup()
            self.finished_at = time.time()
            self._finish()

    def _cleanup(self):
        for r in self._remotes:
            r.close()
        self._remotes = []

    def _finish(self):
        dur = (self.finished_at or 0) - (self.started_at or 0)
        icon = {"tamam": "BASARILI", "hata": "BASARISIZ", "iptal": "IPTAL"}.get(self.status, self.status)
        self.emit(RESULT, "=" * 64)
        self.emit(RESULT, "SONUC: %s   sure: %.1f sn" % (icon, dur))
        for line in self.result.get("ozet", []):
            self.emit(RESULT, "  " + line)
        if self.error:
            self.emit(RESULT, "  hata: %s" % self.error)
        self.emit(RESULT, "=" * 64)
        self.summary_text = self.result.get("baslik", self.spec.summary())
        try:
            self.store.append_history(self)
        except Exception as exc:
            self.emit(WARN, "Gecmis kaydedilemedi: %s" % exc)
        self._finished_evt.set()

    def _stage(self, key, note=""):
        st = next((s for s in self._steps if s["key"] == key), None)
        if st:
            st["status"] = "calisiyor"
        idx = (self._steps.index(st) + 1) if st else 0
        self.stage = key
        self.emit(STAGE, "[%d/%d] %s%s" % (idx, len(self._steps),
                                           st["title"] if st else key,
                                           (" - " + note) if note else ""),
                  step=idx, total=len(self._steps), key=key)
        self._check_cancel()

    def _done(self, key):
        for s in self._steps:
            if s["key"] == key:
                s["status"] = "tamam"
        self._check_cancel()

    # -- yardimcilar ------------------------------------------------------

    def _profile_for(self, role):
        """'kaynak' | 'hedef' rolune karsilik gelen profili doner.

        Yerelden Dosyaya isinde kaynak, Dosyadan Yerele isinde hedef profil
        saklanmaz; baglanti bilgileri is tanimindan (is ekranindan) alinir.
        """
        if role == "hedef" and self.spec.type == "file_to_local":
            return self.spec.local_profile()
        if role == "kaynak" and self.spec.type == "local_to_file":
            return self.spec.local_profile(self.spec.source_db)
        pid = self.spec.source_profile if role == "kaynak" else self.spec.target_profile
        return self.store.get_profile(pid)

    def _connect(self, profile_id, role, profile=None):
        p = profile if profile is not None else self.store.get_profile(profile_id)
        if not p:
            raise ValueError("%s icin sunucu profili bulunamadi (id=%s)" % (role, profile_id))
        if not p.is_local() and not p.host:
            raise ValueError("%s profili icin sunucu adresi bos" % role)
        r = _make_remote(p, log=self.emit, should_stop=self.cancelled)
        r.connect()
        self._remotes.append(r)
        return p, r

    def _workdir(self):
        base = os.path.join(self.store.root, "work")
        os.makedirs(base, exist_ok=True)
        return base

    def _backupdir(self):
        base = os.path.join(self.store.root, "backups")
        os.makedirs(base, exist_ok=True)
        return base

    def _ensure_dir(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".dbf-yazma-testi")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            raise PgError(
                "Yedek klasorune yazilamiyor: %s\n  %s\n"
                "Klasor alanini bos birakirsaniz yedekler varsayilan olarak\n"
                "  %s\n"
                "altina yazilir." % (path, exc, self._backupdir()))

    # -- akislar ----------------------------------------------------------

    def _run_file_to_server(self):
        s = self.spec
        local_dump = os.path.abspath(s.source_dump)
        checks = s.checks()

        # 1) yerel dokum
        self._stage("preflight")
        info = self._inspect_local_dump(local_dump)
        self._done("preflight")

        # 2) baglan + 3) ortam
        self._stage("connect")
        tgt, tgt_r = self._connect(s.target_profile, "hedef")
        self._done("connect")
        self._stage("envcheck")
        engine = PgDocker(tgt_r, tgt, self.emit)
        engine.ping()
        tgt_db = self._resolve_target_db(tgt)
        self.emit(INFO, "Hedef veritabani: %s (boyut %s)" % (tgt_db, engine.size_pretty(tgt_db)))
        self.emit(INFO, "Sunucu bos disk (/tmp): %s" % (tgt_r.free_space(s.upload_dir or "/tmp")))
        self._done("envcheck")

        self._prepare_target(engine, tgt_r, tgt, tgt_db, local_dump, info, checks)

    def _run_file_to_local(self):
        s = self.spec
        local_dump = os.path.abspath(s.source_dump)
        checks = s.checks()

        # 1) yerel dokum
        self._stage("preflight")
        info = self._inspect_local_dump(local_dump)
        self._done("preflight")

        # 2) yerel PostgreSQL
        self._stage("envcheck")
        tgt, tgt_r = self._connect(s.target_profile, "yerel hedef", profile=s.local_profile())
        engine = PgDocker(tgt_r, tgt, self.emit)
        engine.ping()
        tgt_db = self._resolve_target_db(tgt)
        self.emit(INFO, "Yerel hedef veritabani: %s (boyut %s)" % (tgt_db, engine.size_pretty(tgt_db)))
        self.emit(INFO, "Calisma bicimi: %s" % (
            "docker konteyneri (%s)" % tgt.pg_container if engine.docker else "dogrudan PostgreSQL"))
        self.emit(INFO, "Yerel bos disk: %s" % tgt_r.free_space(self._backupdir()))
        self._done("envcheck")

        self._prepare_target(engine, tgt_r, tgt, tgt_db, local_dump, info, checks, local=True)

    def _run_local_to_file(self):
        s = self.spec
        src_db = (s.source_db or "").strip()
        if not src_db:
            raise ValueError("Yedeklenecek yerel veritabani secilmedi")

        # 1) yerel PostgreSQL
        self._stage("envcheck")
        prof = s.local_profile(src_db)
        src, src_r = self._connect(s.source_profile, "yerel kaynak", profile=prof)
        engine = PgDocker(src_r, src, self.emit)
        engine.ping()
        if not engine.database_exists(src_db):
            raise PgError("Yerel veritabani bulunamadi: %s" % src_db)
        self.emit(INFO, "Yerel veritabani: %s (boyut %s)" % (src_db, engine.size_pretty(src_db)))
        self.emit(INFO, "Calisma bicimi: %s" % (
            "docker konteyneri (%s)" % src.pg_container if engine.docker else "dogrudan PostgreSQL"))
        out_dir = (s.local_copy_dir or "").strip() or os.path.join(self._backupdir(), _safe(src_db))
        self._ensure_dir(out_dir)
        self.emit(INFO, "Yedek klasoru: %s" % out_dir)
        self.emit(INFO, "Yerel bos disk: %s" % src_r.free_space(out_dir))
        self._done("envcheck")

        stopped = []
        if s.stop_source_services:
            self._stage("stop_src")
            stopped = self._stop_services(src_r, src, True)
            if not stopped:
                self.emit(INFO, "Durdurulacak yerel servis tanimli degil")
            self._done("stop_src")

        self._stage("dump_src")
        local_dump = os.path.join(out_dir, "%s_%s.dump" % (_safe(src_db), _stamp()))
        t0 = time.time()
        _, size = engine.dump(local_dump, dbname=src_db)
        self.emit(OK, "Yedek alindi: %s (%s, %.1f sn)" % (
            local_dump, human_bytes(size), time.time() - t0))
        self._done("dump_src")

        self._stage("checksum")
        sha = self._sha256_file(local_dump)
        with open(local_dump + ".sha256", "w", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (sha, os.path.basename(local_dump)))
        self.emit(OK, "SHA-256: %s" % sha)
        self.emit(INFO, "Yan dosya: %s.sha256" % local_dump)
        self._done("checksum")

        if s.stop_source_services:
            self._stage("start_src")
            start_containers(src_r, src.start_list() or stopped, self.emit)
            self._done("start_src")

        self.result["baslik"] = "Yedek alindi: yerel/%s" % src_db
        self.result["ozet"] = [
            "Kaynak      : yerel makine (%s@%s:%s) DB=%s" % (src.db_user, src.db_host, src.db_port, src_db),
            "Dosya       : %s" % local_dump,
            "Boyut       : %s" % human_bytes(os.path.getsize(local_dump)),
            "SHA-256     : %s" % sha,
            "Yan dosya   : %s.sha256" % local_dump,
        ]
        self._done("report")

    def _run_server_to_server(self):
        s = self.spec
        checks = s.checks()

        self._stage("connect")
        src, src_r = self._connect(s.source_profile, "kaynak")
        tgt, tgt_r = self._connect(s.target_profile, "hedef")
        self._done("connect")

        self._stage("envcheck_src")
        src_engine = PgDocker(src_r, src, self.emit)
        src_engine.ping()
        src_db = s.source_db or src.db_name
        self.emit(INFO, "Kaynak veritabani: %s / %s (boyut %s)" % (
            src.host, src_db, src_engine.size_pretty(src_db)))
        if not src_engine.database_exists(src_db):
            raise PgError("Kaynak veritabani bulunamadi: %s" % src_db)
        self._done("envcheck_src")

        stopped_src = []
        if s.stop_source_services:
            self._stage("stop_src")
            stopped_src = stop_containers(src_r, src.stop_list(), self.emit)
            self._done("stop_src")

        self._stage("dump_src")
        remote_dump = "/tmp/dbf_kaynak_%s_%s.dump" % (src_db, _stamp())
        t0 = time.time()
        _, size = src_engine.dump(remote_dump, dbname=src_db)
        src_sha = src_engine.sha256(remote_dump)
        self.emit(OK, "Kaynak yedek hazir: %s (%s, %.1f sn)" % (
            remote_dump, human_bytes(size), time.time() - t0))
        self.emit(INFO, "Kaynak SHA-256: %s" % src_sha)
        self._done("dump_src")

        self._stage("pull")
        local_dump = os.path.join(self._workdir(), os.path.basename(remote_dump))
        got = src_r.download(remote_dump, local_dump)
        local_sha = self._sha256_file(local_dump)
        if local_sha != src_sha:
            raise SSHError("Indirilen yedek bozuk (SHA-256 uyusmuyor)")
        self.emit(OK, "Indirme dogrulandi: %s (%s)" % (local_dump, human_bytes(got)))
        src_r.run("rm -f %s" % _q(remote_dump), check=False, quiet=True)
        self._done("pull")

        if s.stop_source_services:
            self._stage("start_src")
            start_containers(src_r, src.start_list() or stopped_src, self.emit)
            self._done("start_src")

        self.result["kaynak_yedek"] = dict(db=src_db, sha=src_sha, bytes=got, local=local_dump)

        tgt_engine = PgDocker(tgt_r, tgt, self.emit)
        tgt_engine.ping()
        tgt_db = self._resolve_target_db(tgt)
        self.emit(INFO, "Hedef veritabani: %s (boyut %s)" % (tgt_db, tgt_engine.size_pretty(tgt_db)))
        self._prepare_target(tgt_engine, tgt_r, tgt, tgt_db, local_dump,
                             {"path": local_dump, "bytes": got, "sha256": local_sha}, checks)

    def _run_server_to_file(self):
        s = self.spec
        self._stage("connect")
        src, src_r = self._connect(s.source_profile, "kaynak")
        self._done("connect")

        self._stage("envcheck_src")
        engine = PgDocker(src_r, src, self.emit)
        engine.ping()
        src_db = s.source_db or src.db_name
        self.emit(INFO, "Kaynak veritabani: %s / %s (boyut %s)" % (
            src.host, src_db, engine.size_pretty(src_db)))
        self._done("envcheck_src")

        stopped = []
        if s.stop_source_services:
            self._stage("stop_src")
            stopped = stop_containers(src_r, src.stop_list(), self.emit)
            self._done("stop_src")

        self._stage("dump_src")
        remote_dump = "/tmp/dbf_kaynak_%s_%s.dump" % (src_db, _stamp())
        _, size = engine.dump(remote_dump, dbname=src_db)
        src_sha = engine.sha256(remote_dump)
        self.emit(OK, "Yedek hazir: %s (%s)" % (remote_dump, human_bytes(size)))
        self._done("dump_src")

        self._stage("pull")
        out_dir = s.local_copy_dir or self._backupdir()
        os.makedirs(out_dir, exist_ok=True)
        fname = "%s_%s_%s.dump" % (_safe(src.name), src_db, _stamp())
        local_dump = os.path.join(out_dir, fname)
        got = src_r.download(remote_dump, local_dump)
        local_sha = self._sha256_file(local_dump)
        if local_sha != src_sha:
            raise SSHError("Indirilen yedek bozuk (SHA-256 uyusmuyor)")
        with open(local_dump + ".sha256", "w", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (local_sha, os.path.basename(local_dump)))
        self.emit(OK, "Yedek kaydedildi: %s (%s)" % (local_dump, human_bytes(got)))
        src_r.run("rm -f %s" % _q(remote_dump), check=False, quiet=True)
        self._done("pull")

        if s.stop_source_services:
            self._stage("start_src")
            start_containers(src_r, src.start_list() or stopped, self.emit)
            self._done("start_src")

        self.result["baslik"] = "Yedek alindi: %s/%s" % (src.name, src_db)
        self.result["ozet"] = [
            "Kaynak      : %s (%s) DB=%s" % (src.name, src.host, src_db),
            "Dosya       : %s" % local_dump,
            "Boyut       : %s" % human_bytes(got),
            "SHA-256     : %s" % local_sha,
            "Dogrulama   : indirme sonrasi SHA-256 karsilastirmasi gecti",
        ]
        self._done("report")

    # -- hedefe geri yukleme (ortak) --------------------------------------

    def _prepare_target(self, engine, target_remote, target, tgt_db, local_dump,
                        dump_info, checks, local=False):
        s = self.spec

        if s.take_safety_backup:
            self._stage("safety")
            if engine.database_exists(tgt_db):
                out_dir = os.path.join(self._backupdir(), _safe(target.name))
                os.makedirs(out_dir, exist_ok=True)
                local_bk = os.path.join(out_dir, "%s_%s.dump" % (tgt_db, _stamp()))
                if local:
                    # Dosya zaten bu makinede: dogrudan guvenlik klasorune yazilir
                    _, size = engine.dump(local_bk, dbname=tgt_db)
                else:
                    remote_bk = "/tmp/dbf_guvenlik_%s_%s.dump" % (tgt_db, _stamp())
                    _, size = engine.dump(remote_bk, dbname=tgt_db)
                    target_remote.download(remote_bk, local_bk)
                    target_remote.run("rm -f %s" % _q(remote_bk), check=False, quiet=True)
                bk_sha = self._sha256_file(local_bk)
                self.result["guvenlik_yedegi"] = local_bk
                self.emit(OK, "Guvenlik yedegi: %s (%s)" % (local_bk, human_bytes(size)))
                self.emit(INFO, "Guvenlik yedegi SHA-256: %s" % bk_sha)
            else:
                self.emit(INFO, "Hedefte '%s' veritabani yok, guvenlik yedegi gerekmedi" % tgt_db)
            self._done("safety")

        if s.stop_services:
            self._stage("stop")
            stopped = self._stop_services(target_remote, target, local)
            if not stopped and not target.stop_list():
                self.emit(INFO, "Durdurulacak servis tanimli degil (profil alani bos)")
            self._done("stop")

        # 5) dokumu hedefe ulastir
        if local:
            remote_dump = local_dump
            self.emit(INFO, "Dokum zaten bu makinede, aktarim gerekmiyor: %s" % local_dump)
        else:
            self._stage("upload")
            remote_dump = "%s/dbf_yuklenen_%s.dump" % ((s.upload_dir or "/tmp").rstrip("/"), _stamp())
            target_remote.upload(local_dump, remote_dump)
            remote_sha = engine.sha256(remote_dump)
            if dump_info.get("sha256") and remote_sha.lower() != dump_info["sha256"].lower():
                raise SSHError("Yukleme sonrasi SHA-256 uyusmuyor (uzak=%s)" % remote_sha)
            self.emit(OK, "Yukleme dogrulandi (SHA-256 %s)" % remote_sha[:16] + "...")
            self._done("upload")

        # 6) drop / create
        self._stage("drop")
        if s.mode == "replace":
            engine.terminate_connections(tgt_db)
            exists = engine.database_exists(tgt_db)
            if exists:
                self.emit(WARN, "'%s' veritabani SILINIYOR" % tgt_db)
            engine.drop_database(tgt_db)
            self.emit(OK, "Eski veritabani silindi (varsa): %s" % tgt_db)
        engine.create_database(tgt_db)
        self.emit(OK, "Veritabani olusturuldu: %s" % tgt_db)
        self._done("drop")

        # 7) restore
        self._stage("restore")
        t0 = time.time()
        engine.restore(remote_dump, tgt_db)
        self.emit(OK, "Geri yukleme tamamlandi (%.1f sn)" % (time.time() - t0))
        if not local:
            target_remote.run("rm -f %s" % _q(remote_dump), check=False, quiet=True)
        self._done("restore")

        # 8) dogrulama
        verify_lines = []
        if s.verify:
            self._stage("verify")
            verify_lines = self._verify(engine, tgt_db, checks)
            self._done("verify")
        else:
            self.emit(WARN, "Dogrulama atlandi (kullanici tercihi)")

        # 9) servisleri baslat
        if s.start_services:
            self._stage("start")
            names = target.start_list()
            if names:
                self._start_services(target_remote, target, local)
            else:
                self.emit(INFO, "Hedef profilde baslatilacak servis tanimli degil")
            if not (local and target.pg_mode != "docker"):
                running = target_remote.capture("docker ps --format '{{.Names}}' | tr '\\n' ' '")
                self.emit(INFO, "Calisan konteynerler: %s" % running)
            self._done("start")

        self._stage("report")
        self.result["baslik"] = "Geri yukleme: %s/%s" % (target.name, tgt_db)
        self.result["ozet"] = [
            "Hedef       : %s (%s) DB=%s" % (target.name, self._target_host(target), tgt_db),
            "Kaynak      : %s" % (local_dump if s.type in ("file_to_server", "file_to_local")
                                  else "canli pg_dump"),
            "Boyut       : %s" % human_bytes(dump_info.get("bytes") or os.path.getsize(local_dump)),
            "SHA-256     : %s" % dump_info.get("sha256", "?"),
            "Guvenlik yd.: %s" % self.result.get("guvenlik_yedegi", "alinnmadi"),
            "Dogrulama   : %s" % ("; ".join(verify_lines) if verify_lines else "yapilmadi"),
            "Servisler   : %s" % (", ".join(target.start_list()) or "degisiklik yok"),
        ]
        self._done("report")

    def _stop_services(self, remote, profile, local):
        names = profile.stop_list()
        if not names:
            return []
        if local and profile.pg_mode != "docker":
            return stop_windows_services(remote, names, self.emit)
        return stop_containers(remote, names, self.emit)

    def _start_services(self, remote, profile, local):
        names = profile.start_list()
        if not names:
            return []
        if local and profile.pg_mode != "docker":
            return start_windows_services(remote, names, self.emit)
        return start_containers(remote, names, self.emit)

    @staticmethod
    def _target_host(target):
        return "yerel makine" if target.is_local() else target.host

    def _verify(self, engine, tgt_db, checks):
        lines = []
        tables = engine.table_count(engine.p.db_schema)
        self.emit(INFO, "Sema '%s' tablo sayisi: %s" % (engine.p.db_schema, tables))
        lines.append("%s tablo" % tables)

        if self.spec.expect_migration:
            mig = engine.migration_version(engine.p.db_schema)
            if mig == self.spec.expect_migration:
                self.emit(OK, "Migration surumu beklenen degerde: %s" % mig)
            else:
                raise PgError("Migration uyusmuyor: beklenen %s, gelen %s" % (self.spec.expect_migration, mig))
            lines.append("migration %s" % mig)

        all_checks = list(checks)
        all_checks.append(VerifyCheck(sql="select pg_size_pretty(pg_database_size('%s'))" % tgt_db,
                                      expected="", label="veritabani boyutu"))
        ok_list, bad_list = verify_checks(engine, all_checks, self.emit)
        if bad_list:
            raise PgError("Dogrulama basarisiz: " + "; ".join(bad_list))
        lines.append("%d kontrol gecti" % len(ok_list))
        return lines

    # -- kucuk yardimcilar ------------------------------------------------

    def _resolve_target_db(self, target):
        s = self.spec
        if s.mode == "new_db":
            return s.new_db_name or (s.target_db or target.db_name) + "_kopya"
        return s.target_db or target.db_name

    def _inspect_local_dump(self, path):
        if not os.path.isfile(path):
            raise OSError("Dokum dosyasi bulunamadi: %s" % path)
        size = os.path.getsize(path)
        sha = self._sha256_file(path)
        self.emit(INFO, "Dosya : %s" % path)
        self.emit(INFO, "Boyut : %s" % human_bytes(size))
        self.emit(INFO, "SHA-256: %s" % sha)

        sidecar = path + ".sha256"
        if os.path.isfile(sidecar):
            expected = ""
            with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.strip().split()
                    if parts and len(parts[0]) == 64:
                        expected = parts[0].lower()
                        break
            if expected and expected != sha:
                raise OSError("Dokum SHA-256 uyusmuyor! Beklenen %s, gelen %s" % (expected, sha))
            if expected:
                self.emit(OK, "SHA-256 yan dosyasi (.sha256) ile dogrulandi")
            else:
                self.emit(WARN, "Yan dosya okunamadi, dogrulama atlandi")
        else:
            self.emit(WARN, "Yan dosya (.sha256) yok; yalnizca yerel hesaplama yapildi")
        return {"path": path, "bytes": size, "sha256": sha}

    @staticmethod
    def _sha256_file(path):
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    # -- CLI icin canli yazdirma ------------------------------------------

    def attach_console(self):
        """CLI icin: olaylari ekrana renkli ve canli yazar."""
        def _worker():
            _, q = self.bus.subscribe()
            idle = 0.0
            while True:
                try:
                    ev = q.get(timeout=0.4)
                except Exception:
                    if self._finished_evt.is_set() and q.empty():
                        break
                    idle += 0.4
                    if idle > 3600:
                        break
                    continue
                idle = 0.0
                _print_event(ev)
        t = threading.Thread(target=_worker, name="dbf-console", daemon=True)
        t.start()
        return t


_COLORS = {
    PLAN: "\033[36m", STAGE: "\033[1;36m", INFO: "\033[0m", OK: "\033[32m",
    WARN: "\033[33m", ERROR: "\033[31m", CMD: "\033[90m", OUT: "\033[37m",
    ERR: "\033[31m", PROGRESS: "\033[35m", RESULT: "\033[1;37m",
}


def _print_event(ev):
    color = _COLORS.get(ev["level"], "")
    plain = ev["level"] in (PLAN, RESULT, STAGE)
    prefix = {
        PLAN: "PLAN", STAGE: "", INFO: "  -", OK: " OK", WARN: " ! ", ERROR: "XX ",
        CMD: "  $", OUT: "  |", ERR: "  !", PROGRESS: "  ~", RESULT: "",
    }.get(ev["level"], "   ")
    line = ev["message"] if plain else ("%s %s" % (prefix, ev["message"]))
    print("%s%s\033[0m" % (color, line), flush=True)


def _safe(name):
    keep = "-_.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(c if c in keep else "_" for c in (name or "sunucu"))


def _q(text):
    return "'" + str(text).replace("'", "'\\''") + "'"
