"""Command line entry point: ``veille run|serve|worker|hash-password|migrate|doctor``."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from veille.config import Settings, get_settings


def _cmd_hash_password(args: argparse.Namespace) -> int:
    from argon2 import PasswordHasher

    password = args.password or getpass.getpass("Mot de passe : ")
    if not args.password:
        confirm = getpass.getpass("Confirmer : ")
        if confirm != password:
            print("Les mots de passe ne correspondent pas.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Mot de passe trop court (8 caractères minimum).", file=sys.stderr)
        return 1
    print(PasswordHasher().hash(password))
    return 0


def _settings() -> Settings:
    from veille.wiring import configure_logging

    settings = get_settings()
    configure_logging(settings)
    return settings


def _cmd_run(args: argparse.Namespace) -> int:
    from veille.contracts import RunTrigger
    from veille.db import migrate
    from veille.wiring import run_once

    settings = _settings()
    migrate(settings.database_path)
    return run_once(settings, trigger=RunTrigger.CLI, force=args.force, theme_ids=args.theme)


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from veille.wiring import build_web_app

    settings = _settings()
    uvicorn.run(
        build_web_app(settings),
        host=settings.web_host,
        port=settings.web_port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level=settings.log_level.lower(),
        server_header=False,
    )
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    import os
    import signal
    import threading

    from veille.clock import SystemClock
    from veille.contracts import RunTrigger
    from veille.db import migrate
    from veille.scheduler import run_forever
    from veille.wiring import run_once

    settings = _settings()
    if hasattr(os, "nice"):
        os.nice(10)  # low CPU priority: the VPS is shared
    migrate(settings.database_path)
    stop = threading.Event()

    def _stop(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_forever(
        lambda: run_once(settings, trigger=RunTrigger.CRON),
        clock=SystemClock(),
        run_at=settings.run_at,
        tz=settings.tz,
        stop=stop,
    )
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from veille.db import migrate

    settings = _settings()
    applied = migrate(settings.database_path)
    print(f"Migrations appliquées : {', '.join(applied) or 'aucune (à jour)'}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from veille.doctor import run_checks

    settings = _settings()
    ok = True
    for check in run_checks(settings):
        print(f"[{'OK' if check.ok else 'KO'}] {check.name} : {check.detail}")
        ok = ok and check.ok
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="veille", description="Veille Journal")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Generate today's articles now")
    run.add_argument("--force", action="store_true", help="Regenerate existing articles")
    run.add_argument("--theme", type=int, action="append", help="Only this theme id")
    run.set_defaults(func=_cmd_run)

    sub.add_parser("serve", help="Start the web server").set_defaults(func=_cmd_serve)
    sub.add_parser("worker", help="Start the nightly scheduler").set_defaults(func=_cmd_worker)
    sub.add_parser("migrate", help="Apply database migrations").set_defaults(func=_cmd_migrate)
    sub.add_parser("doctor", help="Check DB, Ollama, SearXNG").set_defaults(func=_cmd_doctor)

    hp = sub.add_parser("hash-password", help="Print an argon2id hash for APP_PASSWORD_HASH")
    hp.add_argument("--password", help="Password (otherwise prompted)")
    hp.set_defaults(func=_cmd_hash_password)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
