"""Ре-экспорт конфигурации метрики из reviewer/ (PRI-271)."""
from reviewer.metrics.brief_quality.config import (  # noqa: F401
    DEFAULT,
    DEFAULT_BRIEFS_DIR,
    DEFAULT_CORE_PATHS,
    BriefQualityConfig,
)
