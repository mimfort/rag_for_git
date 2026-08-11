"""Детерминированная анонимизация текста репорта бага (PRI-239).

Плагин ставят на коммерческие кодовые базы, поэтому вымарывание не доверяется LLM:
это чистые функции над текстом, покрытые negative-тестами («в результате нет подстроки X»).

Порядок замен зафиксирован и важен: сначала литералы конкретной установки (репо, ветки,
хосты, секреты), затем формы, содержащие внутри себя другие формы (URL содержит хост,
путь содержит имя файла). Обратный порядок распылил бы внешнюю форму на куски и часть
из них утекла бы.

Что модуль НЕ прячет: пути внутри дерева самого инструмента (``reviewer/…``,
``plugin/…``). Это код reviewer, а не пользователя, и без него баг невоспроизводим.
Список корней намеренно узкий: ``tests/``, ``docs/``, ``src/`` есть и у пользователя,
поэтому они вымарываются наравне с остальным.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

REDACTED = "[REDACTED]"

#: Каталоги, однозначно принадлежащие инструменту. Узкий список — сознательный выбор:
#: расширение его общими именами (tests, docs, src) открыло бы утечку путей пользователя.
TOOL_ROOTS = ("reviewer", "plugin")

#: Файлы самого инструмента, чьи имена одинаковы у всех установок и потому безлики.
#: Без этого списка `.review.yml` в тексте бага превращался бы в [FILE] и репорт терял
#: главный ориентир конфигурации.
TOOL_FILES = frozenset(
    {".review.yml", "README.md", "README.ru.md", "CLAUDE.md", "SKILL.md",
     "pyproject.toml", "docker-compose.yml", "plugin.json", ".env"}
)

PLACEHOLDER = {
    "path": "[PATH]",
    "file": "[FILE]",
    "repo": "[REPO]",
    "branch": "[BRANCH]",
    "task": "[TASK]",
    "url": "[URL]",
    "host": "[HOST]",
    "email": "[EMAIL]",
    "token": "[TOKEN]",
    "code": "[CODE]",
}

# Расширения кода/конфига: последний сегмент из этого набора означает файл, а не хост,
# и запрещает host-эвристике съесть «service.py».
_CODE_EXT = frozenset(
    """py pyi js jsx ts tsx go rs java kt rb php cs cpp cc c h hpp scala swift sql
    yml yaml toml ini cfg env json md txt lock sh""".split()
)

_TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),            # GitHub classic/oauth
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),          # GitHub fine-grained
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"),             # GitLab PAT
    re.compile(r"\bpa-[A-Za-z0-9_\-]{20,}"),                # Voyage
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),                # generic secret key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),         # Slack
    re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{12,}"),        # Authorization header
)

#: Длинный непрозрачный литерал: sha, fingerprint, ключ API доски. Применяется ПОСЛЕ
#: путей: символ ``/`` из набора исключён намеренно — с ним шаблон съедал длинный путь
#: целиком и хвост ``reviewer/…`` терялся вместе с местом падения.
_OPAQUE_PATTERNS = (
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+_\-]{40,}={0,2}\b"),
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"\b(?:https?|ssh|git)://[^\s<>\"')\]]+")
_SCP_REMOTE = re.compile(r"\bgit@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+")
# Lookbehind обязателен: без него шаблон цеплялся за «/mcp/service.py» ВНУТРИ
# сохраняемого пути `reviewer/mcp/service.py` и вымарывал его хвост.
_ABS_PATH = re.compile(
    r"(?<![A-Za-z0-9._~\-])(?:[A-Za-z]:\\[^\s\"'<>|]+|~?/[A-Za-z0-9._~\-]+(?:/[A-Za-z0-9._~\-]+)+)")
_REL_PATH = re.compile(r"\b[A-Za-z0-9._\-]+(?:[/\\][A-Za-z0-9._\-]+)+")
# Одиночное имя файла — только когда перед ним нет разделителя пути: внутри уже
# сохранённого пути `reviewer/mcp/service.py` хвост трогать нельзя.
_BARE_FILE = re.compile(
    r"(?<![/\\\w.\-])[A-Za-z0-9._\-]+\.(?:" + "|".join(sorted(_CODE_EXT)) + r")\b")
_TASK_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
_HOST = re.compile(r"\b(?:[A-Za-z0-9][A-Za-z0-9\-]*\.){1,}[A-Za-z][A-Za-z0-9\-]{1,}(?::\d{2,5})?\b")

# Строка похожа на исходник: ключевое слово в начале И пунктуация кода в строке.
# Одного ключевого слова мало — русская/английская проза начинается с «with», «return»
# и превратилась бы в [CODE] целиком.
_CODE_KEYWORD = re.compile(
    r"^\s*(?:async\s+def\s|def\s|class\s|import\s|from\s+\S+\s+import\s|@\w|return\s|"
    r"raise\s|yield\s|if\s|elif\s|for\s|while\s|try\s*:|except\s|finally\s*:|with\s|"
    r"lambda\s|const\s|let\s|var\s|function\s|public\s|private\s|func\s|fn\s)")
_CODE_PUNCT = re.compile(r"[(){}\[\]]|->|=>|::|\w\s*=\s*\S|;\s*$|:\s*$")
_TRACEBACK_LINE = re.compile(r'^\s*(?:File\s+"|Traceback\b)')


def _is_tool_path(candidate: str) -> bool:
    """Путь ведёт в дерево самого инструмента (безопасен и нужен для воспроизведения)."""
    normalized = candidate.replace("\\", "/").lstrip("./")
    return normalized.split("/", 1)[0] in TOOL_ROOTS


def _tool_tail(candidate: str) -> str | None:
    """Хвост абсолютного пути начиная с корня инструмента.

    ``/Users/kate/work/acme/reviewer/mcp/service.py`` → ``reviewer/mcp/service.py``:
    утекает именно префикс (имя пользователя, каталог заказчика), хвост безопасен.
    """
    parts = candidate.replace("\\", "/").split("/")
    for index, part in enumerate(parts):
        if part in TOOL_ROOTS:
            return "/".join(parts[index:])
    return None


@dataclass(frozen=True)
class Sanitizer:
    """Санитайзер, знающий литералы конкретной установки.

    Литералы (репо, ветки, хосты, секреты) вымарываются точным совпадением ДО эвристик:
    точное знание надёжнее регулярки и ловит формы, которые эвристика пропустит —
    например имя репозитория, упомянутое в прозе без слэша.
    """

    secrets: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        secrets: Collection[str] = (),
        repos: Collection[str] = (),
        branches: Collection[str] = (),
        hosts: Collection[str] = (),
        extra: Collection[str] = (),
    ) -> "Sanitizer":
        return cls(
            secrets=_clean(secrets),
            repos=_clean(repos),
            branches=_clean(branches),
            hosts=_clean(hosts),
            extra=_clean(extra),
        )

    def __call__(self, value: object) -> str:
        return self.text(value)

    def text(self, value: object) -> str:
        """Анонимизировать произвольный текст (многострочный — построчно)."""
        rendered = self._literals(str(value))
        return "\n".join(_line(line) for line in rendered.splitlines())

    def lines(self, values: Iterable[object]) -> list[str]:
        return [self.text(item) for item in values]

    def _literals(self, rendered: str) -> str:
        # Длинные литералы первыми: короткий префикс не должен оставлять хвост длинного.
        for secret in sorted(set(self.secrets) | set(self.extra), key=len, reverse=True):
            rendered = rendered.replace(secret, REDACTED)
        for repo in sorted(set(self.repos), key=len, reverse=True):
            rendered = rendered.replace(repo, PLACEHOLDER["repo"])
            for part in repo.split("/"):
                if len(part) >= 3 and part not in TOOL_ROOTS:
                    rendered = re.sub(rf"\b{re.escape(part)}\b", PLACEHOLDER["repo"], rendered)
        for host in sorted(set(self.hosts), key=len, reverse=True):
            rendered = rendered.replace(host, PLACEHOLDER["host"])
        for branch in sorted(set(self.branches), key=len, reverse=True):
            # Имя ветки бывает коротким и частым словом («dev»), поэтому только по
            # границам слова — иначе плейсхолдеры разнесут осмысленный текст.
            rendered = re.sub(rf"\b{re.escape(branch)}\b", PLACEHOLDER["branch"], rendered)
        return rendered


def _clean(values: Collection[str]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values if item and str(item).strip()}))


def _line(line: str) -> str:
    if _looks_like_code(line):
        # Фрагмент исходника заменяется целиком: частичная замена оставила бы
        # структуру и идентификаторы кода пользователя.
        return PLACEHOLDER["code"]
    for pattern in _TOKEN_PATTERNS:
        line = pattern.sub(PLACEHOLDER["token"], line)
    line = _EMAIL.sub(PLACEHOLDER["email"], line)
    line = _URL.sub(PLACEHOLDER["url"], line)
    line = _SCP_REMOTE.sub(PLACEHOLDER["url"], line)
    line = _ABS_PATH.sub(lambda m: _abs_path_repl(m.group(0)), line)
    line = _REL_PATH.sub(lambda m: _rel_path_repl(m.group(0)), line)
    line = _BARE_FILE.sub(lambda m: _bare_file_repl(m.group(0)), line)
    for pattern in _OPAQUE_PATTERNS:
        line = pattern.sub(PLACEHOLDER["token"], line)
    line = _TASK_KEY.sub(PLACEHOLDER["task"], line)
    line = _HOST.sub(lambda m: _host_repl(m.group(0)), line)
    return line


def _looks_like_code(line: str) -> bool:
    if _TRACEBACK_LINE.match(line):
        return False
    return bool(_CODE_KEYWORD.match(line)) and bool(_CODE_PUNCT.search(line))


def _abs_path_repl(candidate: str) -> str:
    tail = _tool_tail(candidate)
    return tail if tail else PLACEHOLDER["path"]


def _rel_path_repl(candidate: str) -> str:
    return candidate.replace("\\", "/") if _is_tool_path(candidate) else PLACEHOLDER["path"]


def _bare_file_repl(candidate: str) -> str:
    return candidate if candidate in TOOL_FILES else PLACEHOLDER["file"]


def _host_repl(candidate: str) -> str:
    """Хост вымарывается, но «service.py» и «README.md» хостами не являются."""
    label = candidate.split(":", 1)[0].rsplit(".", 1)[-1].lower()
    if label in _CODE_EXT:
        return candidate
    return PLACEHOLDER["host"]


def sanitize_text(value: object, **kwargs) -> str:
    """Разовая анонимизация без переиспользуемого санитайзера."""
    return Sanitizer.build(**kwargs).text(value)
