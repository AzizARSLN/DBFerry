"""DBFerry web katmani.

Yerel bir HTTP sunucusu acar: yapilandirma ekrani (statik arayuz), profil
yonetimi ve is yurutme API'si. Is loglari Server-Sent Events ile canli akar.
Yalnizca Python standart kutuphanesi + paramiko kullanilir.
"""

import json
import os
import queue
import socket
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import APP_NAME, APP_TAGLINE, __version__
from ..jobs import Job
from ..localx import LocalError, LocalRemote
from ..model import JOB_TYPES, RESTORE_MODES, VERIFY_QUERY_HELP, JobSpec, ServerProfile
from ..pg import PgDocker, PgError
from ..sshx import SSHError, fetch_hostkey
from ..store import ConfigError, Store

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # dbf_ferry/
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)                                   # DBFerry/
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_DATA_DIR = os.path.join(PROJECT_DIR, "data")


class Registry(object):
    """Calisan ve bitmis isleri tutar."""

    def __init__(self, store):
        self.store = store
        self.jobs = {}
        self._lock = threading.Lock()

    def add(self, job):
        with self._lock:
            self.jobs[job.id] = job
        return job

    def get(self, jid):
        return self.jobs.get(jid)

    def active(self):
        with self._lock:
            return [j for j in self.jobs.values() if j.status in ("hazir", "calisiyor")]

    def recent(self, n=20):
        with self._lock:
            items = sorted(self.jobs.values(), key=lambda j: j.started_at or 0, reverse=True)
        return items[:n]


class Handler(BaseHTTPRequestHandler):
    server_version = "DBFerry/%s" % __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def log_error(self, fmt, *args):
        return

    # -- yardimcilar ------------------------------------------------------

    @property
    def reg(self):
        return self.server.registry

    @property
    def store(self):
        return self.server.registry.store

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, message, code=400):
        self._json({"ok": False, "error": str(message)}, code)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("Gecersiz JSON govdesi")

    def _static(self, rel):
        path = os.path.normpath(os.path.join(STATIC_DIR, rel.lstrip("/")))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self.send_error(404, "Bulunamadi")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- GET --------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/meta":
                return self._json({
                    "app": APP_NAME,
                    "tagline": APP_TAGLINE,
                    "version": __version__,
                    "job_types": JOB_TYPES,
                    "restore_modes": RESTORE_MODES,
                    "verify_help": VERIFY_QUERY_HELP,
                    "data_dir": self.store.root,
                })
            if path == "/api/profiles":
                return self._json({"ok": True,
                                   "profiles": [p.safe() for p in self.store.load_profiles()]})
            if path == "/api/history":
                return self._json({"ok": True, "items": self.store.load_history(30)})
            if path == "/api/fs":
                query = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                raw = (query.get("path") or [""])[0]
                return self._json(self._fs_list(raw))
            if path == "/api/jobs":
                return self._json({"ok": True, "jobs": [j.to_dict() for j in self.reg.recent(15)]})
            if path.startswith("/api/jobs/"):
                rest = path[len("/api/jobs/"):]
                jid, _, action = rest.partition("/")
                job = self.reg.get(jid)
                if not job:
                    return self._err("Is bulunamadi: %s" % jid, 404)
                if action == "events":
                    return self._sse(job)
                return self._json({"ok": True, "job": job.to_dict()})
            return self._err("Bilinmeyen yol: %s" % path, 404)
        except (ConfigError, ValueError) as exc:
            return self._err(exc)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            return self._err("Sunucu hatasi: %s" % exc, 500)

    # -- POST -------------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            data = self._body()
            if path == "/api/profiles":
                saved = self.store.upsert_profile(data)
                return self._json({"ok": True, "id": saved.id, "profile": saved.safe()})
            if path == "/api/profiles/delete":
                ok = self.store.delete_profile(data.get("id", ""))
                return self._json({"ok": ok})
            if path == "/api/hostkey":
                info = fetch_hostkey(data.get("host", ""), int(data.get("port") or 22))
                return self._json({"ok": True, **info})
            if path == "/api/local/dbs":
                return self._json(self._local_databases(data))
            if path == "/api/plan":
                job = Job(JobSpec.from_dict(data.get("spec") or {}), self.store)
                return self._json({"ok": True, "plan": job.plan(), "summary": job.spec.summary()})
            if path == "/api/inspect":
                job = self.reg.add(Job(JobSpec.from_dict(data.get("spec") or {}), self.store,
                                       title="Ortam Denetimi"))
                job.inspect_async()
                return self._json({"ok": True, "job_id": job.id})
            if path == "/api/run":
                job = self.reg.add(Job(JobSpec.from_dict(data.get("spec") or {}), self.store))
                job.run_async()
                return self._json({"ok": True, "job_id": job.id})
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                jid = path[len("/api/jobs/"):-len("/cancel")]
                job = self.reg.get(jid)
                if not job:
                    return self._err("Is bulunamadi", 404)
                job.cancel()
                return self._json({"ok": True})
            return self._err("Bilinmeyen yol: %s" % path, 404)
        except (ConfigError, ValueError, SSHError, LocalError, PgError) as exc:
            return self._err(exc)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            return self._err("Sunucu hatasi: %s" % exc, 500)

    # -- SSE --------------------------------------------------------------

    def _sse(self, job):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        snapshot, q = job.bus.subscribe()
        try:
            for ev in snapshot:
                self._sse_write(ev)
            while True:
                try:
                    ev = q.get(timeout=1.0)
                except queue.Empty:
                    if job.status not in ("hazir", "calisiyor"):
                        while not q.empty():
                            self._sse_write(q.get_nowait())
                        break
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(ev)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            job.bus.unsubscribe(q)

    def _sse_write(self, ev):
        payload = json.dumps(ev, ensure_ascii=False, default=str)
        self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
        self.wfile.flush()

    # -- yerel PostgreSQL --------------------------------------------------

    @staticmethod
    def _local_databases(data):
        """Is ekraninda girilen bilgilerle yerel PostgreSQL'e baglanip DB'leri listeler."""
        prof = ServerProfile.from_dict({
            "location": "local",
            "name": data.get("name") or "Yerel PostgreSQL",
            "pg_mode": data.get("pg_mode") or "local",
            "pg_container": data.get("pg_container") or "postgres",
            "pg_bin": data.get("pg_bin") or "",
            "db_host": data.get("db_host") or "127.0.0.1",
            "db_port": data.get("db_port") or 5432,
            "db_user": data.get("db_user") or "postgres",
            "db_password": data.get("db_password") or "",
            "db_name": data.get("db_name") or "postgres",
        })
        r = LocalRemote(prof)
        r.connect()
        try:
            engine = PgDocker(r, prof, lambda *a, **k: None)
            version = engine.ping() or ""
            dbs = engine.list_databases()
        finally:
            r.close()
        return {
            "ok": True,
            "databases": dbs,
            "version": version.split(",")[0],
            "mode": "docker konteyneri (%s)" % prof.pg_container if prof.pg_mode == "docker" else "dogrudan PostgreSQL",
        }

    # -- yerel dosya gezgini ----------------------------------------------

    @staticmethod
    def _fs_list(raw_path):
        path = urllib.parse.unquote(raw_path or "").strip()
        if not path:
            path = os.path.expanduser("~")
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(path):
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            path = os.path.expanduser("~")
        dirs, files = [], []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False),
                                                              e.name.lower()))
        except OSError as exc:
            raise ValueError("Klasor okunamadi (%s): %s" % (path, exc))
        for e in entries[:600]:
            try:
                if e.is_dir(follow_symlinks=False):
                    dirs.append({"name": e.name, "path": e.path})
                elif e.is_file(follow_symlinks=False):
                    files.append({"name": e.name, "path": e.path, "size": _fmt_size(e.stat().st_size)})
            except OSError:
                continue
        parent = os.path.dirname(path.rstrip("\\/"))
        if parent == path:
            parent = ""
        return {"ok": True, "path": path, "parent": parent, "dirs": dirs, "files": files}


def _fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def create_server(host="127.0.0.1", port=8787, data_dir=None):
    store = Store(data_dir or DEFAULT_DATA_DIR)
    registry = Registry(store)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.registry = registry
    return httpd


def serve(host="127.0.0.1", port=8787, data_dir=None, open_browser=True, quiet=False):
    httpd = create_server(host, port, data_dir)
    url = "http://%s:%d/" % (host, httpd.server_address[1])
    if not quiet:
        print("\n  %s v%s - %s" % (APP_NAME, __version__, APP_TAGLINE))
        print("  Yapilandirma ekrani: %s" % url)
        print("  Veri klasoru       : %s" % httpd.registry.store.root)
        print("  Durdurmak icin     : Ctrl+C\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Sunucu kapatiliyor...")
    finally:
        httpd.server_close()
    return 0


def free_port(start=8787, host="127.0.0.1", tries=25):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) != 0:
                return port
    return start
