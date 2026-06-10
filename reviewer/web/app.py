"""Фабрика FastAPI-приложения для веб-админки наблюдаемости.

Использование::

    from reviewer.web.app import create_app
    from reviewer.config.settings import Settings
    app = create_app(Settings())

    # или через CLI:
    # reviewer serve --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from reviewer.config.settings import Settings
from reviewer.web.api import make_router
from reviewer.web.history import ReviewHistory

log = logging.getLogger(__name__)

# Путь к собранному фронтенду (web/frontend/dist относительно корня проекта)
_FRONTEND_DIST = Path(__file__).parent.parent.parent / "web" / "frontend" / "dist"


def create_app(settings: Settings) -> FastAPI:
    """Создать и настроить FastAPI-приложение.

    - Инициализирует ReviewHistory и schema (fail-soft: если БД недоступна,
      сервер всё равно стартует с предупреждением).
    - Монтирует API-роутер (/api/*).
    - Монтирует собранный SPA на / (если dist существует) или отдаёт заглушку.
    - Настраивает CORS для dev-сервера Vite (:5173).
    """
    app = FastAPI(
        title="Reviewer — наблюдаемость агента",
        description="История прогонов ревью, стоимость и находки.",
        version="0.1.0",
    )

    # CORS: разрешаем dev-сервер Vite
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # История прогонов
    history = ReviewHistory(settings.pg_dsn)
    try:
        history.init_schema()
    except Exception as exc:
        log.warning(
            "Не удалось инициализировать схему БД истории (%s). "
            "Убедитесь, что Postgres запущен: docker compose up -d. "
            "API-эндпоинты вернут ошибку 500 до подключения к БД.",
            exc,
        )

    # API-роутер
    app.include_router(make_router(history))

    # Статика / SPA
    if _FRONTEND_DIST.is_dir():
        # Монтируем статику, SPA-fallback на index.html
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
        log.info("Фронтенд смонтирован из %s", _FRONTEND_DIST)
    else:
        # Заглушка — подсказка разработчику
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def frontend_stub() -> str:
            return (
                "<html><body style='font-family:monospace;padding:2rem'>"
                "<h2>Reviewer — веб-админка</h2>"
                "<p>Фронтенд ещё не собран. Чтобы собрать:</p>"
                "<pre>cd web/frontend\nnpm install\nnpm run build</pre>"
                "<p>API доступен по адресу <a href='/api/runs'>/api/runs</a></p>"
                "</body></html>"
            )

    return app
