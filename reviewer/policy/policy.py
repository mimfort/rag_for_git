from __future__ import annotations
from dataclasses import dataclass, field
from fnmatch import fnmatch
import yaml

_SEV = {"low": 0, "medium": 1, "high": 2, "critical": 3}

@dataclass
class ReviewPolicy:
    categories: dict[str, bool] = field(default_factory=dict)
    severity_threshold: str = "low"
    ignore: list[str] = field(default_factory=list)
    max_comments: int = 25

    @classmethod
    def from_yaml(cls, text: str | None) -> "ReviewPolicy":
        if not text:
            return cls()
        data = yaml.safe_load(text) or {}
        return cls(
            categories=data.get("categories", {}),
            severity_threshold=data.get("severity_threshold", "low"),
            ignore=(data.get("paths") or {}).get("ignore", []),
            max_comments=data.get("max_comments", 25),
        )

    def category_enabled(self, category: str) -> bool:
        return self.categories.get(category, True)

    def gate(self, finding) -> bool:
        if not self.category_enabled(finding.category):
            return False
        if _SEV.get(finding.severity, 0) < _SEV.get(self.severity_threshold, 0):
            return False
        if any(fnmatch(finding.file, pat) for pat in self.ignore):
            return False
        return True
