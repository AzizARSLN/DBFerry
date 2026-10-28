# DBFerry

[![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](#)
[![Dependencies](https://img.shields.io/badge/runtime-paramiko-blue)](#)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20macos-lightgrey)](#)
[![Console](https://img.shields.io/badge/web%20console-127.0.0.1%3A8787-informational)](#)

**Self-hosted PostgreSQL migration and backup tool with a local web console.**

DBFerry moves a PostgreSQL database from one place to another without hand-writing
`pg_dump` / `pg_restore` chains. It takes care of the parts that usually go wrong:
stopping the right services in the right order, keeping a safety backup, verifying the
transfer with SHA-256, restoring into a clean database, and then reporting what actually
happened.

Remote hosts are reached over SSH (Paramiko). The local machine is driven through Git
Bash, so the same job definition works whether the target is a server in a datacenter or
PostgreSQL installed on the laptop in front of you.

There is no server to deploy and no database of its own — everything runs from the
machine you launch it on.

---

## Job types

| Key | Direction | What it does |
| --- | --- | --- |
| `file_to_server` | local `.dump` → remote host | Restores a dump file into a Docker PostgreSQL on a remote server |
| `file_to_local` | local `.dump` → this machine | Restores a dump file into the PostgreSQL installed locally |
| `server_to_server` | live copy | Dumps on the source, pulls it down, restores on the target |
| `server_to_file` | remote host → local disk | Backs a remote database up to `.dump` + `.sha256` |
| `local_to_file` | this machine → local disk | Backs a local database up to `.dump` + `.sha256` |

Backup jobs are read-only. Restore jobs can drop and recreate the target database,
depending on the chosen restore mode.

## Features

- **Plan before action.** Every job produces a numbered step list first. Nothing is
  touched until you approve it. The same list is the progress bar during the run.
- **Live output.** Log lines stream to the console over Server-Sent Events, including
  the exact commands that were executed and their stdout/stderr.
- **Integrity checks.** Dumps are written in PostgreSQL custom format (`pg_dump -Fc`)
  and checksummed with SHA-256. A `.sha256` sidecar file is written next to each backup
  and verified on the way back in.
- **Maintenance window handling.** Optionally stops the application containers before
  dumping a live database, then brings them back when the job is done. Container lists
  are configured per profile.
- **Docker or direct.** PostgreSQL can live inside a container (commands run through
  `docker exec`) or be installed on the host (commands run directly). Both are supported
  for remote and local targets.
- **Safety backup.** Before a destructive restore, the current database is dumped to
  `data/backups/<name>/` so there is a way back.
- **Post-restore verification.** Table counts, `schema_migrations` version and your own
  `SQL | expected` checks are validated after the restore.
- **Profiles and history.** Hosts are stored once and reused; every run is appended to a
  local history file with its status and duration.

## How it works

```
PLAN ──► approve ──► RUN ──► REPORT
                      │
                      └── events → console (CLI) / SSE (web)
```

A live copy is just a pipeline of shell steps, each one logged as it runs:

```
pg_dump -Fc ──► SHA-256 ──► transfer (SFTP or local copy) ──► DROP + CREATE ──► pg_restore ──► verify
```

A read-only backup stops at the checksum step and writes the file to disk.

## Requirements

- Python 3.8 or newer
- [`paramiko`](https://www.paramiko.org/) (the only third-party dependency)
- PostgreSQL client tools — `pg_dump`, `pg_restore`, `psql`, `sha256sum`
  - in Docker mode they are expected to exist inside the container
  - in direct mode they must be reachable on `PATH` (or pointed to with the profile's
    `PostgreSQL bin` field / `DBFERRY_PG_BIN`)
- For remote jobs: SSH access to the host, and permission to run `docker` on it
- **Windows only:** [Git for Windows](https://git-scm.com/download/win). Local jobs run
  their commands through `bash.exe`; the detection can be overridden with `DBFERRY_BASH`.

## Installation

```bash
git clone https://github.com/AzizARSLN/DBFerry.git
cd DBFerry
python -m pip install -r requirements.txt
```

## Usage

### Web console

```bash
python dbf.py web
```

Opens `http://127.0.0.1:8787` in your browser. Pick a job type from the tabs, fill in the
connection details, use **Planı Göster** to preview the steps, then **Süreci Başlat** to
run it. Everything is logged in the right-hand panel while it runs.

```bash
python dbf.py web --port 9000 --no-browser
```

### Command line

```bash
python dbf.py run --spec job.json            # plan, ask for confirmation, run
python dbf.py run --spec job.json --yes      # skip the confirmation prompt
python dbf.py inspect --spec job.json        # read-only environment check
python dbf.py profiles                       # list saved hosts
python dbf.py history --limit 20             # recent runs
```

### Example job spec

`run` and `inspect` take a JSON file. A backup of a local database looks like this:

```json
{
  "type": "local_to_file",
  "source_db": "appdb",
  "local_db_host": "127.0.0.1",
  "local_db_port": 5432,
  "local_db_user": "postgres",
  "local_db_password": "secret",
  "local_pg_mode": "local",
  "local_pg_bin": "C:\\Program Files\\PostgreSQL\\17\\bin",
  "local_copy_dir": ""
}
```

And a restore onto a remote server:

```json
{
  "type": "file_to_server",
  "source_dump": "C:\\backups\\appdb.dump",
  "target_profile": "a1b2c3d4",
  "target_db": "appdb",
  "mode": "replace",
  "take_safety_backup": true,
  "stop_services": true,
  "start_services": true,
  "verify": true,
  "expect_migration": "V068"
}
```

`target_profile` is the `id` of a saved profile (see `python dbf.py profiles`). Leave
`local_copy_dir` empty to write backups under `data/backups/<database>/`.

## Project layout

```
DBFerry/
├── dbf.py                 # CLI entry point (web, run, inspect, profiles, history)
├── requirements.txt
└── dbf_ferry/
    ├── model.py           # ServerProfile, JobSpec, job/restore mode definitions
    ├── store.py           # profiles.json + history.jsonl
    ├── events.py          # event bus used by both the CLI and the web console
    ├── sshx.py            # SSH/SFTP layer and host key handling
    ├── localx.py          # local machine layer (drives Git Bash, mirrors the SSH API)
    ├── pg.py              # PostgreSQL operations: dump, restore, verify, docker exec
    ├── jobs.py            # orchestration: plan, run, step tracking, reporting
    └── web/
        ├── app.py         # stdlib HTTP server + JSON/SSE API
        └── static/        # console UI (index.html, app.js, style.css)
```

## HTTP API

The web console is a thin client over a small JSON API. It is useful for scripting too.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/meta` | version, job types, data directory |
| GET | `/api/profiles` | saved hosts (passwords masked) |
| POST | `/api/profiles` | create or update a profile |
| POST | `/api/profiles/delete` | remove a profile |
| POST | `/api/hostkey` | fetch an SSH host key fingerprint |
| POST | `/api/local/dbs` | connect to the local PostgreSQL and list databases |
| POST | `/api/plan` | build and return the step list for a spec |
| POST | `/api/inspect` | start a read-only environment check |
| POST | `/api/run` | start a job |
| GET | `/api/jobs/<id>/events` | Server-Sent Events stream for a running job |
| POST | `/api/jobs/<id>/cancel` | request cancellation |
| GET | `/api/history` | recent runs |

## Security notes

- Profiles — including SSH and database passwords — are kept in `data/profiles.json` on
  the machine running DBFerry. Passwords are currently stored in **plain text** there, so
  treat that file as a secret. It is listed in `.gitignore` and is never sent to the
  browser: the API returns masked profiles with `has_password` flags instead.
- The web console has **no authentication** and binds to `127.0.0.1` by default. Do not
  expose it to a network you do not control.
- In `replace` mode a restore drops the target database before recreating it. Leave
  `take_safety_backup` enabled unless you have your own copy.

## Notes and limitations

- Service startup waits for a container to reach `running`, not `healthy`. Stacks with
  slow dependencies are reported as soon as the container is up, so give them a moment
  before pointing traffic at the API.
- `schema_migrations` verification is skipped (with a warning) when the table does not
  exist in the configured schema.
- Local jobs on Windows need Git Bash; on Linux and macOS the system shell is used.
- Environment overrides: `DBFERRY_BASH`, `DBFERRY_PG_BIN`.

---

## Türkçe

**Yerel web konsolu olan, kendi sunucunuzda çalışan PostgreSQL taşıma ve yedekleme aracı.**

DBFerry, bir PostgreSQL veritabanını elle `pg_dump` / `pg_restore` zinciri yazmadan
başka bir ortama taşır. Genelde sorun çıkaran işleri üstlenir: servisleri doğru sırayla
durdurur, önce güvenlik yedeği alır, aktarımı SHA-256 ile doğrular, temiz bir veritabanına
geri yükler ve sonunda ne yaptığını raporlar.

Uzak makinelere SSH (Paramiko) ile, yerel makineye Git Bash üzerinden erişir. Aynı iş
tanımı hem veri merkezindeki bir sunucu hem de yanı başınızdaki PostgreSQL için çalışır.
Kurulacak bir sunucu ya da ayrı bir veritabanı yoktur.

### İş tipleri

| Anahtar | Yön | Açıklama |
| --- | --- | --- |
| `file_to_server` | yerel `.dump` → uzak sunucu | Döküm dosyasını uzaktaki Docker PostgreSQL'e geri yükler |
| `file_to_local` | yerel `.dump` → bu bilgisayar | Döküm dosyasını yereldeki PostgreSQL'e geri yükler |
| `server_to_server` | canlı kopya | Kaynakta döküm alır, indirir, hedefte geri yükler |
| `server_to_file` | uzak sunucu → yerel disk | Uzaktaki veritabanını `.dump` + `.sha256` olarak yedekler |
| `local_to_file` | bu bilgisayar → yerel disk | Yerel veritabanını `.dump` + `.sha256` olarak yedekler |

Yedekleme işleri salt okunurdur; geri yükleme işleri seçilen moda göre hedef veritabanını
silip yeniden oluşturabilir.

### Kurulum

```bash
git clone https://github.com/AzizARSLN/DBFerry.git
cd DBFerry
python -m pip install -r requirements.txt
```

Gereksinimler: Python 3.8+, `paramiko` ve PostgreSQL istemci araçları (`pg_dump`,
`pg_restore`, `psql`). Windows'ta yerel işlemler için Git for Windows (Git Bash) gerekir.

### Kullanım

```bash
python dbf.py web                      # yapılandırma ekranını aç (127.0.0.1:8787)
python dbf.py run --spec job.json      # planı göster, onay iste, çalıştır
python dbf.py inspect --spec job.json  # salt okunur ortam denetimi
python dbf.py profiles                 # kayıtlı sunucular
python dbf.py history                  # iş geçmişi
```

### Notlar

- Her iş önce **plan** üretir; siz onaylamadan hiçbir şey değiştirilmez. Aynı liste
  çalışma sırasında ilerleme göstergesi olarak kullanılır.
- Yedekler PostgreSQL özel formatında (`pg_dump -Fc`) alınır ve SHA-256 ile doğrulanır;
  her yedeğin yanına `.sha256` dosyası yazılır. Boş `local_copy_dir` bırakılırsa dosyalar
  `data/backups/<veritabanı>/` altına gider.
- Profiller (`data/profiles.json`) şifreleri düz metin olarak tutar; bu dosyayı gizli
  veri gibi koruyun. Sürüm kontrolüne dahil edilmez ve tarayıcıya maskelenmiş gönderilir.
- Web konsolunun kimlik doğrulaması yoktur ve varsayılan olarak `127.0.0.1` adresine
  bağlanır; dışarıya açmayın.
- `replace` modunda hedef veritabanı silinip yeniden oluşturulur; güvenlik yedeğini
  kapatmadığınızdan emin olun.

---

## Contributing

Bug reports and pull requests are welcome — please open an issue first for anything
larger than a small fix.

## Author

Aziz ARSLAN
