"""SSH / SFTP katmani.

paramiko uzerine ince bir sarmalayici: host key parmak izi dogrulamasi,
uzak komut calistirma, dosya transferi ve canli log yayini.
"""

import base64
import hashlib
import os
import socket
import time

import paramiko

from .events import CMD, ERR, INFO, OK, OUT, PROGRESS, WARN


class SSHError(RuntimeError):
    pass


def fingerprint_of(key):
    """paramiko PKey -> 'SHA256:....' biciminde parmak izi."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def fetch_hostkey(host, port=22, timeout=15):
    """Kimlik dogrulamadan sunucunun host key parmak izini ogrenir.

    Arayuzdeki 'Anahtari Getir' dugmesi bunu kullanir.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        return {"hostkey": fingerprint_of(key), "key_type": key.get_name()}
    except Exception as exc:
        raise SSHError("Host key alinamadi (%s:%s): %s" % (host, port, exc))
    finally:
        try:
            transport.close()
        except Exception:
            pass


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024


class Remote(object):
    """Tek bir uzak sunucu oturumu."""

    def __init__(self, profile, log=None, should_stop=None):
        self.p = profile
        self._log = log or (lambda *a, **k: None)
        self._should_stop = should_stop or (lambda: False)
        self.client = None
        self.sftp = None
        self.hostkey_seen = ""

    # -- yasam dongusu -------------------------------------------------------

    def connect(self, timeout=25):
        addr = "%s@%s:%s" % (self.p.user, self.p.host, self.p.port)
        self._log(INFO, "%s adresine baglaniliyor" % addr)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        t0 = time.time()
        try:
            self.client.connect(
                hostname=self.p.host,
                port=self.p.port,
                username=self.p.user,
                password=self.p.password or None,
                look_for_keys=False,
                allow_agent=False,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
            )
        except paramiko.AuthenticationException:
            raise SSHError("%s icin kimlik dogrulama basarisiz (kullanici/sifre hatali)" % addr)
        except (socket.timeout, socket.error) as exc:
            raise SSHError("%s adresine ulasilamadi: %s" % (addr, exc))
        except paramiko.SSHException as exc:
            raise SSHError("%s ile SSH el sikismasi basarisiz: %s" % (addr, exc))

        self._verify_hostkey()
        self.sftp = self.client.open_sftp()
        self._log(OK, "Baglanti kuruldu (%s, %.2f sn)" % (addr, time.time() - t0))
        return self

    def _verify_hostkey(self):
        try:
            key = self.client.get_transport().get_remote_server_key()
        except Exception:
            self._log(WARN, "Sunucu host key'i okunamadi, dogrulama atlandi")
            return
        seen = fingerprint_of(key)
        self.hostkey_seen = seen
        expected = (self.p.hostkey or "").strip()
        if not expected:
            self._log(WARN, "Host key kayitli degil. Bu profilde saklayin: %s" % seen)
            return
        if expected.rstrip("=") != seen.rstrip("="):
            self.close()
            raise SSHError(
                "HOST KEY UYUSMUYOR! Beklenen %s, gelen %s. "
                "OlasI MITM saldirisi ya da sunucu yeniden kurulmus olabilir." % (expected, seen)
            )
        self._log(OK, "Host key dogrulandi (%s, %s)" % (seen, key.get_name()))

    def close(self):
        for obj in (self.sftp, self.client):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.sftp = None
        self.client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- komut calistirma ----------------------------------------------------

    def run(self, command, check=True, timeout=3600, quiet=False):
        if self._should_stop():
            raise SSHError("Islem kullanici tarafindan durduruldu")
        if not quiet:
            self._log(CMD, command)
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if not quiet:
            for line in out.splitlines():
                if line.strip():
                    self._log(OUT, line)
            for line in err.splitlines():
                if line.strip():
                    self._log(ERR, line)
        if check and code != 0:
            detail = (err or out).strip().splitlines()
            raise SSHError("Uzak komut basarisiz (exit=%d): %s%s" % (
                code, command,
                ("\n  -> " + detail[-1]) if detail else "",
            ))
        return code, out, err

    def capture(self, command, timeout=300):
        """Komut ciktisini loglamadan, kirpilmis sekilde dondurur."""
        code, out, err = self.run(command, check=False, quiet=True, timeout=timeout)
        return out.strip()

    def exists(self, remote_path):
        return self.capture("test -e %s && echo VAR || echo YOK" % _shq(remote_path)) == "VAR"

    def free_space(self, remote_dir="/tmp"):
        return self.capture("df -h %s 2>/dev/null | tail -1 | awk '{print $4}'" % _shq(remote_dir))

    # -- transfer ------------------------------------------------------------

    def upload(self, local_path, remote_path, stage=None):
        total = os.path.getsize(local_path)
        last = {"pct": -1, "t": 0.0}

        def cb(done, size):
            pct = int(done * 100 / size) if size else 0
            now = time.time()
            if pct != last["pct"] and (pct % 5 == 0 or now - last["t"] > 2):
                last["pct"] = pct
                last["t"] = now
                self._log(PROGRESS, "Yukleniyor  %3d%%  %s / %s" % (
                    pct, human_bytes(done), human_bytes(size)), stage=stage, pct=pct)

        self._log(INFO, "%s -> %s:%s (%s)" % (
            os.path.basename(local_path), self.p.host, remote_path, human_bytes(total)))
        self.sftp.put(local_path, remote_path, callback=cb)
        self._log(OK, "Transfer tamamlandi (%s)" % human_bytes(total))
        return total

    def download(self, remote_path, local_path, stage=None):
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        total = int(self.capture("stat -c %%s %s 2>/dev/null || echo 0" % _shq(remote_path)) or 0)
        last = {"pct": -1, "t": 0.0}

        def cb(done, size):
            pct = int(done * 100 / size) if size else 0
            now = time.time()
            if pct != last["pct"] and (pct % 5 == 0 or now - last["t"] > 2):
                last["pct"] = pct
                last["t"] = now
                self._log(PROGRESS, "Indiriliyor  %3d%%  %s / %s" % (
                    pct, human_bytes(done), human_bytes(size)), stage=stage, pct=pct)

        self._log(INFO, "%s:%s -> %s (%s)" % (
            self.p.host, remote_path, os.path.basename(local_path), human_bytes(total)))
        self.sftp.get(remote_path, local_path, callback=cb)
        self._log(OK, "Indirme tamamlandi: %s" % local_path)
        return os.path.getsize(local_path)


def _shq(text):
    """Kabuk icin tek tirnakli guvenli alinti."""
    return "'" + str(text).replace("'", "'\\''") + "'"
