from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

SeverityLevel = Literal["low", "medium", "high", "critical"]
SuggestionsMode = Literal["apply", "text"]
GraphBackend = Literal["auto", "scip", "treesitter"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
    # web admin basic auth (опционально; если не заданы — доступ без аутентификации)
    web_admin_user: str = ""
    web_admin_password: str = ""

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    def review_categories_list(self) -> list[str]:
        return self._csv(self.review_categories)
