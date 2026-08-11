"""Сборка текста issue из анонимизированных частей (PRI-239).

Модуль ничего не вымарывает сам: на вход приходят уже очищенные строки. Разделение
намеренное — санитайзер тестируется отдельно на отсутствие подстрок, а рендер на
структуру, и ни один из них не может «почти правильно» сделать чужую работу.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from reviewer.bugreport.environment import EnvironmentBlock
from reviewer.bugreport.triage import Severity

#: Репозиторий инструмента: единственный адресат канала.
TARGET_REPO = "mimfort/rag_for_git"

#: Маркер в теле issue — по нему ищутся дубли среди открытых issue.
MARKER = "<!-- reviewer:bug-report -->"

_SEVERITY_LABEL: dict[str, str] = {
    "blocker": "blocker — работа невозможна, обхода нет",
    "degraded": "degraded — есть обход или ручной фолбэк",
    "contract": "contract — расхождение документации и поведения",
}


@dataclass(frozen=True)
class BugReport:
    """Готовый к публикации репорт: заголовок, тело и метаданные исхода."""

    title: str
    body: str
    severity: Severity
    signature: str
    environment: EnvironmentBlock
    labels: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "signature": self.signature,
            "environment_keys": self.environment.keys(),
            "labels": list(self.labels),
            "warnings": list(self.warnings),
        }


def render_issue(
    *,
    summary: str,
    expected: str,
    actual: str,
    steps: list[str],
    severity: Severity,
    kind_reason: str,
    signature: str,
    environment: EnvironmentBlock,
    details: str = "",
) -> BugReport:
    """Собрать issue. Все текстовые аргументы обязаны быть уже анонимизированы."""
    title = _title(summary, severity)
    sections = [
        MARKER,
        "## Что произошло",
        summary.strip() or "_(не указано)_",
        "",
        "## Ожидалось",
        expected.strip() or "_(не указано)_",
        "",
        "## Фактически",
        actual.strip() or "_(не указано)_",
        "",
        "## Шаги воспроизведения",
        _steps(steps),
        "",
        "## Классификация",
        f"- **Уровень**: {_SEVERITY_LABEL.get(severity, severity)}",
        f"- **Почему это дефект инструмента**: {kind_reason}",
        f"- **Сигнатура симптома**: `{signature}`",
        "",
        "## Окружение",
        environment.as_markdown(),
    ]
    if details.strip():
        sections += ["", "## Детали", details.strip()]
    sections += [
        "",
        "---",
        "_Отчёт собран каналом репорта багов reviewer и анонимизирован автоматически: "
        "фрагменты кода, пути, имена репозиториев, веток и файлов, ключи и URL задач, "
        "хосты и токены в него не попадают._",
    ]
    return BugReport(
        title=title,
        body="\n".join(sections).strip() + "\n",
        severity=severity,
        signature=signature,
        environment=environment,
        labels=("bug",),
    )


def _title(summary: str, severity: Severity) -> str:
    first = next((line.strip() for line in summary.splitlines() if line.strip()), "дефект reviewer")
    trimmed = first if len(first) <= 100 else first[:97].rstrip() + "…"
    return f"[{severity}] {trimmed}"


def _steps(steps: list[str]) -> str:
    cleaned = [item.strip() for item in steps if str(item).strip()]
    if not cleaned:
        return "_(не указаны)_"
    return "\n".join(f"{index}. {item}" for index, item in enumerate(cleaned, start=1))


def prefilled_url(report: BugReport, repo: str = TARGET_REPO) -> str:
    """Ссылка на форму создания issue с предзаполненным телом — фолбэк ручной публикации.

    Тело режется до 6000 символов: GitHub отбрасывает слишком длинный query, и лучше
    отдать усечённую заготовку, чем ссылку, открывающую пустую форму.
    """
    query = urllib.parse.urlencode(
        {"title": report.title, "body": report.body[:6000], "labels": ",".join(report.labels)}
    )
    return f"https://github.com/{repo}/issues/new?{query}"
