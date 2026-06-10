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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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
    - При завершении работы закрывает пул соединений к Postgres.
    """
    history = ReviewHistory(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        yield
        history.close()

    app = FastAPI(
        title="Reviewer — наблюдаемость агента",
        description="История прогонов ревью, стоимость и находки.",
        version="0.1.0",
        lifespan=_lifespan,
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
    app.include_router(make_router(history, settings))

    # Статика / SPA
    if _FRONTEND_DIST.is_dir():
        # Собранные ассеты (js/css/шрифты) — отдельным mount'ом на /assets.
        _assets = _FRONTEND_DIST / "assets"
        if _assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")
        _index = _FRONTEND_DIST / "index.html"
        _dist_root = _FRONTEND_DIST.resolve()

        # SPA-fallback: реальный файл (favicon и т.п.) если есть, иначе index.html.
        # Без этого обновление страницы на клиентском роуте (напр. /runs/11) отдавало
        # 404 — StaticFiles ищет файл `runs/11`, не находит и не делает fallback.
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # Несуществующий /api/* — JSON-404 (а не HTML), чтобы фронт корректно ловил
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            if full_path:
                try:
                    candidate = (_FRONTEND_DIST / full_path).resolve()
                    if candidate.is_file() and candidate.is_relative_to(_dist_root):
                        return FileResponse(candidate)
                except (OSError, ValueError):
                    pass
            return FileResponse(_index)
        log.info("Фронтенд (SPA) обслуживается из %s", _FRONTEND_DIST)
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
