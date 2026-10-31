"""DBFerry MCP sunucusu.

Ajanlara (Claude, Cursor, Codex ...) PostgreSQL yedekleme ve geri yukleme
yeteneklerini guvenli bir sozlesmeyle acar. Tasarim ilkesi:

    plan et   ->  ne yapilacagini gosterir, hicbir seye dokunmaz
    uygula    ->  yalnizca acik onaydan sonra
    dogrula   ->  sonucu kanitlar

Guvenlik kurallari:
  * DBFERRY_READONLY=1 ise veri degistiren araclar tamamen kapanir.
  * 'replace' modunda hedef veritabani adi onay belirteci olarak istenir.
  * Kimlik bilgileri arac argumanlarina girmez; yerel profil deposundan okunur.
  * Geri yukleme oncesi guvenlik yedegi alinir, aktarimlar SHA-256 ile dogrulanir.

Calistirma:
    python dbf.py mcp
    python dbf.py mcp --transport streamable-http --port 8765
"""

import asyncio
import os
import queue
import threading

from mcp.server.mcpserver import Context, MCPServer

from .. import APP_NAME, __version__
from ..jobs import Job, make_remote
from ..model import JobSpec, VerifyCheck
from ..pg import PgDocker, verify_checks
from ..store import Store

# Bu adimlar veriyi geri donusu olmayacak sekilde degistirir
DESTRUCTIVE_STEPS = ("drop", "restore")
MAX_LOG_LINES = 200

INSTRUCTIONS = """\
DBFerry moves PostgreSQL databases between machines. Use this server when a
database must be backed up, restored or transferred.

Working order:
  1. db_profiles  - discover the configured server profiles
  2. db_inspect   - read-only health check of a target (databases, size,
                    table count, migration version, free disk)
  3. db_plan      - list the exact steps an operation would take, with risk flags
  4. db_backup    - dump a database to a local file (read-only, always allowed)
  5. db_restore   - restore a dump into a target (destructive, gated)
  6. db_verify    - prove the result: table count, migration version, SQL checks

Restore is destructive and refuses to run until you explicitly opt in:
dry_run defaults to true, and mode='replace' additionally requires
confirm=<target database name>. A safety backup is taken before every restore.
Never pass credentials: connections come from the locally stored profiles.
"""

server = MCPServer(
    name="dbferry",
    title=APP_NAME,
    version=__version__,
    instructions=INSTRUCTIONS,
)

_JOB_LOCK = threading.Lock()
_JOBS = {}


# ---------------------------------------------------------------------------
# Ortam ve profil yardimcilari
# ---------------------------------------------------------------------------

def _data_dir():
    """Veri klasoru: DBFERRY_DATA_DIR tanimliysa o, degilse calisma dizinindeki 'data'.

    Calisma dizinine gore cozulur; boylece pip/uvx ile kurulmus bir paket
    site-packages icine yazmaya calismaz.
    """
    return os.path.abspath(os.environ.get("DBFERRY_DATA_DIR") or os.path.join(os.getcwd(), "data"))


def _store():
    return Store(_data_dir())


def _readonly():
    raw = str(os.environ.get("DBFERRY_READONLY", "")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _guard_readonly(what):
    if _readonly():
        raise ValueError(
            "Salt okunur mod etkin (DBFERRY_READONLY=1); '%s' veri degistirdigi icin "
            "bu sunucuda kapalidir." % what)


def _find_profile(target):
    profiles = _store().load_profiles()
    for p in profiles:
        if p.id == target or p.name == target:
            return p
    known = ", ".join("%s (%s)" % (p.name, p.id) for p in profiles) or "kayitli profil yok"
    raise ValueError("Profil bulunamadi: %r. Bilinen profiller: %s" % (target, known))


def _db_name(database, profile):
    return (database or "").strip() or profile.db_name


def _apply_local(spec, profile, database):
    """Yerel profil bilgilerini is tanimina tasir (file_to_local / local_to_file)."""
    spec.local_db_host = profile.db_host
    spec.local_db_port = profile.db_port
    spec.local_db_user = profile.db_user
    spec.local_db_password = profile.db_password
    spec.local_pg_mode = profile.pg_mode
    spec.local_pg_container = profile.pg_container
    spec.local_pg_bin = profile.pg_bin


def _backup_spec(profile, database, out_dir, stop_services):
    spec = JobSpec(
        type="server_to_file",
        source_profile=profile.id,
        source_db=database,
        local_copy_dir=out_dir,
        stop_source_services=stop_services,
    )
    if profile.is_local():
        spec.type = "local_to_file"
        _apply_local(spec, profile, database)
    return spec


def _restore_spec(profile, database, dump, mode, new_db_name, take_safety_backup,
                  stop_services, start_services, verify, expect_migration, queries):
    spec = JobSpec(
        type="file_to_server",
        target_profile=profile.id,
        source_dump=os.path.abspath(dump) if dump else "",
        target_db=database,
        mode=mode,
        new_db_name=new_db_name,
        take_safety_backup=take_safety_backup,
        stop_services=stop_services,
        start_services=start_services,
        verify=verify,
        expect_migration=expect_migration,
        verify_queries=queries,
    )
    if profile.is_local():
        spec.type = "file_to_local"
        _apply_local(spec, profile, database)
    return spec


# ---------------------------------------------------------------------------
# Is calistirma yardimcilari
# ---------------------------------------------------------------------------

def _register(job):
    with _JOB_LOCK:
        _JOBS[job.id] = job
    return job


def _line(ev):
    return "[%s] %s" % (ev.get("level", "?"), ev.get("message", ""))


def _format_events(events, limit=MAX_LOG_LINES):
    return [_line(ev) for ev in events[-limit:]]


def _drain(q, lines):
    while True:
        try:
            ev = q.get_nowait()
        except queue.Empty:
            return
        lines.append(_line(ev))


async def _publish_progress(job, ctx):
    """Is adimlarini MCP ilerleme bildirimine cevirir (istemci desteklemiyorsa sessizce gecilir)."""
    state = job.to_dict()
    steps = state.get("plan") or []
    done = sum(1 for s in steps if s.get("status") == "tamam")
    try:
        await ctx.report_progress(progress=done, total=len(steps) or None,
                                  message=job.stage or state.get("status") or "")
    except Exception:
        pass


async def _drive(job, ctx, start):
    """Isi arka planda baslatir, olaylari toplar ve ilerlemeyi canli bildirir."""
    snapshot, q = job.bus.subscribe()
    lines = _format_events(snapshot)
    start()
    while job.status in ("hazir", "calisiyor"):
        _drain(q, lines)
        await _publish_progress(job, ctx)
        await asyncio.sleep(0.15)
    _drain(q, lines)
    await _publish_progress(job, ctx)
    return lines[-MAX_LOG_LINES:]


def _job_result(job, lines):
    return {
        "job_id": job.id,
        "status": job.status,
        "ok": job.status == "tamam",
        "summary": job.summary_text or job.spec.summary(),
        "error": job.error or None,
        "steps": job.to_dict().get("plan") or [],
        "details": job.result,
        "log": lines,
    }


# ---------------------------------------------------------------------------
# Araclar
# ---------------------------------------------------------------------------

@server.tool()
def db_profiles() -> dict:
    """Kayitli sunucu profillerini listeler. Sifreler asla dondurulmez."""
    profiles = _store().load_profiles()
    return {
        "data_dir": _data_dir(),
        "read_only": _readonly(),
        "profiles": [
            {
                "id": p.id,
                "name": p.name,
                "location": p.location,
                "target": "yerel makine" if p.is_local() else "%s@%s:%s" % (p.user, p.host, p.port),
                "pg_mode": p.pg_mode,
                "container": p.pg_container if p.pg_mode == "docker" else None,
                "database": p.db_name,
                "schema": p.db_schema,
                "stop_containers": p.stop_list(),
                "start_containers": p.start_list(),
            }
            for p in profiles
        ],
    }


@server.tool()
async def db_inspect(target: str, ctx: Context) -> dict:
    """Salt okunur ortam denetimi: veritabani listesi, boyut, tablo sayisi, migration surumu ve disk.

    Hicbir seyi degistirmez; her islemden once bununla baslayin.
    """
    profile = _find_profile(target)
    spec = JobSpec(type="server_to_file", source_profile=profile.id, verify=False)
    job = _register(Job(spec, _store(), title="Ortam Denetimi"))
    lines = await _drive(job, ctx, job.inspect_async)
    return _job_result(job, lines)


@server.tool()
def db_plan(target: str, database: str = "", operation: str = "restore",
            mode: str = "replace") -> dict:
    """Bir islemin adimlarini ve risklerini listeler; hicbir seye dokunmaz.

    operation: 'backup' | 'restore'. mode: 'replace' (mevcut veritabani silinir)
    | 'new_db' (mevcut veritabaniya dokunulmaz).
    """
    profile = _find_profile(target)
    db = _db_name(database, profile)
    if operation == "backup":
        spec = _backup_spec(profile, db, "", False)
    elif operation == "restore":
        if mode not in ("replace", "new_db"):
            raise ValueError("mode 'replace' veya 'new_db' olmali (gelen: %r)" % mode)
        spec = _restore_spec(profile, db, "", mode, "", True, True, True, True, "", "")
    else:
        raise ValueError("operation 'backup' veya 'restore' olmali (gelen: %r)" % operation)

    steps = Job(spec, _store(), title="Plan").plan()
    destructive = [s["key"] for s in steps if s["key"] in DESTRUCTIVE_STEPS]
    return {
        "operation": operation,
        "target": profile.name,
        "database": db,
        "local": profile.is_local(),
        "summary": spec.summary(),
        "steps": steps,
        "destructive_steps": destructive,
        "requires_confirmation": bool(destructive),
        "read_only": _readonly(),
    }


@server.tool()
async def db_backup(target: str, ctx: Context, database: str = "", out_dir: str = "",
                    stop_services: bool = False) -> dict:
    """Veritabanini yerel diske yedekler (pg_dump -Fc + SHA-256). Salt okunur; her modda calisir.

    database bos birakilirsa profil varsayilani kullanilir. stop_services=true ise
    yedek oncesi servisler durdurulur ve sonrasinda geri baslatilir.
    """
    profile = _find_profile(target)
    if not profile.is_local() and not profile.host:
        raise ValueError("Profil uzak ancak sunucu adresi bos: %s" % profile.name)
    db = _db_name(database, profile)
    spec = _backup_spec(profile, db, out_dir, stop_services)
    job = _register(Job(spec, _store(), title="Yedek"))
    lines = await _drive(job, ctx, job.run_async)
    return _job_result(job, lines)


@server.tool()
async def db_restore(target: str, ctx: Context, dump: str = "", database: str = "",
                     mode: str = "replace", new_db_name: str = "", dry_run: bool = True,
                     confirm: str = "", stop_services: bool = True, start_services: bool = True,
                     take_safety_backup: bool = True, verify: bool = True,
                     expect_migration: str = "", queries: list[str] | None = None) -> dict:
    """Yedek dosyasini hedefe geri yukler. Varsayilan olarak yalnizca PLAN dondurur.

    Calistirmak icin dry_run=false gerekir. mode='replace' mevcut veritabaniyi
    sildigi icin ayrica confirm=<hedef veritabani adi> istenir. Geri yukleme
    oncesi otomatik guvenlik yedegi alinir.
    """
    _guard_readonly("db_restore")
    profile = _find_profile(target)
    if mode not in ("replace", "new_db"):
        raise ValueError("mode 'replace' veya 'new_db' olmali (gelen: %r)" % mode)
    db = _db_name(database, profile)
    spec = _restore_spec(profile, db, dump, mode, new_db_name, take_safety_backup,
                         stop_services, start_services, verify, expect_migration,
                         "\n".join(queries or []))
    job = _register(Job(spec, _store(), title="Geri yukleme"))

    if dry_run:
        return {
            "dry_run": True,
            "executed": False,
            "target": profile.name,
            "database": db,
            "mode": mode,
            "summary": spec.summary(),
            "steps": job.plan(),
            "next": "Calistirmak icin dry_run=false gonderin"
                    + ("; mode='replace' oldugu icin confirm='%s' de gerekir." % db),
        }

    if not dump:
        raise ValueError("Geri yuklemek icin 'dump' (yedek dosyasi yolu) zorunlu")
    if not os.path.isfile(os.path.abspath(dump)):
        raise ValueError("Yedek dosyasi bulunamadi: %s" % dump)
    if mode == "replace" and confirm != db:
        raise ValueError(
            "Onay gerekli: mode='replace' mevcut '%s' veritabanini silip yeniden olusturur. "
            "Onaylamak icin confirm='%s' gonderin." % (db, db))

    lines = await _drive(job, ctx, job.run_async)
    return _job_result(job, lines)


@server.tool()
async def db_verify(target: str, ctx: Context, database: str = "",
                    queries: list[str] | None = None, expect_migration: str = "") -> dict:
    """Sonucu kanitlar: veritabani varligi, boyut, tablo sayisi, migration surumu ve SQL kontrolleri.

    queries bicimi: "SQL | beklenen_deger" (beklenen deger opsiyoneldir).
    """
    profile = _find_profile(target)
    db = _db_name(database, profile)
    return await asyncio.to_thread(_verify_blocking, profile, db,
                                   "\n".join(queries or []), expect_migration)


def _verify_blocking(profile, database, checks_text, expect_migration):
    checks = VerifyCheck.parse_multiline(checks_text)
    checks.append(VerifyCheck(
        sql="select count(*) from information_schema.tables where table_schema='%s'" % profile.db_schema,
        label="sema tablo sayisi"))

    silent = lambda *a, **k: None
    remote = make_remote(profile, log=silent, should_stop=lambda: False)
    try:
        remote.connect()
        engine = PgDocker(remote, profile, silent)
        engine.ping()
        out = {
            "profile": profile.name,
            "database": database,
            "schema": profile.db_schema,
            "exists": engine.database_exists(database),
            "size": engine.size_pretty(database),
            "tables": engine.table_count(profile.db_schema),
            "migration": engine.migration_version(profile.db_schema) or None,
        }
        if expect_migration:
            out["migration_expected"] = expect_migration
            out["migration_ok"] = out["migration"] == expect_migration
        ok_list, bad_list = verify_checks(engine, checks, silent)
        out["checks_ok"] = ok_list
        out["checks_failed"] = bad_list
        out["ok"] = not bad_list and bool(out["exists"])
        return out
    finally:
        remote.close()


@server.tool()
def db_job_status(job_id: str = "", limit: int = 40) -> dict:
    """Calisan ve gecmis islerin durumunu ve son log satirlarini dondurur."""
    if job_id:
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
        if job is not None:
            return _job_result(job, _format_events(job.bus.snapshot(), limit))
        for item in _store().load_history(500):
            if item.get("id") == job_id:
                return {
                    "job_id": job_id,
                    "status": item.get("status"),
                    "ok": item.get("status") == "tamam",
                    "summary": item.get("summary"),
                    "finished_at": item.get("finished_at"),
                    "source": "history",
                }
        raise ValueError("Is bulunamadi: %r" % job_id)

    with _JOB_LOCK:
        jobs = list(_JOBS.values())
    return {
        "active": [
            {"job_id": j.id, "status": j.status, "summary": j.summary_text or j.spec.summary()}
            for j in jobs if j.status in ("hazir", "calisiyor")
        ],
        "recent": [
            {"job_id": i.get("id"), "status": i.get("status"), "summary": i.get("summary")}
            for i in _store().load_history(limit)
        ],
    }


# ---------------------------------------------------------------------------
# Giris noktasi
# ---------------------------------------------------------------------------

TRANSPORTS = ("stdio", "sse", "streamable-http")


def main(transport="stdio", host="127.0.0.1", port=8765):
    if transport not in TRANSPORTS:
        raise ValueError("Bilinmeyen tasima: %r (%s)" % (transport, " | ".join(TRANSPORTS)))
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run(transport, host=host, port=port)


if __name__ == "__main__":
    main()
