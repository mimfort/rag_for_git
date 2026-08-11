"""Блок «Окружение» репорта: только форма установки, никогда её содержимое (PRI-239).

Без окружения баг невоспроизводим, а многие дефекты модель-специфичны (эхо fingerprint
в PRI-237 воспроизводилось именно на дешёвой модели). Поэтому issue несёт отдельный
блок, который сервер собирает сам: версии, тип доски, тип VCS, бэкенд графа, дрейф,
числовые масштабы.

Разделение ответственности жёсткое. Сервер знает то, что знает только он (версии,
способ установки, зарегистрированный тип провайдера, бэкенд графа); модель сообщает
то, что знает только она (свою модель, тир, режим subagent/inline, CLI). Ни одно поле
не несёт содержимого: тип доски — да, её URL и ключ проекта — нет; факт self-hosted —
да, хост — нет; число кластеров — да, их имена — нет.

Пользователь может **урезать** блок перед публикацией: исключить любые строки или блок
целиком. Урезание не блокирует публикацию — это явный критерий приёмки.
"""
from __future__ import annotations

import platform
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from importlib import metadata

from reviewer.bugreport.sanitize import Sanitizer

#: Порядок полей в блоке. Он же — канонический список идентификаторов для урезания.
FIELD_ORDER: tuple[str, ...] = (
    "orchestrator_model",
    "subagent_models",
    "mode",
    "cli",
    "cli_version",
    "os",
    "python",
    "reviewer_version",
    "plugin_version",
    "install_mode",
    "board_type",
    "vcs_type",
    "vcs_self_hosted",
    "graph_backend",
    "index_present",
    "index_drift",
    "scale",
    "tool",
    "skill_step",
)

FIELD_LABELS: dict[str, str] = {
    "orchestrator_model": "Модель оркестратора",
    "subagent_models": "Модели субагентов",
    "mode": "Режим",
    "cli": "CLI / харнес",
    "cli_version": "Версия CLI",
    "os": "ОС и платформа",
    "python": "Python",
    "reviewer_version": "Версия reviewer",
    "plugin_version": "Версия плагина",
    "install_mode": "Способ установки",
    "board_type": "Тип доски",
    "vcs_type": "Тип VCS",
    "vcs_self_hosted": "Self-hosted VCS",
    "graph_backend": "Бэкенд графа",
    "index_present": "Индекс построен",
    "index_drift": "Дрейф индекса (коммитов)",
    "scale": "Масштабы (числа)",
    "tool": "Вызванный тул",
    "skill_step": "Шаг скилла",
}

#: Поля, которые модель вправе заполнить сама: сервер их не знает в принципе.
CLIENT_FIELDS: frozenset[str] = frozenset(
    {"orchestrator_model", "subagent_models", "mode", "cli", "cli_version",
     "tool", "skill_step"}
)

#: Числовые масштабы: только количества, без имён и содержимого.
SCALE_KEYS: tuple[str, ...] = ("clusters", "files", "findings", "tasks", "chunks", "branches")


@dataclass(frozen=True)
class EnvironmentBlock:
    """Собранный блок «Окружение»: упорядоченные пары идентификатор → значение."""

    values: dict[str, str] = field(default_factory=dict)

    def keys(self) -> list[str]:
        return [key for key in FIELD_ORDER if key in self.values]

    def trimmed(self, *, include: Collection[str] | None = None,
                exclude: Collection[str] = ()) -> "EnvironmentBlock":
        """Урезать блок: оставить только ``include`` и убрать ``exclude``.

        Пустой ``include`` (не None) означает «исключить блок целиком» — допустимый
        выбор пользователя, который не мешает публикации.
        """
        excluded = {str(item) for item in exclude}
        allowed = None if include is None else {str(item) for item in include}
        return EnvironmentBlock(
            values={
                key: value
                for key, value in self.values.items()
                if key not in excluded and (allowed is None or key in allowed)
            }
        )

    def as_markdown(self) -> str:
        """Markdown-список блока; пустой блок → явная строка, а не пустота."""
        if not self.values:
            return "_(блок исключён пользователем)_"
        return "\n".join(
            f"- **{FIELD_LABELS.get(key, key)}**: {self.values[key]}" for key in self.keys()
        )


def _distribution_version(name: str) -> str:
    try:
        return metadata.version(name)
    except Exception:      # noqa: BLE001 — отсутствие дистрибутива не должно ломать репорт
        return "unknown"


def collect_environment(
    *,
    client_fields: Mapping[str, object] | None = None,
    board_type: str = "",
    vcs_type: str = "",
    vcs_self_hosted: bool | None = None,
    graph_backend: str = "",
    index_present: bool | None = None,
    index_drift: int | None = None,
    scale: Mapping[str, object] | None = None,
    install_mode: str = "",
    sanitizer: Sanitizer | None = None,
) -> EnvironmentBlock:
    """Собрать блок «Окружение» из server-side фактов и полей, присланных моделью.

    Клиентские поля пропускаются через тот же санитайзер, что и остальной текст, и
    ограничены ``CLIENT_FIELDS``: модель не может дописать в блок произвольный ключ
    (и через него — содержимое репозитория).
    """
    clean = sanitizer or Sanitizer()
    values: dict[str, str] = {}

    for key, raw in (client_fields or {}).items():
        if key in CLIENT_FIELDS and str(raw or "").strip():
            values[key] = clean.text(raw).strip()

    values["os"] = f"{platform.system()} {platform.release()} ({platform.machine()})"
    values["python"] = platform.python_version()
    values["reviewer_version"] = _distribution_version("rag-reviewer")
    values["plugin_version"] = _plugin_version()
    if install_mode:
        values["install_mode"] = clean.text(install_mode)
    else:
        values["install_mode"] = _install_mode()
    if board_type:
        values["board_type"] = clean.text(board_type)
    if vcs_type:
        values["vcs_type"] = clean.text(vcs_type)
    if vcs_self_hosted is not None:
        # Факт self-hosted, никогда хост: инсталляция заказчика не должна быть узнаваема.
        values["vcs_self_hosted"] = "да" if vcs_self_hosted else "нет"
    if graph_backend:
        values["graph_backend"] = clean.text(graph_backend)
    if index_present is not None:
        values["index_present"] = "да" if index_present else "нет"
    if index_drift is not None:
        values["index_drift"] = str(int(index_drift))
    rendered_scale = _scale(scale)
    if rendered_scale:
        values["scale"] = rendered_scale
    return EnvironmentBlock(values={key: values[key] for key in FIELD_ORDER if key in values})


def _scale(scale: Mapping[str, object] | None) -> str:
    """Только известные ключи и только целые числа: свободный текст сюда не проходит."""
    if not scale:
        return ""
    parts: list[str] = []
    for key in SCALE_KEYS:
        raw = scale.get(key)
        if raw is None:
            continue
        try:
            parts.append(f"{key}={int(raw)}")
        except (TypeError, ValueError):
            continue
    return ", ".join(parts)


def _plugin_version() -> str:
    """Версия плагина = версия дистрибутива: манифесты собираются из pyproject."""
    return _distribution_version("rag-reviewer")


def _install_mode() -> str:
    """Способ установки по форме окружения; сбой определения не ломает репорт."""
    try:
        from reviewer.versioning import detect_installation

        return str(detect_installation().mode)
    except Exception:      # noqa: BLE001
        return "editable" if "site-packages" not in (sys.prefix or "") else "unknown"
