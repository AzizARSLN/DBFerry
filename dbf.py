#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DBFerry komut satiri arayuzu.

Kullanim:
    python dbf.py web                      # Yapilandirma ekranini ac (onerilen)
    python dbf.py web --port 9000 --no-browser
    python dbf.py mcp                      # MCP sunucusunu baslat (ajanlar icin)
    python dbf.py mcp --transport streamable-http --port 8765
    python dbf.py run --spec is.json       # Isi konsoldan calistir
    python dbf.py run --spec is.json --yes
    python dbf.py inspect --spec is.json   # Salt okunur ortam denetimi
    python dbf.py profiles                 # Kayitli sunuculari listele
    python dbf.py history                  # Is gecmisi
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbf_ferry import APP_NAME, APP_TAGLINE, __version__          # noqa: E402
from dbf_ferry.jobs import Job                                     # noqa: E402
from dbf_ferry.model import JOB_TYPES, JobSpec                     # noqa: E402
from dbf_ferry.store import Store                                  # noqa: E402


def data_dir(args):
    if args.data_dir:
        return args.data_dir
    from dbf_ferry.web.app import DEFAULT_DATA_DIR
    return DEFAULT_DATA_DIR


def cmd_web(args):
    from dbf_ferry.web.app import free_port, serve
    port = args.port
    if port == 0:
        port = free_port(8787)
    return serve(host=args.host, port=port, data_dir=data_dir(args),
                 open_browser=not args.no_browser)


def cmd_mcp(args):
    from dbf_ferry.mcp.server import main as mcp_main
    mcp_main(transport=args.transport, host=args.host, port=args.port)
    return 0


def load_spec(path):
    if not os.path.isfile(path):
        sys.stderr.write("HATA: is dosyasi bulunamadi: %s\n" % path)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if "spec" in raw:
        raw = raw["spec"]
    return JobSpec.from_dict(raw)


def _run_job(args, inspect_only=False):
    store = Store(data_dir(args))
    spec = load_spec(args.spec)
    title = "Ortam Denetimi" if inspect_only else JOB_TYPES.get(spec.type, spec.type)
    job = Job(spec, store, title=title)

    print()
    print("  %s v%s - %s" % (APP_NAME, __version__, APP_TAGLINE))
    print("  Is   : %s" % spec.summary())
    print()

    if not inspect_only:
        steps = job.plan()
        print("  YAPILACAKLAR (%d adim)" % len(steps))
        for i, st in enumerate(steps, 1):
            print("   %2d. %-36s %s" % (i, st["title"], st["detail"]))
        print()
        if not args.yes:
            answer = input("  Devam etmek icin 'EVET' yazin: ").strip()
            if answer != "EVET":
                print("  Iptal edildi.")
                return 1

    console = job.attach_console()
    (job.inspect_async() if inspect_only else job.run_async())
    console.join()
    while job.status in ("hazir", "calisiyor"):
        time.sleep(0.2)
    return 0 if job.status == "tamam" else 1


def cmd_run(args):
    return _run_job(args, inspect_only=False)


def cmd_inspect(args):
    return _run_job(args, inspect_only=True)


def cmd_profiles(args):
    store = Store(data_dir(args))
    profiles = store.load_profiles()
    if not profiles:
        print("  Kayitli sunucu yok. 'python dbf.py web' ile ekleyin.")
        return 0
    print("\n  %-18s %-24s %-14s %s" % ("AD", "SUNUCU / KONUM", "VERITABANI", "KONTEYNER"))
    print("  " + "-" * 80)
    for p in profiles:
        if p.is_local():
            addr = "yerel (%s@%s:%s)" % (p.db_user, p.db_host, p.db_port)
            cont = p.pg_container if p.pg_mode == "docker" else "-"
        else:
            addr = "%s@%s:%s" % (p.user, p.host, p.port)
            cont = p.pg_container
        print("  %-18s %-24s %-14s %s" % (
            p.name[:18], addr[:24], p.db_name[:14], cont))
    print()
    return 0


def cmd_history(args):
    store = Store(data_dir(args))
    items = store.load_history(args.limit)
    if not items:
        print("  Gecmis bos.")
        return 0
    print("\n  %-9s %-22s %-10s %s" % ("DURUM", "TARIH", "SURE", "IS"))
    print("  " + "-" * 90)
    for it in items:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("started_at") or 0))
        dur = "%.1f sn" % (it.get("duration") or 0)
        print("  %-9s %-22s %-10s %s" % (it.get("status", "?"), when, dur, it.get("summary", "")[:44]))
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="dbf",
        description="%s - %s" % (APP_NAME, APP_TAGLINE),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version="%s %s" % (APP_NAME, __version__))
    parser.add_argument("--data-dir", help="Profil/gecmis klasoru")
    sub = parser.add_subparsers(dest="command")

    p_web = sub.add_parser("web", help="Yapilandirma ekranini ac")
    p_web.add_argument("--port", type=int, default=0, help="0 = bos port bul")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--no-browser", action="store_true")
    p_web.set_defaults(func=cmd_web)

    p_mcp = sub.add_parser("mcp", help="MCP sunucusunu baslat (ajanlar icin)")
    p_mcp.add_argument("--transport", default="stdio",
                       choices=("stdio", "sse", "streamable-http"))
    p_mcp.add_argument("--host", default="127.0.0.1")
    p_mcp.add_argument("--port", type=int, default=8765)
    p_mcp.set_defaults(func=cmd_mcp)

    p_run = sub.add_parser("run", help="Isi calistir")
    p_run.add_argument("--spec", required=True, help="Is tanimi JSON dosyasi")
    p_run.add_argument("--yes", "-y", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_ins = sub.add_parser("inspect", help="Salt okunur ortam denetimi")
    p_ins.add_argument("--spec", required=True)
    p_ins.set_defaults(func=cmd_inspect)

    p_prof = sub.add_parser("profiles", help="Sunuculari listele")
    p_prof.set_defaults(func=cmd_profiles)

    p_hist = sub.add_parser("history", help="Is gecmisi")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
