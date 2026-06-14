import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

SeverityLevel = Literal["low", "medium", "high", "critical"]
SuggestionsMode = Literal["apply", "text"]
GraphBackend = Literal["auto", "scip", "treesitter"]


def _resolve_env_file() -> str:
    """Найти .env независимо от рабочей директории процесса.

    MCP-клиенты (Claude Code, Cursor, Mimo и пр.) запускают `reviewer-mcp` с
    произвольным CWD, поэтому относительный `.env` подхватывается ненадёжно.
    Резолвим из стабильных мест по приоритету:
      1. $REVIEWER_ENV_FILE — явный путь (escape hatch);
      2. $XDG_CONFIG_HOME/rag-reviewer/.env (по умолчанию ~/.config/...) — канон;
      3. ./.env — удобство при запуске из корня репозитория (dev).
    Переменные реального окружения всё равно имеют приоритет над файлом
    (дефолт pydantic-settings), так что ключи можно передавать и через блок
    `env` в конфиге MCP-клиента.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates = (
        os.environ.get("REVIEWER_ENV_FILE"),
        os.path.join(xdg, "rag-reviewer", ".env"),
        ".env",
    )
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_resolve_env_file(), extra="ignore")

    # review tuning (дефолты; per-repo .review.yml может переопределить)
    review_severity_threshold: SeverityLevel = "medium"     # low|medium|high|critical — ниже отбрасываем
    review_min_confidence: float = 0.5            # отбрасывать findings с confidence ниже
    review_max_comments: int = 25                 # кап inline-комментариев на ревью
    review_categories: str = ""                    # CSV вайтлист категорий; пусто = все
    review_suggestions: SuggestionsMode = "apply"             # apply = applyable ```suggestion; text = только текстом
    review_max_files: int = 50                    # кап файлов PR на ревью; остальные — в сводку как пропущенные
    max_tool_result_chars: int = 8000             # максимальная длина результата tool-вызова в промпт
    review_output_language: str = "ru"            # язык текста находок в публикуемом ревью
    review_skip_drafts: bool = True               # не ревьюить draft-PR
    review_history: bool = True                   # сохранять историю прогонов в Postgres
    # Voyage
    voyage_api_key: str = ""
    embedding_model: str = "voyage-code-3"
    embedding_dim: int = 1024
    embedding_batch_size: int = 256
    rerank_model: str = "rerank-2.5"
    # stores
    pg_dsn: str = "postgresql://reviewer:reviewer@localhost:5433/reviewer"
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 4
    neo4j_uri: str = "neo4j://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "reviewerpass"
    graph_backend: GraphBackend = "auto"   # auto|scip|treesitter
    # github
    github_token: str = ""
    github_retry_attempts: int = 3
    github_retry_backoff_base: float = 1.0
    # multi-repo: дефолтный repo для session-less тулов (search_codebase) и
    # `reviewer index` без --repo; пусто = repo задаётся явно (мульти-репо-режим)
    default_repo: str = ""
    # multi-branch: отслеживаемые ветки (CSV); первая — первичная (дефолт для
    # ветка-агностичных операций: CLI search / solve-task). PR в ветку вне списка
    # ревью пропускает. Пусто = ["main"].
    review_branches: str = "main"
    # web admin basic auth (опционально; если не заданы — доступ без аутентификации)
    web_admin_user: str = ""
    web_admin_password: str = ""

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    def review_categories_list(self) -> list[str]:
        return self._csv(self.review_categories)

    def review_branches_list(self) -> list[str]:
        return self._csv(self.review_branches) or ["main"]

    def primary_branch(self) -> str:
        return self.review_branches_list()[0]
