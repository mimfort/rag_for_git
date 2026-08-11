import os
from pathlib import Path
from typing import Literal

from pydantic import PrivateAttr
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
    _provider_env_file: str | Path | None = PrivateAttr(default=None)

    def __init__(self, **values) -> None:
        provider_env_file = values.get("_env_file", self.model_config.get("env_file"))
        super().__init__(**values)
        self._provider_env_file = provider_env_file

    # review tuning (дефолты; per-repo .review.yml может переопределить)
    review_severity_threshold: SeverityLevel = "medium"     # low|medium|high|critical — ниже отбрасываем
    review_min_confidence: float = 0.5            # отбрасывать findings с confidence ниже
    review_max_comments: int = 25                 # кап inline-комментариев на ревью
    review_categories: str = ""                    # CSV вайтлист категорий; пусто = все
    review_suggestions: SuggestionsMode = "apply"             # apply = applyable ```suggestion; text = только текстом
    review_max_files: int = 50                    # кап файлов PR на ревью; остальные — в сводку как пропущенные
    review_grounding_max_distance: int = 5        # макс. дистанция снапа строки к commentable при grounding
    max_tool_result_chars: int = 8000             # максимальная длина результата tool-вызова в промпт
    review_output_language: str = "ru"            # язык текста находок в публикуемом ревью
    review_skip_drafts: bool = True               # не ревьюить draft-PR
    review_history: bool = True                   # сохранять историю прогонов в Postgres
    review_session_persist: bool = True           # персист сессии PR в Postgres (crash-recovery)
    review_session_ttl_hours: int = 24            # TTL персистнутой сессии до истечения
    # Канал репорта багов самого reviewer (PRI-239). Выключатель уровня деплоя:
    # False запрещает канал для всех репозиториев, per-repo `bug_reports: false`
    # в .review.yml — только для своего. Публикация в любом случае требует апрува.
    review_bug_reports: bool = True
    # Voyage
    voyage_api_key: str = ""
    embedding_model: str = "voyage-code-3"
    embedding_dim: int = 1024
    embedding_batch_size: int = 256   # верхняя граница батча по ЧИСЛУ чанков
    # Бюджет батча по токенам (PRI-228): Voyage отвергает запрос дороже 120000
    # токенов валидационной ошибкой, которую retry не спасает. Батч набирается
    # по этому бюджету, а embedding_batch_size остаётся вторым потолком.
    embedding_token_budget: int = 100000
    rerank_model: str = "rerank-2.5"
    # stores
    pg_dsn: str = "postgresql://reviewer:reviewer@localhost:5433/reviewer"
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 4
    neo4j_uri: str = "neo4j://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "reviewerpass"
    graph_backend: GraphBackend = "auto"   # auto|scip|treesitter
    summary_cluster_depth: int = 2   # глубина пути кластера подсистемы (PRI-159); env
    # SUMMARY_CLUSTER_DEPTH; per-repo override в .review.yml; смена = полный пересбор сводок (PRI-166)
    summary_topk_threshold: int = 20   # порог масштаба для приора сводок (PRI-167): при числе
    # сводок > N query-путь get_subsystem_summaries отдаёт top-k по близости, иначе все; env
    # SUMMARY_TOPK_THRESHOLD; per-repo override в .review.yml
    summary_rebuild_cap: int | None = None   # макс. кластеров на пересборку за проход
    # (PRI-165); None/0 = безлимит; env SUMMARY_REBUILD_CAP
    # github
    github_token: str = ""
    github_retry_attempts: int = 3
    github_retry_backoff_base: float = 1.0
    # multi-platform VCS (PRI-133): тип провайдера резолвится из repo_vcs
    # (auto-derive при reviewer index), здесь — фолбэк и токены по платформе.
    vcs_provider: str = "github"          # фолбэк, когда repo_vcs пуст / remote не распознан
    gitlab_token: str = ""                # токен платформы gitlab
    gitlab_url: str = "https://gitlab.com"  # дефолт base-url; фолбэк для self-hosted
    # multi-repo: дефолтный repo для session-less тулов (search_codebase) и
    # `reviewer index` без --repo; пусто = repo задаётся явно (мульти-репо-режим)
    default_repo: str = ""
    # multi-branch: отслеживаемые ветки (CSV); первая — первичная (дефолт для
    # ветка-агностичных операций: CLI search / solve-task). PR в ветку вне списка
    # ревью пропускает. Пусто = ["main"].
    review_branches: str = "main"
    # task board (доска задач) — ГЛОБАЛЬНЫЙ дефолт деплоя: подключение к доске
    # одинаково для всех репозиториев одной команды/орга, поэтому задаётся один
    # раз в env, а не дублируется в .review.yml каждого репо. Per-repo
    # `.review.yml` task_board (из base-ветки) переопределяет это значение;
    # пустой `task_board:` в .review.yml явно выключает доску для репо.
    # Все четыре пусты = доска не настроена (task_board_default() → None).
    task_board_type: str = ""          # yougile | jira | ...
    task_board_mcp: str = ""           # имя MCP-сервера доски (инструменты mcp__<mcp>__*)
    task_board_key_pattern: str = ""   # регэксп ключа задачи, напр. [A-Z]+-\d+
    task_board_url_template: str = ""  # шаблон ссылки на задачу, напр. https://.../#{code}
    # креды и base URL REST API доски — для server-side синка (sync_board).
    # Server-internal: НЕ возвращаются board_config() (клиентам креды не утекают).
    task_board_api_key: str = ""       # ключ REST API доски (напр. YOUGILE_API_KEY)
    task_board_api_base: str = ""      # base URL REST API; пусто → дефолт по типу
    # per-type связка ключей REST-досок (форма A). yougile фолбэчит на legacy
    # TASK_BOARD_API_KEY/API_BASE (обратная совместимость старых деплоев).
    yougile_api_key: str = ""          # ключ yougile (приоритет над legacy TASK_BOARD_API_KEY)
    yougile_api_base: str = ""         # base URL yougile; пусто → дефолт по типу
    youtrack_token: str = ""           # permanent token youtrack (perm:...)
    youtrack_base_url: str = ""        # base URL youtrack API; обязателен (инстанс-специфичен)
    # вложения задач (PRI-196): лимиты скачивания/парсинга для server-side синка.
    task_attachment_max_bytes: int = 10 * 1024 * 1024   # пропуск файлов больше (байт)
    task_attachment_timeout: float = 10.0               # таймаут скачивания одного файла (с)
    task_attachment_embed_chars: int = 8000             # потолок текста на файл в эмбеддинге
    task_attachment_store_chars: int = 200000           # санити-кап текста на файл в jsonb
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

    def task_board_default(self) -> dict | None:
        """Глобальный конфиг доски из env (фолбэк, когда в .review.yml нет task_board).

        Тип доски выводится из configured_board_types() — по факту наличия REST-кредов,
        а не из TASK_BOARD_TYPE (устарел; поле pydantic сохранено для совместимости).
        Возвращает dict в форме блока ``task_board`` из .review.yml (только непустые ключи)
        или ``None``, если ничего не задано.
        """
        cfg = {}
        types = self.configured_board_types()
        if len(types) == 1:
            cfg["type"] = types[0]
        elif len(types) > 1:
            cfg["type"] = types
        if self.task_board_mcp:
            cfg["mcp"] = self.task_board_mcp
        if self.task_board_key_pattern:
            cfg["key_pattern"] = self.task_board_key_pattern
        if self.task_board_url_template:
            cfg["url_template"] = self.task_board_url_template
        return cfg or None

    def task_board_api_base_for(self, type_: str, *, registry=None,
                                credential_source=None) -> str:
        """Совместимый доступ к base URL через provider spec."""
        from reviewer.config.provider_credentials import ProviderCredentialSource
        from reviewer.tasks.boards.registry import default_board_registry

        registry = registry or default_board_registry()
        source = credential_source or ProviderCredentialSource.from_settings(self)
        try:
            spec = registry.get(type_)
        except KeyError:
            return ""
        resolved = source.resolve(spec)
        return next(
            (
                resolved.values[field.env]
                for field in spec.credential_fields
                if not field.secret and resolved.values[field.env]
            ),
            spec.default_api_base,
        )

    def board_creds(self, type_: str, *, registry=None,
                    credential_source=None) -> tuple[str, str]:
        """Совместимый tuple основных credential/base значений из provider spec."""
        from reviewer.config.provider_credentials import ProviderCredentialSource
        from reviewer.tasks.boards.registry import default_board_registry

        registry = registry or default_board_registry()
        source = credential_source or ProviderCredentialSource.from_settings(self)
        try:
            spec = registry.get(type_)
        except KeyError:
            return "", ""
        resolved = source.resolve(spec)
        api_key = next(
            (
                resolved.values[field.env]
                for field in spec.credential_fields
                if field.secret
            ),
            "",
        )
        return api_key, self.task_board_api_base_for(
            type_,
            registry=registry,
            credential_source=source,
        )

    def configured_board_types(self, *, registry=None,
                               credential_source=None) -> list[str]:
        """Типы досок с полным набором обязательных credentials из registry."""
        from reviewer.config.provider_credentials import ProviderCredentialSource
        from reviewer.tasks.boards.registry import default_board_registry

        registry = registry or default_board_registry()
        source = credential_source or ProviderCredentialSource.from_settings(self)
        return list(registry.configured_types(source))
