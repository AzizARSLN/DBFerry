"""DBFerry veri modeli: sunucu profilleri ve is tanimlari."""

import uuid
from dataclasses import dataclass, field, asdict


def new_id():
    return uuid.uuid4().hex[:8]


def _to_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "evet", "on")


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Sunucu profili
# ---------------------------------------------------------------------------

@dataclass
class ServerProfile:
    """Bir hedef/kaynak sunucu ve uzerindeki PostgreSQL konteynerini tanimlar."""

    id: str = ""
    name: str = ""
    location: str = "remote"          # remote (SSH) | local (bu bilgisayar)
    host: str = ""
    port: int = 22
    user: str = "root"
    password: str = ""
    hostkey: str = ""                 # SHA256:... (bos ise ilk baglantida ogrenilir)

    # Uzak proje bilgileri
    project_dir: str = ""
    pg_mode: str = "docker"           # docker | local
    pg_container: str = "postgres"
    pg_bin: str = ""                  # yerel PostgreSQL bin klasoru (bos ise otomatik bulunur)

    # Veritabani bilgileri
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "postgres"
    db_user: str = "postgres"
    db_password: str = ""
    db_schema: str = "public"

    # Servis kesintisi yonetimi (virgulle ayrilmis konteyner adlari)
    stop_containers: str = ""
    start_containers: str = ""

    notes: str = ""

    def __post_init__(self):
        self.port = _to_int(self.port, 22)
        self.db_port = _to_int(self.db_port, 5432)
        if self.pg_mode not in ("docker", "local"):
            self.pg_mode = "docker"
        if self.location not in ("remote", "local"):
            self.location = "remote"
        if not self.id:
            self.id = new_id()
        if not self.name:
            if self.location == "local":
                self.name = "Yerel PostgreSQL"
            else:
                self.name = self.host or self.id

    # -- yardimcilar ---------------------------------------------------------

    def is_local(self):
        return self.location == "local"

    def stop_list(self):
        return [c.strip() for c in self.stop_containers.split(",") if c.strip()]

    def start_list(self):
        return [c.strip() for c in self.start_containers.split(",") if c.strip()]

    def safe(self):
        """Sifreleri maskeleyerek arayuze gonderilecek kopyayi uretir."""
        d = asdict(self)
        d["has_password"] = bool(self.password)
        d["password"] = ""
        d["has_db_password"] = bool(self.db_password)
        d["db_password"] = ""
        return d

    @staticmethod
    def from_dict(d):
        fields = {f for f in ServerProfile.__dataclass_fields__}
        return ServerProfile(**{k: v for k, v in d.items() if k in fields})


# ---------------------------------------------------------------------------
# Is tanimi
# ---------------------------------------------------------------------------

JOB_TYPES = {
    "file_to_server": "Dosyadan Sunucuya  (yerel dokumu geri yukle)",
    "file_to_local": "Dosyadan Yerele  (yerel dokumu yerel veritabanina yukle)",
    "server_to_server": "Sunucudan Sunucuya  (canli kopya)",
    "server_to_file": "Sunucudan Yerele  (yedek al)",
    "local_to_file": "Yerelden Dosyaya  (yerel veritabanini yedekle)",
}

RESTORE_MODES = {
    "replace": "Mevcut veritabanini sil, yerine yukle",
    "new_db": "Ayri bir veritabani adina yukle (mevcuda dokunma)",
}

VERIFY_QUERY_HELP = (
    "Her satira bir kontrol yazin. Bicim:  SQL | beklenen_deger\n"
    "Ornek:  select count(*) from public.siparis | 10"
)


@dataclass
class VerifyCheck:
    sql: str = ""
    expected: str = ""
    label: str = ""

    @staticmethod
    def parse_multiline(text):
        checks = []
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                sql, _, exp = line.rpartition("|")
            else:
                sql, exp = line, ""
            sql = sql.strip()
            if sql:
                checks.append(VerifyCheck(sql=sql, expected=exp.strip()))
        return checks


@dataclass
class JobSpec:
    """Tek bir tasima/geri yukleme isleminin tanimi."""

    type: str = "file_to_server"

    # Kaynak
    source_profile: str = ""            # server_to_* icin profil id
    source_db: str = ""                 # bos ise profil varsayilani
    source_dump: str = ""               # file_to_server icin yerel dosya yolu

    # Hedef
    target_profile: str = ""
    target_db: str = ""
    mode: str = "replace"
    new_db_name: str = ""

    # Davranis
    take_safety_backup: bool = True
    stop_services: bool = True
    start_services: bool = True
    stop_source_services: bool = False
    upload_dir: str = "/tmp"
    keep_local_copy: bool = False
    local_copy_dir: str = ""

    # Dogrulama
    verify: bool = True
    expect_migration: str = ""
    verify_queries: str = ""

    # Yerel hedef (file_to_local): profil gerekmez, baglanti is ekraninda girilir
    local_db_host: str = "127.0.0.1"
    local_db_port: int = 5432
    local_db_user: str = "postgres"
    local_db_password: str = ""
    local_pg_mode: str = "local"        # local (dogrudan) | docker
    local_pg_container: str = "postgres"
    local_pg_bin: str = ""

    @staticmethod
    def from_dict(d):
        fields = {f for f in JobSpec.__dataclass_fields__}
        spec = JobSpec(**{k: v for k, v in d.items() if k in fields})
        for key in ("take_safety_backup", "stop_services", "start_services",
                    "stop_source_services", "keep_local_copy", "verify"):
            setattr(spec, key, _to_bool(getattr(spec, key), True if key != "stop_source_services" else False))
        if spec.mode not in RESTORE_MODES:
            spec.mode = "replace"
        spec.local_db_port = _to_int(spec.local_db_port, 5432)
        if spec.local_pg_mode not in ("docker", "local"):
            spec.local_pg_mode = "local"
        return spec

    def local_profile(self, dbname=None):
        """Yerel baglanti icin is ekraninda girilen bilgilerden gecici profil uretir.

        Dosyadan Yerele isinde hedef, Yerelden Dosyaya isinde kaynak olarak
        kullanilir. dbname verilmezse hedef alanlari kullanilir.
        """
        containers = self.local_pg_container if self.local_pg_mode == "docker" else ""
        return ServerProfile(
            location="local",
            name="Yerel PostgreSQL",
            pg_mode=self.local_pg_mode,
            pg_container=self.local_pg_container,
            pg_bin=self.local_pg_bin,
            db_host=self.local_db_host,
            db_port=self.local_db_port,
            db_user=self.local_db_user,
            db_password=self.local_db_password,
            db_name=dbname or self.target_db or self.new_db_name or "postgres",
            stop_containers=containers,
            start_containers=containers,
        )

    def checks(self):
        return VerifyCheck.parse_multiline(self.verify_queries)

    def safe(self):
        """Hassas alanlari maskeleyerek disariya verilecek kopyayi uretir."""
        d = dict(self.__dict__)
        d["has_local_password"] = bool(self.local_db_password)
        d["local_db_password"] = ""
        return d

    def summary(self):
        t = JOB_TYPES.get(self.type, self.type)
        if self.type == "file_to_local":
            db = self.new_db_name if self.mode == "new_db" else self.target_db
            return "%s -> yerel/%s [%s]" % (self.source_dump, db or "?", t)
        if self.type == "file_to_server":
            db = self.new_db_name if self.mode == "new_db" else self.target_db
            return "%s -> %s [%s]" % (self.source_dump, db or self.target_profile, t)
        if self.type == "local_to_file":
            return "yerel/%s [%s]" % (self.source_db or "?", t)
        if self.type == "server_to_server":
            return "%s -> %s [%s]" % (self.source_profile, self.target_profile, t)
        return "%s [%s]" % (self.source_profile, t)
