"""Yerel makine katmani.

Remote (SSH) sinifiyla ayni arayuzu sunar; komutlar Git Bash uzerinden
calistirilir. Boylece pg.py icindeki kabuk komutlari (psql, pg_restore,
sha256sum, stat, df) uzak sunucudakiyle birebir ayni sekilde isler.
Dosya transferleri yerel kopyalamaya donusur.
"""

import os
import shutil
import subprocess

from .events import CMD, ERR, INFO, OK, OUT, WARN
from .sshx import _shq

_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

_PG_ROOTS = (
    r"C:\Program Files\PostgreSQL",
    r"C:\Program Files (x86)\PostgreSQL",
)


class LocalError(RuntimeError):
    pass


def find_bash():
    """Git Bash (POSIX kabuk) yolunu bulur."""
    env = (os.environ.get("DBFERRY_BASH") or "").strip()
    if env and os.path.isfile(env):
        return env
    for cand in _BASH_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return shutil.which("bash") or ""


def find_pg_bin():
    """Yerel PostgreSQL bin klasorunu (psql, pg_restore ...) bulur."""
    env = (os.environ.get("DBFERRY_PG_BIN") or "").strip()
    if env and os.path.isdir(env):
        return env
    for root in _PG_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root), reverse=True)
        except OSError:
            continue
        for name in names:
            cand = os.path.join(root, name, "bin")
            if os.path.isfile(os.path.join(cand, "psql.exe")):
                return cand
    exe = shutil.which("psql")
    return os.path.dirname(exe) if exe else ""


def to_posix(path):
    """'C:\\klasor\\dosya.dump' -> '/c/klasor/dosya.dump' (Git Bash icin)."""
    p = str(path or "").strip()
    if not p or p.startswith("/"):
        return p
    p = p.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        rest = p[2:]
        if not rest.startswith("/"):
            rest = "/" + rest
        return "/%s%s" % (p[0].lower(), rest)
    return p


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


class LocalRemote(object):
    """Yerel makinede komut calistiran Remote benzeri oturum."""

    def __init__(self, profile, log=None, should_stop=None):
        self.p = profile
        self._log = log or (lambda *a, **k: None)
        self._should_stop = should_stop or (lambda: False)
        self.hostkey_seen = ""
        self.bash = find_bash()
        self.pg_bin = (getattr(profile, "pg_bin", "") or "").strip() or find_pg_bin()
        docker = shutil.which("docker")
        self.docker_bin = os.path.dirname(docker) if docker else ""
        self._env_prefix = ""

    # -- yasam dongusu -------------------------------------------------------

    def connect(self, timeout=25):
        if not self.bash:
            raise LocalError(
                "Git Bash bulunamadi. Yerel geri yukleme icin Git for Windows gerekir "
                "(ya da DBFERRY_BASH ortam degiskeniyle bash.exe yolunu belirtin).")
        if not self.pg_bin and self.p.pg_mode != "docker":
            raise LocalError(
                "Yerel PostgreSQL bin klasoru bulunamadi (psql/pg_restore). "
                "Profilde 'PostgreSQL bin' alanini doldurun.")
        parts = []
        if self.pg_bin and os.path.isdir(self.pg_bin):
            parts.append(to_posix(self.pg_bin))
        if self.docker_bin and os.path.isdir(self.docker_bin):
            parts.append(to_posix(self.docker_bin))
        if parts:
            self._env_prefix = 'export PATH="%s:$PATH"; ' % ":".join(parts)
        self._log(INFO, "Yerel kabuk: Git Bash (%s)" % self.bash)
        self._log(INFO, "Yerel PostgreSQL bin: %s" % (self.pg_bin or "<yok>"))
        return self

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- komut calistirma ----------------------------------------------------

    def run(self, command, check=True, timeout=3600, quiet=False):
        if self._should_stop():
            raise LocalError("Islem kullanici tarafindan durduruldu")
        if not quiet:
            self._log(CMD, command)
        env = os.environ.copy()
        env.setdefault("LANG", "C.UTF-8")
        try:
            proc = subprocess.run(
                [self.bash, "-lc", self._env_prefix + command],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, cwd=os.path.expanduser("~"), env=env,
                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise LocalError("Yerel komut zaman asimina ugradi (%.0f sn): %s" % (timeout, command))
        except OSError as exc:
            raise LocalError("Yerel komut calistirilamadi: %s" % exc)
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
        if not quiet:
            for line in out.splitlines():
                if line.strip():
                    self._log(OUT, line)
            for line in err.splitlines():
                if line.strip():
                    self._log(ERR, line)
        if check and code != 0:
            detail = (err or out).strip().splitlines()
            raise LocalError("Yerel komut basarisiz (exit=%d): %s%s" % (
                code, command, ("\n  -> " + detail[-1]) if detail else ""))
        return code, out, err

    def capture(self, command, timeout=300):
        code, out, err = self.run(command, check=False, quiet=True, timeout=timeout)
        return out.strip()

    def exists(self, path):
        return self.capture("test -e %s && echo VAR || echo YOK" % _shq(to_posix(path))) == "VAR"

    def free_space(self, directory=""):
        path = directory or os.path.expanduser("~")
        for _ in range(8):
            if os.path.exists(path):
                break
            parent = os.path.dirname(str(path).rstrip("\\/"))
            if not parent or parent == path:
                break
            path = parent
        try:
            return _human(shutil.disk_usage(path).free)
        except OSError:
            return "?"

    # -- transfer (yerelde kopyalama) ---------------------------------------

    def upload(self, local_path, remote_path, stage=None):
        return self._copy(local_path, remote_path, stage)

    def download(self, remote_path, local_path, stage=None):
        return self._copy(remote_path, local_path, stage)

    def _copy(self, src, dst, stage=None):
        if self._should_stop():
            raise LocalError("Islem kullanici tarafindan durduruldu")
        if not os.path.isfile(src):
            raise LocalError("Kaynak dosya bulunamadi: %s" % src)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        size = os.path.getsize(src)
        self._log(INFO, "%s -> %s (%s)" % (src, dst, _human(size)), stage=stage)
        shutil.copy2(src, dst)
        self._log(OK, "Kopyalama tamamlandi: %s" % dst)
        return os.path.getsize(dst)


def stop_windows_services(remote, names, log):
    """Yerel Windows servislerini durdurur (docker disi kurulumlar icin)."""
    if not names:
        return []
    log(INFO, "Durdurulacak Windows servisleri: %s" % ", ".join(names))
    stopped = []
    for name in names:
        code, _, err = remote.run("net stop %s" % _shq(name), check=False)
        if code == 0:
            log(OK, "  %s -> durduruldu" % name)
            stopped.append(name)
        else:
            log(WARN, "  %s -> durdurulamadi%s" % (name, _tail(err)))
    return stopped


def start_windows_services(remote, names, log):
    """Yerel Windows servislerini baslatir."""
    if not names:
        return []
    log(INFO, "Baslatilacak Windows servisleri: %s" % ", ".join(names))
    for name in names:
        code, _, err = remote.run("net start %s" % _shq(name), check=False)
        if code == 0:
            log(OK, "  %s -> calisiyor" % name)
        else:
            log(WARN, "  %s -> baslatilamadi%s" % (name, _tail(err)))
    return names


def _tail(text):
    lines = (text or "").strip().splitlines()
    return (": " + lines[-1]) if lines else ""
