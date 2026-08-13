from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

from reviewer.config.settings import SeverityLevel
from reviewer.config.task_board import normalize_task_board_config
from reviewer.index.pathfilter import is_ignored
from reviewer.policy.context_limits import ContextLimits

_SEV = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Дефолтный фильтр кластеризации сводок (PRI-245): тестовые деревья бесполезны
# как высокоуровневый приор, но дают около двух третей объёма работы. Голые
# имена — is_ignored ловит и сам каталог, и всё поддерево, но не reviewer/testing.py.
DEFAULT_SUMMARY_PATHS_IGNORE: tuple[str, ...] = ("tests", "test")


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
    task_board_warnings: list[str] = field(default_factory=list)  # migration metadata, не provider options
    grounding_max_distance: int = 5                              # макс. дистанция снапа строки к commentable при grounding
    summary_cluster_depth: int = 2                               # глубина пути кластера подсистемы; per-repo override .review.yml (PRI-166)
    summary_topk_threshold: int = 20                            # порог масштаба приора сводок; per-repo override .review.yml (PRI-167)
    summary_cluster_depth_overrides: dict[str, int] = field(
        default_factory=dict)             # per-prefix depth из .review.yml (PRI-161)
    context_limits: ContextLimits = field(default_factory=ContextLimits)  # PRI-202, только из .review.yml
    bug_reports: bool = True                                     # канал репорта багов reviewer (PRI-239)
    summary_paths_ignore: list[str] = field(
        default_factory=lambda: list(DEFAULT_SUMMARY_PATHS_IGNORE)
    )   # фильтр кластеризации сводок; НЕ влияет на индекс ревью (PRI-245)

    @staticmethod
    def _summary_paths_ignore(data: Mapping[str, object]) -> list[str] | None:
        """Явный список из слоя или None, если ключ не задан/пуст (→ дефолт).

        Присутствующий непустой (в т.ч. явный `[]`) ключ заменяет дефолт целиком,
        поэтому `ignore: []` выключает фильтр. Но `ignore:` без значения — это
        YAML `None`, а не явный пустой список: как и у соседнего `paths`
        (см. `load_data`), он не должен молча выключать фильтр — только
        откатываться на дефолт.
        """
        raw = data.get("summary_paths")
        if not isinstance(raw, Mapping) or "ignore" not in raw:
            return None
        value = raw["ignore"]
        if value is None:
            return None
        return [str(item) for item in value]

    @staticmethod
    def _normalized_task_board(raw) -> tuple[dict | None, list[str]]:
        config = normalize_task_board_config(raw)
        if config is None:
            return None, []
        return config.as_dict(), list(config.warnings)

    @classmethod
    def from_yaml(cls, text: str | None) -> "ReviewPolicy":
        if not text:
            return cls()
        data = yaml.safe_load(text) or {}
        sev = data.get("severity_threshold", "low")
        if sev not in _SEV:
            sev = "low"
        task_board, task_board_warnings = cls._normalized_task_board(
            data.get("task_board"))
        summary_paths_ignore = cls._summary_paths_ignore(data)
        return cls(
            categories=data.get("categories", {}),
            severity_threshold=sev,
            ignore=(data.get("paths") or {}).get("ignore", []),
            max_comments=data.get("max_comments", 25),
            min_confidence=data.get("min_confidence", 0.5),
            output_language=str(data.get("output_language", "ru")),
            task_board=task_board,
            task_board_warnings=task_board_warnings,
            grounding_max_distance=data.get("grounding_max_distance", 5),
            summary_cluster_depth=int(data.get("summary_cluster_depth", 2)),
            summary_topk_threshold=int(data.get("summary_topk_threshold", 20)),
            summary_cluster_depth_overrides=dict(
                data.get("summary_cluster_depth_overrides", {}) or {}),
            context_limits=ContextLimits.from_review_yaml(data),
            bug_reports=bool(data.get("bug_reports", True)),
            summary_paths_ignore=(
                list(DEFAULT_SUMMARY_PATHS_IGNORE)
                if summary_paths_ignore is None
                else summary_paths_ignore
            ),
        )

    @classmethod
    def from_settings(cls, settings) -> "ReviewPolicy":
        """Дефолтная политика из env."""
        task_board, task_board_warnings = cls._normalized_task_board(
            settings.task_board_default())
        return cls(
            enabled_only=settings.review_categories_list(),
            severity_threshold=settings.review_severity_threshold,
            max_comments=settings.review_max_comments,
            min_confidence=settings.review_min_confidence,
            output_language=settings.review_output_language,
            task_board=task_board,   # глобальный env-дефолт доски
            task_board_warnings=task_board_warnings,
            grounding_max_distance=settings.review_grounding_max_distance,
            summary_cluster_depth=settings.summary_cluster_depth,
            summary_topk_threshold=settings.summary_topk_threshold,
            # getattr: from_settings вызывают и со стаб-настройками, где поля ещё нет.
            bug_reports=bool(getattr(settings, "review_bug_reports", True)),
        )

    @classmethod
    def load_data(
        cls,
        settings,
        data: Mapping[str, object] | None,
    ) -> "ReviewPolicy":
        """Apply explicit policy keys over Settings-backed defaults."""
        policy = cls.from_settings(settings)
        data = dict(data or {})
        if "categories" in data:
            policy.categories = data["categories"] or {}
            policy.enabled_only = []
        if "severity_threshold" in data and data["severity_threshold"] in _SEV:
            policy.severity_threshold = data["severity_threshold"]
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
            policy.task_board, policy.task_board_warnings = cls._normalized_task_board(
                data["task_board"])
        if "grounding_max_distance" in data:
            policy.grounding_max_distance = data["grounding_max_distance"]
        if "summary_cluster_depth" in data:
            policy.summary_cluster_depth = int(data["summary_cluster_depth"])
        if "summary_topk_threshold" in data:
            policy.summary_topk_threshold = int(data["summary_topk_threshold"])
        if "summary_cluster_depth_overrides" in data:
            policy.summary_cluster_depth_overrides = dict(
                data["summary_cluster_depth_overrides"] or {})
        if "context_limits" in data:
            policy.context_limits = ContextLimits.from_review_yaml(data)
        if "bug_reports" in data:
            policy.bug_reports = bool(data["bug_reports"])
        summary_paths_ignore = cls._summary_paths_ignore(data)
        if summary_paths_ignore is not None:
            policy.summary_paths_ignore = summary_paths_ignore
        return policy

    @classmethod
    def load(cls, settings, yaml_text: str | None) -> "ReviewPolicy":
        """Дефолты из env, поверх — переопределения из .review.yml (только заданные ключи)."""
        data = yaml.safe_load(yaml_text) if yaml_text else {}
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError("review policy YAML must contain a mapping")
        return cls.load_data(settings, data)

    def category_enabled(self, category: str) -> bool:
        if self.enabled_only:
            return category in self.enabled_only
        return self.categories.get(category, True)

    def gate_reason(self, finding) -> str | None:
        """Причина отсева находки гейтом или None, если находка проходит.

        Детерминированно возвращает первое сработавшее правило политики
        (категория/severity/confidence/путь). Строки — со стабильными
        префиксами (`category`/`severity`/`confidence`/`path`), пригодны
        для группировки в наблюдаемости.
        """
        if not self.category_enabled(finding.category):
            return f"category '{finding.category}' disabled"
        sev_f = _SEV.get(finding.severity)
        sev_t = _SEV.get(self.severity_threshold)
        if sev_f is None or sev_t is None or sev_f < sev_t:
            return f"severity '{finding.severity}' below threshold '{self.severity_threshold}'"
        if finding.confidence < self.min_confidence:
            return f"confidence {finding.confidence:.2f} below min {self.min_confidence:.2f}"
        if is_ignored(finding.file, self.ignore):
            return f"path '{finding.file}' ignored"
        return None

    def gate(self, finding) -> bool:
        return self.gate_reason(finding) is None
