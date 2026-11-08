"""Command line entry point: ``veille run|serve|worker|hash-password|migrate|doctor``."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence


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


def _not_wired(args: argparse.Namespace) -> int:  # replaced during integration
    print(f"'{args.command}' is not wired yet", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="veille", description="Veille Journal")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Generate today's articles now")
    run.add_argument("--force", action="store_true", help="Regenerate existing articles")
    run.add_argument("--theme", type=int, action="append", help="Only this theme id")
    run.set_defaults(func=_not_wired)

    sub.add_parser("serve", help="Start the web server").set_defaults(func=_not_wired)
    sub.add_parser("worker", help="Start the nightly scheduler").set_defaults(func=_not_wired)
    sub.add_parser("migrate", help="Apply database migrations").set_defaults(func=_not_wired)
    sub.add_parser("doctor", help="Check DB, Ollama, SearXNG").set_defaults(func=_not_wired)

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
