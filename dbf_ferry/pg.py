"""PostgreSQL motoru.

Docker konteyneri icindeki ya da dogrudan sunucuda calisan bir PostgreSQL
kurulumunda yedek alma, geri yukleme ve dogrulama islemlerini yurutur.

Konteyner icinde uretilen dosyalar 'docker cp' ile sunucu dosya sistemine
tasinir; boylece SFTP ile indirilebilir / yuklenebilir hale gelir.
"""

import re
import uuid

from .events import INFO, OK, WARN
from .localx import to_posix
from .sshx import _shq

IDENT_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
CONTAINER_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class PgError(RuntimeError):
    pass


def assert_ident(name, what="tanimlayici"):
    if not IDENT_RX.match(name or ""):
        raise PgError("Gecersiz %s: %r (yalnizca harf, rakam, _ ve $ kullanilabilir)" % (what, name))
    return name


def assert_container(name):
    if not CONTAINER_RX.match(name or ""):
        raise PgError("Gecersiz konteyner adi: %r" % name)
    return name


class PgDocker(object):
    """Bir PostgreSQL kurulumuyla konusan motor (uzak sunucu ya da yerel makine)."""

    def __init__(self, remote, profile, log):
        self.r = remote
        self.p = profile
        self._log = log

    # -- ic yardimcilar ------------------------------------------------------

    @property
    def docker(self):
        return self.p.pg_mode == "docker"

    def path(self, host_path):
        """Yerel makinede kabuga POSIX bicimi verilir; uzakta degismez."""
        if self.p.is_local():
            return to_posix(host_path)
        return host_path

    def _env(self):
        return "env PGPASSWORD=%s " % _shq(self.p.db_password) if self.p.db_password else ""

    def _conn_args(self):
        return "-h %s -p %s -U %s" % (_shq(self.p.db_host), self.p.db_port, _shq(self.p.db_user))

    def _psql_prefix(self, dbname):
        assert_ident(dbname, "veritabani adi")
        if self.docker:
            assert_container(self.p.pg_container)
            return "docker exec %s %spsql -v ON_ERROR_STOP=1 -U %s -d %s" % (
                _shq(self.p.pg_container), self._env(), _shq(self.p.db_user), _shq(dbname))
        return "%spsql -v ON_ERROR_STOP=1 %s -d %s" % (self._env(), self._conn_args(), _shq(dbname))

    def _container_tmp(self, tag):
        return "/tmp/dbf_%s_%s.dump" % (tag, uuid.uuid4().hex[:8])

    # -- sorgular ------------------------------------------------------------

    def psql(self, sql, dbname=None, tuples_only=True):
        dbname = dbname or self.p.db_name
        flags = "-Atc" if tuples_only else "-c"
        cmd = "%s %s %s" % (self._psql_prefix(dbname), flags, _shq(sql))
        code, out, err = self.r.run(cmd, check=False, quiet=True)
        if code != 0:
            raise PgError("Sorgu basarisiz: %s\n  %s" % (sql, (err or out).strip()))
        return out.strip()

    def scalar(self, sql, dbname=None):
        val = self.psql(sql, dbname=dbname)
        return val.splitlines()[0].strip() if val else ""

    # -- ortam bilgisi -------------------------------------------------------

    def ping(self):
        if self.docker:
            state = self.r.capture(
                "docker inspect -f '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}yok{{end}}' %s 2>/dev/null || echo yok"
                % _shq(self.p.pg_container))
            if state.startswith("yok"):
                raise PgError("PostgreSQL konteyneri bulunamadi: %s" % self.p.pg_container)
            status, _, health = state.partition("|")
            self._log(INFO, "Konteyner %s: durum=%s saglik=%s" % (self.p.pg_container, status, health))
            if status != "running":
                raise PgError("Konteyner calismiyor: %s (%s)" % (self.p.pg_container, status))
        version = self.scalar("select version()")
        self._log(OK, version.split(",")[0] if version else "PostgreSQL surumu okunamadi")
        return version

    def database_exists(self, dbname):
        assert_ident(dbname, "veritabani adi")
        return self.scalar(
            "select 1 from pg_database where datname='%s'" % dbname, dbname="postgres") == "1"

    def list_databases(self):
        raw = self.psql(
            "select datname from pg_database where datistemplate=false order by datname",
            dbname="postgres")
        return [d for d in raw.splitlines() if d.strip()]

    def size_pretty(self, dbname):
        try:
            return self.scalar("select pg_size_pretty(pg_database_size('%s'))" % dbname, dbname="postgres")
        except PgError:
            return "?"

    def migration_version(self, schema):
        if not schema:
            return ""
        try:
            return self.scalar("select max(version) from %s.schema_migrations" % assert_ident(schema, "sema"))
        except PgError as exc:
            self._log(WARN, "schema_migrations okunamadi: %s" % exc)
            return ""

    def table_count(self, schema):
        return self.scalar(
            "select count(*) from information_schema.tables where table_schema='%s'" % schema)

    # -- yedek alma ----------------------------------------------------------

    def dump(self, host_path, dbname=None):
        """Veritabanini custom-format (-Fc) olarak host_path'e yazar."""
        dbname = dbname or self.p.db_name
        assert_ident(dbname, "veritabani adi")
        common = "-Fc --no-owner --no-privileges"
        if self.docker:
            ct = self._container_tmp("dump")
            self.r.run("docker exec %s %spg_dump -U %s -d %s %s -f %s" % (
                _shq(self.p.pg_container), self._env(), _shq(self.p.db_user),
                _shq(dbname), common, _shq(ct)), timeout=7200)
            self.r.run("docker cp %s %s" % (_shq("%s:%s" % (self.p.pg_container, ct)), _shq(host_path)))
            self.r.run("docker exec %s rm -f %s" % (_shq(self.p.pg_container), _shq(ct)), check=False)
        else:
            self.r.run("%spg_dump %s -d %s %s -f %s" % (
                self._env(), self._conn_args(), _shq(dbname), common,
                _shq(self.path(host_path))), timeout=7200)
        size = self.r.capture("stat -c %%s %s" % _shq(self.path(host_path)))
        return host_path, int(size or 0)

    def sha256(self, host_path):
        return self.r.capture(
            "sha256sum %s | awk '{print $1}'" % _shq(self.path(host_path))).strip()

    def inspect_dump(self, host_path):
        """Dokum icindeki nesne sayisini ve ilk satirlari dondurur (bilgi amacli)."""
        if not self.docker:
            posix = self.path(host_path)
            raw = self.r.capture("pg_restore -l %s 2>/dev/null | head -40" % _shq(posix))
            count = self.r.capture("pg_restore -l %s 2>/dev/null | wc -l" % _shq(posix))
            return {"entries": count.strip(), "preview": raw}
        ct = self._container_tmp("inspect")
        try:
            self.r.run("docker cp %s %s" % (_shq(host_path), _shq("%s:%s" % (self.p.pg_container, ct))),
                       check=False, quiet=True)
            count = self.r.capture(
                "docker exec %s pg_restore -l %s 2>/dev/null | wc -l" % (_shq(self.p.pg_container), _shq(ct)))
            preview = self.r.capture(
                "docker exec %s pg_restore -l %s 2>/dev/null | head -25" % (_shq(self.p.pg_container), _shq(ct)))
            return {"entries": count.strip(), "preview": preview}
        finally:
            self.r.run("docker exec %s rm -f %s" % (_shq(self.p.pg_container), _shq(ct)), check=False, quiet=True)

    # -- geri yukleme --------------------------------------------------------

    def terminate_connections(self, dbname):
        assert_ident(dbname, "veritabani adi")
        sql = ("select pg_terminate_backend(pid) from pg_stat_activity "
               "where datname='%s' and pid<>pg_backend_pid()" % dbname)
        try:
            killed = self.psql(sql, dbname="postgres")
            n = len([x for x in killed.splitlines() if x])
            if n:
                self._log(INFO, "%s veritabanina acik %d oturum sonlandirildi" % (dbname, n))
        except PgError as exc:
            self._log(WARN, "Oturumlar sonlandirilamadi: %s" % exc)

    def drop_database(self, dbname):
        assert_ident(dbname, "veritabani adi")
        if self.docker:
            cmd = "docker exec %s %sdropdb -U %s --if-exists --force %s" % (
                _shq(self.p.pg_container), self._env(), _shq(self.p.db_user), _shq(dbname))
        else:
            cmd = "%sdropdb %s --if-exists --force %s" % (self._env(), self._conn_args(), _shq(dbname))
        self.r.run(cmd)

    def create_database(self, dbname, owner=None):
        assert_ident(dbname, "veritabani adi")
        owner = owner or self.p.db_user
        assert_ident(owner, "sahip kullanicisi")
        if self.docker:
            cmd = "docker exec %s %screatedb -U %s -O %s %s" % (
                _shq(self.p.pg_container), self._env(), _shq(self.p.db_user), _shq(owner), _shq(dbname))
        else:
            cmd = "%screatedb %s -O %s %s" % (self._env(), self._conn_args(), _shq(owner), _shq(dbname))
        self.r.run(cmd)

    def restore(self, host_path, dbname):
        assert_ident(dbname, "veritabani adi")
        common = "--no-owner --no-privileges --exit-on-error --single-transaction"
        if self.docker:
            ct = self._container_tmp("restore")
            self.r.run("docker cp %s %s" % (_shq(host_path), _shq("%s:%s" % (self.p.pg_container, ct))))
            try:
                self.r.run("docker exec %s %spg_restore -U %s -d %s %s %s" % (
                    _shq(self.p.pg_container), self._env(), _shq(self.p.db_user),
                    _shq(dbname), common, _shq(ct)), timeout=7200)
            finally:
                self.r.run("docker exec %s rm -f %s" % (_shq(self.p.pg_container), _shq(ct)), check=False)
        else:
            self.r.run("%spg_restore %s -d %s %s %s" % (
                self._env(), self._conn_args(), _shq(dbname), common,
                _shq(self.path(host_path))), timeout=7200)


def stop_containers(remote, names, log):
    if not names:
        return []
    log(INFO, "Durdurulacak servisler: %s" % ", ".join(names))
    remote.run("docker stop %s >/dev/null 2>&1; echo tamam" % " ".join(_shq(n) for n in names),
               check=False)
    stopped = []
    for name in names:
        running = remote.capture("docker inspect -f '{{.State.Running}}' %s 2>/dev/null || echo yok" % _shq(name))
        log(INFO, "  %s -> Running=%s" % (name, running or "?"))
        if running == "false":
            stopped.append(name)
    return stopped


def start_containers(remote, names, log, wait_healthy=True, timeout=60):
    """Konteynerleri baslatir.

    Yalnizca konteynerin 'running' durumuna gelmesi beklenir; saglik (health)
    durumu bilgi amaciyla bildirilir. Saglik kontrolu basarisiz olan servisler
    (orn. bagimli oldugu baska servis kapali) sonsuza kadar beklenirse is bosa
    kilitlenir; bu nedenle saglikli hale gelmesi beklenmez.
    """
    import time

    if not names:
        return []
    log(INFO, "Baslatilacak servisler: %s" % ", ".join(names))
    remote.run("docker start %s >/dev/null 2>&1; echo tamam" % " ".join(_shq(n) for n in names), check=False)

    deadline = time.time() + timeout
    pending = list(names)
    while pending and time.time() < deadline:
        time.sleep(3)
        still = []
        for name in pending:
            state = remote.capture(
                "docker inspect -f '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}yok{{end}}' %s 2>/dev/null || echo 'yok|yok'"
                % _shq(name))
            status, _, health = state.partition("|")
            if status == "yok":
                log(WARN, "%s -> konteyner bulunamadi" % name)
            elif status != "running":
                log(INFO, "%s -> %s (bekleniyor)" % (name, status or "?"))
                still.append(name)
            elif health == "healthy":
                log(OK, "%s -> calisiyor, saglikli" % name)
            elif health == "yok":
                log(OK, "%s -> calisiyor" % name)
            elif health == "unhealthy":
                log(WARN, "%s -> calisti ancak sagliksiz (unhealthy)" % name)
            else:
                log(INFO, "%s -> calisti, saglik: %s" % (name, health))
        pending = still
    for name in pending:
        log(WARN, "%s 'running' durumuna gelmedi (%.0f sn)" % (name, timeout))
    return names


def verify_checks(engine, checks, log):
    """Beklenen degerleri kontrol eder. (basarili, basarisiz) dondurur."""
    ok_list, bad_list = [], []
    for chk in checks:
        try:
            got = engine.psql(chk.sql)
            got_first = got.splitlines()[0].strip() if got else ""
        except PgError as exc:
            bad_list.append("%s -> SORGU HATASI: %s" % (chk.sql, exc))
            log(WARN, "Dogrulama sorgusu hata verdi: %s" % chk.sql)
            continue
        label = chk.label or chk.sql
        if chk.expected and got_first != chk.expected.strip():
            bad_list.append("%s -> beklenen '%s', gelen '%s'" % (label, chk.expected.strip(), got_first))
            log(WARN, "Uyusmazlik: %s (beklenen %s, gelen %s)" % (label, chk.expected.strip(), got_first))
        else:
            ok_list.append("%s -> %s" % (label, got_first))
            log(OK, "%s = %s" % (label, got_first))
    return ok_list, bad_list
