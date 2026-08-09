"""Общий runtime-запуск web-админки для CLI и контейнера."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
CONTAINER_DEFAULT_HOST = "0.0.0.0"


def run_server(host: str, port: int) -> None:
    """Создать приложение и запустить Uvicorn на переданном runtime-адресе."""
    import uvicorn

    from reviewer.config.settings import Settings
    from reviewer.web.app import create_app

    app = create_app(Settings())
    print(f"Запуск веб-сервера на http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Запустить веб-админку reviewer")
    parser.add_argument(
        "--host",
        default=os.getenv("REVIEWER_WEB_HOST", CONTAINER_DEFAULT_HOST),
        help="Хост для uvicorn (env: REVIEWER_WEB_HOST)",
    )
    parser.add_argument(
        "--port",
        default=os.getenv("REVIEWER_WEB_PORT", str(DEFAULT_PORT)),
        type=int,
        help="Порт для uvicorn (env: REVIEWER_WEB_PORT)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Разобрать container args/env и запустить общий server runner."""
    args = _parser().parse_args(argv)
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
