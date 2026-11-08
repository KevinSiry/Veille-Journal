"""Command line entry point: ``veille run|serve|worker|hash-password|migrate|doctor``."""

from __future__ import annotations

import argparse
import getpass
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import datetime

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


EXIT_INTERRUPTED = 130


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def _cmd_run(args: argparse.Namespace) -> int:
    import logging
    import signal

    from veille.contracts import RunTrigger
    from veille.db import migrate
    from veille.wiring import run_once

    settings = _settings()
    migrate(settings.database_path)
    # SIGTERM (docker stop) behaves like Ctrl+C: the run is recorded as ERROR.
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return run_once(settings, trigger=RunTrigger.CLI, force=args.force, theme_ids=args.theme)
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("run interrupted")
        print("Exécution interrompue.", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)


def make_stop_handler(
    stop: threading.Event, busy: threading.Event
) -> Callable[[int, object], None]:
    """Signal handler for the worker: idle -> just stop the loop; while a job runs ->
    also raise KeyboardInterrupt in the main thread, so the run's ``finally`` records
    ERROR and open transactions roll back."""

    def handler(signum: int, frame: object) -> None:
        stop.set()
        if busy.is_set():
            raise KeyboardInterrupt(f"signal {signum}")

    return handler


def _last_run_started(settings: Settings) -> datetime | None:
    from veille.db import SqliteStore, connect

    store = SqliteStore(connect(settings.database_path))
    try:
        last = store.last_run()
    finally:
        store.close()
    return last.started_at if last is not None else None


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
    import logging
    import os
    import signal

    from veille.clock import SystemClock
    from veille.contracts import RunTrigger
    from veille.db import migrate
    from veille.scheduler import needs_catch_up, run_forever
    from veille.wiring import run_once

    settings = _settings()
    if hasattr(os, "nice"):
        os.nice(10)  # low CPU priority: the VPS is shared
    migrate(settings.database_path)
    stop = threading.Event()
    busy = threading.Event()
    handler = make_stop_handler(stop, busy)
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}

    def job() -> int:
        busy.set()
        try:
            return run_once(settings, trigger=RunTrigger.CRON)
        finally:
            busy.clear()

    clock = SystemClock()
    catch_up = needs_catch_up(
        clock.now(), settings.run_at, settings.tz, _last_run_started(settings)
    )
    try:
        run_forever(
            job,
            clock=clock,
            run_at=settings.run_at,
            tz=settings.tz,
            stop=stop,
            catch_up=catch_up,
        )
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("worker interrupted during a run")
        return EXIT_INTERRUPTED
    finally:
        for sig, old_handler in previous.items():
            signal.signal(sig, old_handler)
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
