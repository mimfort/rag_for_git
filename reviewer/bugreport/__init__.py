"""Канал репорта багов самого reviewer: триаж, анонимизация, сборка и публикация issue (PRI-239).

Пакет намеренно не зависит от Components: санитайзер и триаж — чистые функции,
которые тестируются напрямую на отсутствие чувствительных данных в результате.
"""
from reviewer.bugreport.environment import EnvironmentBlock, collect_environment
from reviewer.bugreport.render import BugReport, render_issue
from reviewer.bugreport.sanitize import Sanitizer, sanitize_text
from reviewer.bugreport.triage import Severity, TriageVerdict, signature, triage

__all__ = [
    "BugReport",
    "EnvironmentBlock",
    "Sanitizer",
    "Severity",
    "TriageVerdict",
    "collect_environment",
    "render_issue",
    "sanitize_text",
    "signature",
    "triage",
]
