"""Команды установки и запуска."""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import ConfigError, load_settings
from .db import configure, get_settings, init_db
from .errors import UserError
from .identity import create_user
from .web import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ditly",
        description="Внутренний сервис коротких ссылок",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Запустить сервис")
    create = commands.add_parser("create-user", help="Создать учётку")
    create.add_argument("login", help="Логин новой учётки")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    configure(settings)
    init_db()

    if args.command == "serve":
        _serve()
        return
    if args.command == "create-user":
        _create_user(args.login)
        return
    raise SystemExit(f"Неизвестная команда: {args.command}")


def _serve() -> None:
    settings = get_settings()
    app = create_app(settings)
    print(f"Дитли слушает http://{settings.host}:{settings.port}")
    print(f"Базовый адрес коротких ссылок: {settings.base_url}")
    print(f"База SQLite: {settings.db_path}")
    app.run(host=settings.host, port=settings.port, debug=False)


def _create_user(login: str) -> None:
    password = os.environ.get("DITLY_PASSWORD")
    if password is None:
        password = getpass.getpass("Пароль: ")
        again = getpass.getpass("Ещё раз: ")
        if password != again:
            raise SystemExit("Пароли не совпадают.")
    try:
        create_user(login, password)
    except UserError as exc:
        raise SystemExit(exc.message) from exc
    print(f"Учётка «{login.strip()}» создана.", file=sys.stdout)


if __name__ == "__main__":
    main()
