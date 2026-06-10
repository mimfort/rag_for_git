from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    openrouter_models_fallback: str = ""
    openrouter_max_price_prompt: float | None = None
    openrouter_max_price_completion: float | None = None
    openrouter_provider_sort: str = "price"   # price|throughput|latency
    openrouter_provider_order: str = ""
    openrouter_provider_only: str = ""
    openrouter_provider_ignore: str = ""
    openrouter_allow_fallbacks: bool = True
    openrouter_require_parameters: bool = True
    openrouter_data_collection: str = "deny"
    openrouter_app_url: str = ""
    openrouter_app_title: str = ""
    openrouter_model_verify: str = ""   # модель для верификации находок; пусто = основная
    openrouter_prompt_cache: bool = True  # cache_control-блоки для Anthropic-моделей (prompt caching)
    # review tuning (дефолты; per-repo .review.yml может переопределить)
    review_max_tool_iterations: int = 12          # потолок tool-вызовов агента на файл
    review_severity_threshold: str = "medium"     # low|medium|high|critical — ниже отбрасываем
    review_min_confidence: float = 0.5            # отбрасывать findings с confidence ниже
    review_max_comments: int = 25                 # кап inline-комментариев на ревью
    review_categories: str = ""                    # CSV вайтлист категорий; пусто = все
    review_suggestions: str = "apply"             # apply = applyable ```suggestion; text = только текстом включены
    review_agentic_verify: bool = True            # агентная поштучная верификация находок
    review_synthesis: bool = True                 # кросс-файловый узел synthesize
    review_verify_min_severity: str = "high"      # порог severity для агентной проверки
    review_verify_max_iterations: int = 2         # бюджет tool-loop верификатора на находку
    review_verdict_log: str = ""                  # путь к JSONL-логу вердиктов/публикаций; пусто = выключено
    review_max_parallel_files: int = 4            # кап параллельных analyze (LangGraph max_concurrency)
    review_max_files: int = 50                    # кап файлов PR на ревью; остальные — в сводку как пропущенные
    review_skip_drafts: bool = True               # не ревьюить draft-PR
    review_history: bool = True                   # сохранять историю прогонов в Postgres
    review_trace: bool = True                     # захватывать пошаговый трейс прогона
    # Voyage
    voyage_api_key: str = ""
    embedding_model: str = "voyage-code-3"
    embedding_dim: int = 1024
    embedding_batch_size: int = 256
    rerank_model: str = "rerank-2.5"
    # stores
    pg_dsn: str = "postgresql://reviewer:reviewer@localhost:5433/reviewer"
    neo4j_uri: str = "neo4j://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "reviewerpass"
    graph_backend: str = "auto"   # auto|scip|treesitter
    # github
    github_token: str = ""

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    def openrouter_models_list(self) -> list[str]:
        return self._csv(self.openrouter_models_fallback)

    def review_categories_list(self) -> list[str]:
        return self._csv(self.review_categories)

    def openrouter_provider_block(self) -> dict:
        """Собрать объект provider для extra_body. Только заданные поля."""
        block: dict = {
            "allow_fallbacks": self.openrouter_allow_fallbacks,
            "require_parameters": self.openrouter_require_parameters,
            "data_collection": self.openrouter_data_collection,
        }
        if self.openrouter_provider_sort:
            block["sort"] = self.openrouter_provider_sort
        max_price = {}
        if self.openrouter_max_price_prompt is not None:
            max_price["prompt"] = self.openrouter_max_price_prompt
        if self.openrouter_max_price_completion is not None:
            max_price["completion"] = self.openrouter_max_price_completion
        if max_price:
            block["max_price"] = max_price
        if self._csv(self.openrouter_provider_order):
            block["order"] = self._csv(self.openrouter_provider_order)
        if self._csv(self.openrouter_provider_only):
            block["only"] = self._csv(self.openrouter_provider_only)
        if self._csv(self.openrouter_provider_ignore):
            block["ignore"] = self._csv(self.openrouter_provider_ignore)
        return block
