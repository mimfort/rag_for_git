"""FastAPI-роутер для API истории ревью.

Монтируется в ``app.py`` через ``make_router(history)``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from reviewer.web.history import ReviewHistory

log = logging.getLogger(__name__)


def make_router(history: ReviewHistory) -> APIRouter:
    """Создать APIRouter с эндпоинтами наблюдаемости.

    Args:
        history: экземпляр ReviewHistory (зависимость через замыкание).

    Routes:
        GET /api/runs            — список прогонов с пагинацией.
        GET /api/runs/{run_id}   — прогон + находки (404 если не найден).
        GET /api/stats           — агрегированная статистика.
    """
    router = APIRouter()

    @router.get("/api/runs")
    def list_runs(
        repo: str | None = Query(default=None, description="Фильтр по репозиторию"),
        status: str | None = Query(default=None, description="Фильтр по статусу (ok/error/draft_skip)"),
        limit: int = Query(default=50, ge=1, le=500, description="Кол-во записей"),
        offset: int = Query(default=0, ge=0, description="Смещение (пагинация)"),
    ) -> JSONResponse:
        """Список прогонов ревью (без находок), последние первыми."""
        try:
            runs = history.list_runs(repo=repo, status=status, limit=limit, offset=offset)
            return JSONResponse({"runs": runs, "limit": limit, "offset": offset})
        except Exception as exc:
            log.error("Ошибка при получении списка прогонов: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc

    @router.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> JSONResponse:
        """Прогон вместе с находками. 404, если прогон не найден."""
        try:
            run = history.get_run(run_id)
        except Exception as exc:
            log.error("Ошибка при получении прогона %s: %s", run_id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc
        if run is None:
            raise HTTPException(status_code=404, detail=f"Прогон {run_id} не найден")
        return JSONResponse(run)

    @router.get("/api/stats")
    def get_stats(
        days: int = Query(default=30, ge=1, le=365, description="Период статистики в днях"),
    ) -> JSONResponse:
        """Агрегированная статистика за последние N дней."""
        try:
            data = history.stats(days=days)
            return JSONResponse(data)
        except Exception as exc:
            log.error("Ошибка при получении статистики: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc

    return router
