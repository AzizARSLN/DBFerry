"""DBFerry kalici depolama: sunucu profilleri ve is gecmisi."""

import json
import os
import threading
import time

from .model import ServerProfile, JobSpec, new_id

PROFILES_FILE = "profiles.json"
HISTORY_FILE = "history.jsonl"


class ConfigError(RuntimeError):
    pass


class Store(object):
    """Profil ve gecmis kayitlarini JSON dosyalarinda tutar."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.profiles_path = os.path.join(self.root, PROFILES_FILE)
        self.history_path = os.path.join(self.root, HISTORY_FILE)
        self._lock = threading.Lock()

    # -- profiller -----------------------------------------------------------

    def load_profiles(self):
        if not os.path.isfile(self.profiles_path):
            return []
        try:
            with open(self.profiles_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (ValueError, OSError) as exc:
            raise ConfigError("profiles.json okunamadi: %s" % exc)
        return [ServerProfile.from_dict(item) for item in raw.get("profiles", [])]

    def save_profiles(self, profiles):
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "profiles": [p.__dict__ for p in profiles],
        }
        self._write_json(self.profiles_path, payload)
        self._harden(self.profiles_path)

    def get_profile(self, pid):
        for p in self.load_profiles():
            if p.id == pid:
                return p
        return None

    def upsert_profile(self, data):
        with self._lock:
            profiles = self.load_profiles()
            incoming = ServerProfile.from_dict(data)
            existing = None
            for p in profiles:
                if p.id == incoming.id:
                    existing = p
                    break
            if existing is None:
                incoming.id = incoming.id or new_id()
                profiles.append(incoming)
                saved = incoming
            else:
                # Sifre alani bos geldiyse mevcut sifreyi koru
                if not incoming.password:
                    incoming.password = existing.password
                if not incoming.db_password:
                    incoming.db_password = existing.db_password
                idx = profiles.index(existing)
                profiles[idx] = incoming
                saved = incoming
            self.save_profiles(profiles)
            return saved

    def delete_profile(self, pid):
        with self._lock:
            profiles = self.load_profiles()
            remaining = [p for p in profiles if p.id != pid]
            if len(remaining) == len(profiles):
                return False
            self.save_profiles(remaining)
            return True

    # -- gecmis --------------------------------------------------------------

    def append_history(self, job):
        record = {
            "id": job.id,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "status": job.status,
            "type": job.spec.type,
            "summary": job.summary_text,
            "spec": job.spec.safe(),
            "duration": (job.finished_at or 0) - (job.started_at or 0),
        }
        with self._lock:
            with open(self.history_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def load_history(self, limit=50):
        if not os.path.isfile(self.history_path):
            return []
        items = []
        with open(self.history_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
        items.reverse()
        return items[:limit]

    # -- yardimcilar ---------------------------------------------------------

    @staticmethod
    def _write_json(path, payload):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    @staticmethod
    def _harden(path):
        """Mumkunse dosya izinlerini daralt (sifre icerdigi icin)."""
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
