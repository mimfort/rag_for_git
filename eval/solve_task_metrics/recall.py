"""Ре-экспорт расчётного ядра из reviewer/ (перенос по PRI-249)."""
from reviewer.metrics.brief_quality.recall import (  # noqa: F401
    BULK_CORE_THRESHOLD,
    QualityAggregate,
    TaskQuality,
    aggregate,
    evaluate_task,
)
