"""FastAPI-роутер для API истории ревью.

Монтируется в ``app.py`` через ``make_router(history, settings)``.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from reviewer.config.settings import Settings
from reviewer.web.history import ReviewHistory

log = logging.getLogger(__name__)


def _make_auth_dependency(settings: Settings):
    """Создать зависимость FastAPI для HTTP Basic Auth.

    Если учётные данные не заданы — возвращает пустую зависимость,
    которая пропускает все запросы.
    """
    if not settings.web_admin_user or not settings.web_admin_password:
        return lambda: None

    security = HTTPBasic(auto_error=False)

    def _require_auth(
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> HTTPBasicCredentials | None:
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Требуется аутентификация",
                headers={"WWW-Authenticate": "Basic"},
            )
        # constant-time сравнение обоих полей: обычный != short-circuit'ит на первом
        # несовпадении и протекает по таймингу — даёт подбор логина/пароля.
        user_ok = hmac.compare_digest(
            credentials.username.encode("utf-8"),
            settings.web_admin_user.encode("utf-8"),
        )
        pass_ok = hmac.compare_digest(
            credentials.password.encode("utf-8"),
            settings.web_admin_password.encode("utf-8"),
        )
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверные учётные данные",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials

    return _require_auth


def make_router(history: ReviewHistory, settings: Settings | None = None) -> APIRouter:
    """Создать APIRouter с эндпоинтами наблюдаемости.

    Args:
        history: экземпляр ReviewHistory (зависимость через замыкание).
        settings: экземпляр Settings; если заданы web_admin_user/web_admin_password,
                  все эндпоинты защищаются HTTP Basic Auth.

    Routes:
        GET /api/runs            — список прогонов с пагинацией.
        GET /api/runs/{run_id}   — прогон + находки (404 если не найден).
        GET /api/stats           — агрегированная статистика.
    """
    auth_dep = _make_auth_dependency(settings or Settings())
    router = APIRouter(dependencies=[Depends(auth_dep)])

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

    @router.get("/api/runs/{run_id}/trace")
    def get_trace(run_id: int) -> JSONResponse:
        """Пошаговый трейс прогона, упорядоченный по seq.

        Загружается по требованию (отдельно от /api/runs/{run_id}, т.к. трейс крупный).
        Возвращает пустой список для прогонов без трейса (старые прогоны / REVIEW_TRACE=false).
        """
        try:
            steps = history.get_trace(run_id)
            return JSONResponse({"steps": steps})
        except Exception as exc:
            log.error("Ошибка при получении трейса прогона %s: %s", run_id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc

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
