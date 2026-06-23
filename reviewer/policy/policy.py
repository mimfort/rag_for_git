from __future__ import annotations
from dataclasses import dataclass, field
from fnmatch import fnmatch
import yaml

from reviewer.config.settings import SeverityLevel

_SEV = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class ReviewPolicy:
    categories: dict[str, bool] = field(default_factory=dict)   # явный on/off (форма .review.yml)
    enabled_only: list[str] = field(default_factory=list)        # вайтлист (форма env); пусто = без вайтлиста
    severity_threshold: SeverityLevel = "low"
    ignore: list[str] = field(default_factory=list)
    max_comments: int = 25
    min_confidence: float = 0.5
    output_language: str = "ru"                                  # язык текста находок в публикуемом ревью
    task_board: dict | None = None                               # конфиг доски задач из .review.yml (None = выкл.)
    grounding_max_distance: int = 5                              # макс. дистанция снапа строки к commentable при grounding
    summary_cluster_depth: int = 2                               # глубина пути кластера подсистемы; per-repo override .review.yml (PRI-166)
    summary_topk_threshold: int = 20                            # порог масштаба приора сводок; per-repo override .review.yml (PRI-167)

    @classmethod
    def from_yaml(cls, text: str | None) -> "ReviewPolicy":
        if not text:
            return cls()
        data = yaml.safe_load(text) or {}
        sev = data.get("severity_threshold", "low")
        if sev not in _SEV:
            sev = "low"
        return cls(
            categories=data.get("categories", {}),
            severity_threshold=sev,
            ignore=(data.get("paths") or {}).get("ignore", []),
            max_comments=data.get("max_comments", 25),
            min_confidence=data.get("min_confidence", 0.5),
            output_language=str(data.get("output_language", "ru")),
            task_board=data.get("task_board") or None,
            grounding_max_distance=data.get("grounding_max_distance", 5),
            summary_cluster_depth=int(data.get("summary_cluster_depth", 2)),
            summary_topk_threshold=int(data.get("summary_topk_threshold", 20)),
        )

    @classmethod
    def from_settings(cls, settings) -> "ReviewPolicy":
        """Дефолтная политика из env."""
        return cls(
            enabled_only=settings.review_categories_list(),
            severity_threshold=settings.review_severity_threshold,
            max_comments=settings.review_max_comments,
            min_confidence=settings.review_min_confidence,
            output_language=settings.review_output_language,
            task_board=settings.task_board_default(),   # глобальный env-дефолт доски
            grounding_max_distance=settings.review_grounding_max_distance,
            summary_cluster_depth=settings.summary_cluster_depth,
            summary_topk_threshold=settings.summary_topk_threshold,
        )

    @classmethod
    def load(cls, settings, yaml_text: str | None) -> "ReviewPolicy":
        """Дефолты из env, поверх — переопределения из .review.yml (только заданные ключи)."""
        policy = cls.from_settings(settings)
        if not yaml_text:
            return policy
        data = yaml.safe_load(yaml_text) or {}
        if "categories" in data:
            policy.categories = data["categories"] or {}
            policy.enabled_only = []   # явная форма .yml отменяет env-вайтлист
        if "severity_threshold" in data:
            sev = data["severity_threshold"]
            if sev in _SEV:
                policy.severity_threshold = sev
        if "max_comments" in data:
            policy.max_comments = data["max_comments"]
        if "min_confidence" in data:
            policy.min_confidence = data["min_confidence"]
        ignore = (data.get("paths") or {}).get("ignore")
        if ignore is not None:
            policy.ignore = ignore
        if "output_language" in data:
            policy.output_language = str(data["output_language"])
        if "task_board" in data:
            policy.task_board = data["task_board"] or None
        if "grounding_max_distance" in data:
            policy.grounding_max_distance = data["grounding_max_distance"]
        if "summary_cluster_depth" in data:
            policy.summary_cluster_depth = int(data["summary_cluster_depth"])
        if "summary_topk_threshold" in data:
            policy.summary_topk_threshold = int(data["summary_topk_threshold"])
        return policy

    def category_enabled(self, category: str) -> bool:
        if self.enabled_only:
            return category in self.enabled_only
        return self.categories.get(category, True)

    def gate(self, finding) -> bool:
        if not self.category_enabled(finding.category):
            return False
        sev_f = _SEV.get(finding.severity)
        sev_t = _SEV.get(self.severity_threshold)
        if sev_f is None or sev_t is None or sev_f < sev_t:
            return False
        if finding.confidence < self.min_confidence:
            return False
        if any(fnmatch(finding.file, pat) for pat in self.ignore):
            return False
        return True
