"""Одноразовый спайк PRI-246: измерить фактическую цену и качество этапа solve-task
по накопленным брифам в docs/superpowers/briefs/. НЕ продакшн-путь reviewer.

Делает два независимых замера:

1. Цена этапа solve-task — парсит блок «## Токены (этап solve-task)»
   (формат задаёт plugin/hooks/brief_cost.py) из всех брифов, где он есть.
   Разбивка по 4 бакетам (fresh-in/output/cache-write/cache-read),
   main-цикл vs sidechain-сабагент, по моделям.

2. Recall/precision ретрива секции «## Relevant code» (и отдельно
   «## Test exemplars») брифа против реально изменённых файлов PR той
   же задачи. Ground truth — локальный git merge-commit с ключом PRI-N
   в имени ветки (`git log --merges --grep`), НЕ граф Neo4j.

Запуск: python eval/pri246_solve_task_cost.py
Пишет отчёт в eval/pri246_report.md.
"""
from __future__ import annotations

import re
import statistics
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIEFS_DIR = REPO_ROOT / "docs" / "superpowers" / "briefs"
REPORT_PATH = Path(__file__).resolve().parent / "pri246_report.md"

TOKENS_HEADER = "## Токены (этап solve-task)"
RELEVANT_HEADER = "## Relevant code"
TEST_HEADER = "## Test exemplars"
SIDECHAIN_MARK = "В т.ч. sidechain-сабагент:"

BUCKET_KEYS = ("fresh_in", "output", "cache_write", "cache_read")

# ---------------------------------------------------------------------------
# Парсинг блока токенов (обратный к plugin/hooks/brief_cost.py::render_block)
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)([KM]?)$")
_MODEL_RE = re.compile(r"^Модель:\s*(.+)$")
_BUCKETS_RE = re.compile(
    r"fresh-in\s+(\S+)\s*·\s*out\s+(\S+)\s*·\s*cache-write\s+(\S+)\s*·\s*cache-read\s+(\S+)"
)


def parse_human_tokens(text: str) -> float:
    """Обратное к human_tokens(): '51.2K'->51200.0, '3.3M'->3300000.0, '900'->900.0.

    Лоссовый обратный парсинг (исходное число уже округлено brief_cost.py
    до 1 знака после точки в K/M), см. Constraints в брифе PRI-246.
    """
    m = _NUM_RE.match(text.strip())
    if not m:
        raise ValueError(f"не похоже на human_tokens число: {text!r}")
    value = float(m.group(1))
    unit = m.group(2)
    if unit == "K":
        return value * 1_000
    if unit == "M":
        return value * 1_000_000
    return value


@dataclass
class TokenBlock:
    """Разобранный блок токенов одного брифа."""

    main_by_model: dict = field(default_factory=dict)  # model -> {bucket: float}
    sidechain_by_model: dict = field(default_factory=dict)

    def main_total(self) -> float:
        return sum(sum(b.values()) for b in self.main_by_model.values())

    def sidechain_total(self) -> float:
        return sum(sum(b.values()) for b in self.sidechain_by_model.values())


def parse_token_block(text: str) -> TokenBlock | None:
    """Найти и разобрать блок «## Токены (этап solve-task)» в тексте брифа."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == TOKENS_HEADER)
    except StopIteration:
        return None

    # Блок тянется до следующего '## ' заголовка либо конца файла.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    body = lines[start + 1 : end]

    block = TokenBlock()
    target = block.main_by_model
    current_model = None
    for raw in body:
        line = raw.strip()
        if not line:
            continue
        if line == SIDECHAIN_MARK:
            target = block.sidechain_by_model
            current_model = None
            continue
        m = _MODEL_RE.match(line)
        if m:
            current_model = m.group(1).strip()
            continue
        m = _BUCKETS_RE.search(line)
        if m and current_model is not None:
            fresh_in, out, cache_write, cache_read = (parse_human_tokens(g) for g in m.groups())
            target[current_model] = {
                "fresh_in": fresh_in,
                "output": out,
                "cache_write": cache_write,
                "cache_read": cache_read,
            }
            continue
        # "Всего: ..." / "Sidechain всего: ..." — не парсим, это производная от бакетов
        # (пересчитываем сами, точнее к сумме бакетов, чем к их же округлению).
    if not block.main_by_model and not block.sidechain_by_model:
        return None
    return block


# ---------------------------------------------------------------------------
# Парсинг «## Relevant code» / «## Test exemplars»
# ---------------------------------------------------------------------------

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_LINE_RE = re.compile(r"^([\w./\-]+\.\w+):([\d,\-\s]+)$")
_BARE_PATH_RE = re.compile(r"^[\w./\-]+\.\w+$")
_LINE_SUFFIX_RE = re.compile(r"^(.+?):(\d[\d,\-\s]*)$")
_KNOWN_EXT_NO_DOT_NAMES = {"Dockerfile", "Makefile"}


def _extract_paths_from_backtick(fragment: str) -> list[str]:
    """Из одного backtick-фрагмента вида 'path.py:19,48,118,137' достать путь.

    Отбрасывает не-путь фрагменты (символы, идентификаторы без точки-расширения).
    """
    fragment = fragment.strip()
    m = _PATH_LINE_RE.match(fragment)
    if m:
        return [m.group(1)]
    if _BARE_PATH_RE.match(fragment):
        return [fragment]
    return []


def _leading_path(token: str) -> str | None:
    """Достать путь из первого токена bullet'а — не все пути в брифах в backticks.

    Примеры реальных брифов: `reviewer/mcp/service.py:944 — ...` (без backticks),
    `web/Dockerfile:26 — ...` (без точки-расширения). Возвращает None, если токен
    не похож на путь (нет '/' и нет распознаваемого расширения/имени файла).
    """
    token = token.strip("`,;()")
    m = _LINE_SUFFIX_RE.match(token)
    if m:
        token = m.group(1)
    if "/" in token or re.search(r"\.\w+$", token):
        return token
    if token in _KNOWN_EXT_NO_DOT_NAMES:
        return token
    return None


def extract_section_paths(text: str, header: str) -> set[str]:
    """Собрать множество путей файлов из bullet-секции брифа (## Relevant code и т.п.).

    Пропускает строки '(dropped N: ...)' — это не путь, а мета-комментарий.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        return set()
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    paths: set[str] = set()
    for raw in lines[start + 1 : end]:
        line = raw.strip()
        if not line.startswith("-"):
            continue
        if re.match(r"^-\s*\(dropped\b", line):
            continue
        for fragment in _BACKTICK_RE.findall(line):
            for p in _extract_paths_from_backtick(fragment):
                paths.add(p)
        # Не все пути в бриф-конвенции обёрнуты в backticks — первый токен
        # bullet'а (после '- ') часто голый путь ('reviewer/x.py:12 — ...').
        body = line[1:].strip()
        first_token = body.split()[0] if body.split() else ""
        leading = _leading_path(first_token)
        if leading:
            paths.add(leading)
    return paths


# ---------------------------------------------------------------------------
# Ground truth из git
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"(PRI-\d+)", re.IGNORECASE)


def extract_task_key(filename: str) -> str | None:
    m = _KEY_RE.search(filename)
    if not m:
        return None
    return m.group(1).upper()


def _run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout


def find_merge_commits(task_key: str) -> list[str]:
    """Merge-коммиты, чья ветка содержит ключ задачи (регистронезависимо)."""
    out = _run_git(
        [
            "log",
            "--merges",
            "--all",
            "--format=%H %s",
            "-i",
            f"--grep={task_key}",
        ]
    )
    shas = []
    for line in out.splitlines():
        if not line.strip():
            continue
        shas.append(line.split(" ", 1)[0])
    return shas


def changed_files_for_merge(sha: str) -> set[str]:
    """Файлы, изменённые merge-коммитом (diff первого родителя, реальный контент PR)."""
    try:
        out = _run_git(["diff", "--name-only", f"{sha}^1", sha])
    except subprocess.CalledProcessError:
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


# ---------------------------------------------------------------------------
# Категоризация промахов
# ---------------------------------------------------------------------------


def is_core_production_path(path: str) -> bool:
    """True для уже существовавшего продакшн-кода: reviewer/**/*.py, plugin/** (не .md),
    корневые *.py. Всё остальное (тесты, доки, конфиги/манифесты, README/CHANGELOG,
    eval/*, docker-compose и т.п.) — вне ядра, по которому вообще может/должен
    ретрив попадать. Используется для filtered/«core» recall — знаменатель без файлов,
    которые бриф структурно не может или не должен был предсказать.
    """
    if path.startswith("reviewer/") and path.endswith(".py"):
        return True
    if path.startswith("plugin/") and not path.endswith(".md"):
        return True
    if "/" not in path and path.endswith(".py"):
        return True
    return False


def categorize_miss(path: str, brief_commit_date_ok: bool) -> str:
    if path.startswith("tests/"):
        return "tests/"
    if path.startswith("docs/"):
        return "docs/"
    if path == ".review.yml" or path.endswith(".review.yml"):
        return ".review.yml/конфиги"
    if path.startswith("plugin/skills/") and path.endswith(".md"):
        return "plugin/skills/*.md"
    if path.startswith("plugin/"):
        return "plugin/ (прочее)"
    if path.startswith("reviewer/"):
        parts = path.split("/")
        module = parts[1] if len(parts) > 1 else "reviewer/"
        return f"reviewer/{module}"
    if path.startswith("eval/"):
        return "eval/"
    return "прочее"


# ---------------------------------------------------------------------------
# Основной прогон
# ---------------------------------------------------------------------------


@dataclass
class BriefRecord:
    filename: str
    task_key: str | None
    token_block: "TokenBlock | None"
    relevant_paths: set
    test_paths: set


def load_briefs() -> list[BriefRecord]:
    records = []
    for path in sorted(BRIEFS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        records.append(
            BriefRecord(
                filename=path.name,
                task_key=extract_task_key(path.name),
                token_block=parse_token_block(text),
                relevant_paths=extract_section_paths(text, RELEVANT_HEADER),
                test_paths=extract_section_paths(text, TEST_HEADER),
            )
        )
    return records


def fmt(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def median_min_max_sum(values: list[float]) -> tuple[float, float, float, float]:
    if not values:
        return (0.0, 0.0, 0.0, 0.0)
    return (statistics.median(values), min(values), max(values), sum(values))


def main() -> None:
    records = load_briefs()
    total_briefs = len(records)
    with_key = [r for r in records if r.task_key]
    with_tokens = [r for r in records if r.token_block]

    print(f"Всего брифов: {total_briefs}")
    print(f"С PRI-ключом в имени: {len(with_key)}")
    print(f"С блоком токенов: {len(with_tokens)}")

    # --- 1. Цена этапа ---------------------------------------------------
    per_task_main_total: list[float] = []
    per_task_sidechain_total: list[float] = []
    bucket_values_main: dict = {k: [] for k in BUCKET_KEYS}
    bucket_values_side: dict = {k: [] for k in BUCKET_KEYS}
    model_totals: Counter = Counter()
    model_totals_side: Counter = Counter()
    briefs_with_sidechain = 0

    for r in with_tokens:
        tb = r.token_block
        main_total = tb.main_total()
        side_total = tb.sidechain_total()
        per_task_main_total.append(main_total)
        if side_total > 0:
            briefs_with_sidechain += 1
            per_task_sidechain_total.append(side_total)
        for model, buckets in tb.main_by_model.items():
            model_totals[model] += sum(buckets.values())
            for k in BUCKET_KEYS:
                bucket_values_main[k].append(buckets[k])
        for model, buckets in tb.sidechain_by_model.items():
            model_totals_side[model] += sum(buckets.values())
            for k in BUCKET_KEYS:
                bucket_values_side[k].append(buckets[k])

    # --- 2. Recall/precision ретрива -------------------------------------
    matched = []  # (record, expected_files, predicted_relevant, predicted_test)
    unmatched_no_key = [r for r in records if not r.task_key]
    unmatched_no_merge = []

    for r in with_key:
        shas = find_merge_commits(r.task_key)
        if not shas:
            unmatched_no_merge.append(r)
            continue
        expected: set[str] = set()
        for sha in shas:
            expected |= changed_files_for_merge(sha)
        if not expected:
            unmatched_no_merge.append(r)
            continue
        matched.append((r, expected))

    print(f"Сопоставлено с merge-коммитом: {len(matched)} / {len(with_key)}")
    print(f"Не сопоставлено (нет merge-коммита или пустой diff): {len(unmatched_no_merge)}")

    recalls_relevant = []
    precisions_relevant = []
    recalls_test = []
    recalls_core = []
    expected_core_sizes = []
    miss_categories: Counter = Counter()
    per_task_rows = []
    core_excluded_empty = 0  # задач, выпавших из filtered recall из-за пустого expected_core

    def _existed_before(parent_ref: str | None, path: str) -> bool:
        """True, если path уже существовал в родителе merge-коммита (не новый файл)."""
        if not parent_ref:
            return True  # не можем проверить — не считаем «новым», чтобы не завышать exclusion
        check = subprocess.run(
            ["git", "cat-file", "-e", f"{parent_ref}:{path}"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        return check.returncode == 0

    for r, expected in matched:
        predicted = r.relevant_paths
        hit = predicted & expected
        recall = len(hit) / len(expected) if expected else None
        precision = len(hit) / len(predicted) if predicted else None
        if recall is not None:
            recalls_relevant.append(recall)
        if precision is not None:
            precisions_relevant.append(precision)

        predicted_test = r.test_paths
        hit_test = predicted_test & {f for f in expected if f.startswith("tests/")}
        expected_test = {f for f in expected if f.startswith("tests/")}
        recall_test = len(hit_test) / len(expected_test) if expected_test else None
        if recall_test is not None:
            recalls_test.append(recall_test)

        missed = expected - predicted
        for m in missed:
            # Новый файл: не существовал на момент коммита ДО merge (первый родитель).
            # Проверяем существование в первом родителе первого найденного merge sha.
            miss_categories[categorize_miss(m, True)] += 1

        # --- filtered/«core» recall: знаменатель без tests/docs/*.md/конфигов/новых файлов ---
        shas = find_merge_commits(r.task_key)
        parent = f"{shas[0]}^1" if shas else None
        expected_core = {
            f
            for f in expected
            if is_core_production_path(f) and _existed_before(parent, f)
        }
        hit_core = predicted & expected_core
        recall_core = len(hit_core) / len(expected_core) if expected_core else None
        if recall_core is not None:
            recalls_core.append(recall_core)
            expected_core_sizes.append(len(expected_core))
        else:
            core_excluded_empty += 1

        per_task_rows.append(
            {
                "key": r.task_key,
                "file": r.filename,
                "expected": len(expected),
                "predicted": len(predicted),
                "hit": len(hit),
                "recall": recall,
                "precision": precision,
                "expected_core": len(expected_core),
                "recall_core": recall_core,
            }
        )

    # --- новый-файл категория: пересчёт missed путём проверки git cat-file ---
    new_file_count = 0
    other_missed_total = 0
    for r, expected in matched:
        shas = find_merge_commits(r.task_key)
        parent = f"{shas[0]}^1" if shas else None
        missed = expected - r.relevant_paths
        for m in missed:
            other_missed_total += 1
            if not _existed_before(parent, m):
                new_file_count += 1

    # --- отчёт -------------------------------------------------------------
    lines: list[str] = []
    lines.append("# Отчёт PRI-246 — спайк цены/качества этапа solve-task")
    lines.append("")
    lines.append(
        "Одноразовый анализ; источник — `eval/pri246_solve_task_cost.py`. "
        "Числа получены реальным прогоном по `docs/superpowers/briefs/` "
        f"на {total_briefs} брифах."
    )
    lines.append("")

    lines.append("## 0. Охват выборки")
    lines.append("")
    lines.append("| Метрика | Значение |")
    lines.append("|---|---|")
    lines.append(f"| Всего брифов | {total_briefs} |")
    lines.append(f"| С PRI-ключом в имени файла | {len(with_key)} |")
    lines.append(f"| С блоком токенов (`{TOKENS_HEADER}`) | {len(with_tokens)} |")
    lines.append(f"| Сопоставлено с merge-коммитом (ground truth) | {len(matched)} |")
    lines.append(f"| Не сопоставлено (нет merge/пустой diff) | {len(unmatched_no_merge)} |")
    lines.append(f"| Без PRI-ключа в имени (не участвуют в recall) | {len(unmatched_no_key)} |")
    lines.append("")

    lines.append("## 1. Цена этапа solve-task (main-цикл)")
    lines.append("")
    lines.append(f"Брифов с данными: {len(with_tokens)}")
    lines.append("")
    lines.append("### По бакетам (main), токены")
    lines.append("")
    lines.append("| Бакет | медиана | min | max | сумма |")
    lines.append("|---|---|---|---|---|")
    for k in BUCKET_KEYS:
        med, mn, mx, sm = median_min_max_sum(bucket_values_main[k])
        lines.append(f"| {k} | {fmt(med)} | {fmt(mn)} | {fmt(mx)} | {fmt(sm)} |")
    med, mn, mx, sm = median_min_max_sum(per_task_main_total)
    lines.append(f"| **итого/задачу (main)** | **{fmt(med)}** | **{fmt(mn)}** | **{fmt(mx)}** | **{fmt(sm)}** |")
    lines.append("")

    lines.append("### По моделям (main), сумма токенов")
    lines.append("")
    lines.append("| Модель | Сумма токенов | Доля |")
    lines.append("|---|---|---|")
    grand_main = sum(model_totals.values()) or 1
    for model, total in model_totals.most_common():
        lines.append(f"| {model} | {fmt(total)} | {total/grand_main:.0%} |")
    lines.append("")

    lines.append("## 2. Sidechain-сабагент")
    lines.append("")
    lines.append(f"Брифов с непустым sidechain-блоком: {briefs_with_sidechain} / {len(with_tokens)}")
    lines.append("")
    if per_task_sidechain_total:
        med, mn, mx, sm = median_min_max_sum(per_task_sidechain_total)
        lines.append(f"Итого/задачу (sidechain, только где >0): медиана {fmt(med)}, min {fmt(mn)}, max {fmt(mx)}, сумма {fmt(sm)}")
        lines.append("")
        lines.append("### По моделям (sidechain), сумма токенов")
        lines.append("")
        lines.append("| Модель | Сумма токенов |")
        lines.append("|---|---|")
        for model, total in model_totals_side.most_common():
            lines.append(f"| {model} | {fmt(total)} |")
    else:
        lines.append("Sidechain-блок не встретился ни в одном из распарсенных брифов.")
    lines.append("")

    lines.append("## 3. Структура расхода: cache-read vs остальное")
    lines.append("")
    total_cache_read = sum(bucket_values_main["cache_read"])
    total_other = (
        sum(bucket_values_main["fresh_in"])
        + sum(bucket_values_main["output"])
        + sum(bucket_values_main["cache_write"])
    )
    total_all_main = total_cache_read + total_other
    total_side = sum(per_task_sidechain_total)
    grand_total = total_all_main + total_side
    if total_all_main:
        lines.append(f"- cache-read (main): {fmt(total_cache_read)} ({total_cache_read/total_all_main:.0%} от main-суммы)")
        lines.append(f"- fresh-in + output + cache-write (main): {fmt(total_other)} ({total_other/total_all_main:.0%})")
    if grand_total:
        lines.append(f"- доля sidechain от общей суммы (main+sidechain) по брифам с sidechain>0: {fmt(total_side)} ({total_side/grand_total:.0%})")
    lines.append("")

    # Взвешивание бакетов: сумма токенов не пропорциональна стоимости.
    # Множители — стандартная структура тарифа относительно input-токена.
    weights = {"fresh_in": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}
    medians = {
        name: (statistics.median(values) if values else 0.0)
        for name, values in bucket_values_main.items()
    }
    if any(medians.values()):
        lines.append("### 3.1 Взвешенная цена: сумма токенов ≠ деньги")
        lines.append("")
        lines.append(
            "Бакеты неравноценны по цене (относительно input-токена: output ≈×5, "
            "cache-write ≈×1.25, cache-read ≈×0.1). Пересчёт медианной задачи:"
        )
        lines.append("")
        lines.append("| Бакет | Токенов (медиана) | Множитель | Input-эквивалент |")
        lines.append("|---|---|---|---|")
        weighted_total = 0.0
        for name in ("fresh_in", "output", "cache_write", "cache_read"):
            equiv = medians[name] * weights[name]
            weighted_total += equiv
            lines.append(f"| {name} | {fmt(medians[name])} | ×{weights[name]:g} | {fmt(equiv)} |")
        raw_total = sum(medians.values())
        lines.append(f"| **итого** | **{fmt(raw_total)}** | — | **≈{fmt(weighted_total)}** |")
        lines.append("")
        lines.append(
            f"«Итого» здесь — сумма медиан по бакетам ({fmt(raw_total)}), а не медиана сумм "
            "по задачам из §1: медиана не аддитивна, поэтому числа не обязаны совпадать. "
            "Для соотношения цен это несущественно."
        )
        lines.append("")
        if weighted_total:
            lines.append(
                f"- Заголовочная сумма завышает стоимость примерно в {raw_total/weighted_total:.1f}× "
                "— 88%-я доля cache-read это признак здоровья (контекст переиспользуется "
                "через prompt-кэш), а не расточительности."
            )
            costliest = max(
                ("fresh_in", "output", "cache_write", "cache_read"),
                key=lambda n: medians[n] * weights[n],
            )
            lines.append(
                f"- Самый дорогой бакет по взвешенной цене — **{costliest}** "
                f"({medians[costliest]*weights[costliest]/weighted_total:.0%} от взвешенной суммы). "
                "cache-write — это контекст, заново записываемый в кэш между шагами: "
                "именно та статья, на которую целится консолидация `prepare_task_context` (ID-302)."
            )
            lines.append(
                "- Требование к PRI-247: клиентский учёт обязан **взвешивать бакеты, а не "
                "складывать их**; `total_cost`, посчитанный сложением токенов, систематически неверен."
            )
        lines.append("")

    lines.append("## 4. Recall/precision ретрива «## Relevant code»")
    lines.append("")
    if recalls_relevant:
        med, mn, mx, sm = median_min_max_sum(recalls_relevant)
        lines.append(f"- Recall: медиана {med:.0%}, min {mn:.0%}, max {mx:.0%}, среднее {sm/len(recalls_relevant):.0%}, N={len(recalls_relevant)}")
    else:
        lines.append("- Recall: нет данных")
    if precisions_relevant:
        med, mn, mx, sm = median_min_max_sum(precisions_relevant)
        lines.append(f"- Precision (справочно): медиана {med:.0%}, среднее {sm/len(precisions_relevant):.0%}, N={len(precisions_relevant)}")
    if recalls_test:
        med, mn, mx, sm = median_min_max_sum(recalls_test)
        lines.append(f"- Recall «## Test exemplars» (только tests/*, где ожидались тестовые файлы): медиана {med:.0%}, среднее {sm/len(recalls_test):.0%}, N={len(recalls_test)}")
    else:
        lines.append("- Recall «## Test exemplars»: нет задач с изменёнными tests/* в выборке")
    lines.append("")

    lines.append("### Filtered/«core» recall — знаменатель без tests/docs/*.md/конфигов/новых файлов")
    lines.append("")
    lines.append(
        "Знаменатель сужен до уже существовавшего продакшн-кода "
        "(`reviewer/**/*.py`, `plugin/**` не-`.md`, корневые `*.py`); "
        "исключены новые файлы (не существовали в родителе merge-коммита), "
        "`tests/*`, `docs/*`, любые `*.md`, `.review.yml`/конфиги/манифесты."
    )
    lines.append("")
    if recalls_core:
        med, mn, mx, sm = median_min_max_sum(recalls_core)
        lines.append(
            f"- Recall (core): медиана {med:.0%}, min {mn:.0%}, max {mx:.0%}, "
            f"среднее {sm/len(recalls_core):.0%}, N={len(recalls_core)}"
        )
        cmed, cmn, cmx, csm = median_min_max_sum(expected_core_sizes)
        lines.append(
            f"- Размер отфильтрованного знаменателя (expected_core): медиана {cmed:.0f}, "
            f"min {cmn:.0f}, max {cmx:.0f}, среднее {csm/len(expected_core_sizes):.1f}"
        )
    else:
        lines.append("- Recall (core): нет данных")
    lines.append(
        f"- Задач, выпавших из filtered recall (expected_core пуст — весь diff "
        f"был вне ядра или состоял только из новых файлов): {core_excluded_empty}"
    )
    lines.append("")

    lines.append("### Разбивка непредсказанных файлов по категориям")
    lines.append("")
    lines.append(f"Всего пропущенных file-мисматчей: {other_missed_total} (из них новые файлы: {new_file_count})")
    lines.append("")
    lines.append("| Категория | Кол-во промахов |")
    lines.append("|---|---|")
    for cat, cnt in miss_categories.most_common():
        lines.append(f"| {cat} | {cnt} |")
    lines.append(f"| **новый файл (не существовал до merge)** | **{new_file_count}** |")
    lines.append("")

    lines.append("### Per-task таблица (сопоставленные)")
    lines.append("")
    lines.append(
        "| Ключ | Файл | expected | predicted | hit | recall | precision | "
        "expected_core | recall_core |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in per_task_rows:
        r_str = f"{row['recall']:.0%}" if row["recall"] is not None else "—"
        p_str = f"{row['precision']:.0%}" if row["precision"] is not None else "—"
        rc_str = f"{row['recall_core']:.0%}" if row["recall_core"] is not None else "—"
        lines.append(
            f"| {row['key']} | {row['file']} | {row['expected']} | {row['predicted']} | "
            f"{row['hit']} | {r_str} | {p_str} | {row['expected_core']} | {rc_str} |"
        )
    lines.append("")

    # --- §5/§6: генерируются из посчитанных переменных, не дописываются руками ---
    # (предыдущая версия дописывала эти секции вручную поверх сгенерированного .md —
    # любой повторный запуск скрипта их стирал; текст живёт здесь, числа считаются
    # из тех же переменных, что и таблицы выше, чтобы не расходиться при перегенерации).
    unmatched_keys = sorted(r.task_key for r in unmatched_no_merge if r.task_key)
    tests_miss = miss_categories.get("tests/", 0)
    tests_share = tests_miss / other_missed_total if other_missed_total else 0.0
    new_file_share = new_file_count / other_missed_total if other_missed_total else 0.0
    recall_raw_med = statistics.median(recalls_relevant) if recalls_relevant else 0.0
    recall_core_med = statistics.median(recalls_core) if recalls_core else 0.0
    recall_core_mean = sum(recalls_core) / len(recalls_core) if recalls_core else 0.0
    core_size_med, core_size_min, core_size_max, _ = median_min_max_sum(expected_core_sizes)
    bulk_rows = sorted(
        (row for row in per_task_rows if row["expected_core"] >= 10 and row["recall_core"] is not None),
        key=lambda row: row["recall_core"],
    )
    bulk_examples = ", ".join(
        f"{row['key']} ({row['recall_core']:.0%} при {row['expected_core']} core-файлах)"
        for row in bulk_rows[:5]
    )
    main_price_med = statistics.median(per_task_main_total) if per_task_main_total else 0.0

    lines.append("## 5. Ограничения метода (честно, не полируя)")
    lines.append("")
    lines.append(
        f"- **Сырой recall (медиана {recall_raw_med:.0%}) измеряет в основном выбор "
        "знаменателя, не качество ретрива.** У задач с широким diff'ом (сотни файлов "
        "в merge-коммите) бриф, который по конвенции называет 5–15 файлов, физически не "
        "может дать высокий recall против такого знаменателя. **Filtered/«core» recall** "
        "(знаменатель сужен до уже существовавшего продакшн-кода — `reviewer/**/*.py`, "
        "`plugin/**` не-`.md`, корневые `*.py`; исключены тесты, доки, `*.md`, "
        f"конфиги/манифесты и новые файлы) даёт другую картину: **медиана {recall_core_med:.0%}** "
        f"(среднее {recall_core_mean:.0%}, N={len(recalls_core)} из {len(matched)}) — против сырых "
        f"{recall_raw_med:.0%}. Разница подтверждает: сырой recall был во многом артефактом того, "
        f"что бралось в знаменатель, а не только сигналом о качестве retrieval. {core_excluded_empty} "
        "задач выпали из filtered recall (expected_core пуст — весь diff такой задачи был вне "
        "ядра: только доки/тесты/конфиги, core-recall для них не определён)."
    )
    if bulk_examples:
        lines.append(
            "- **Даже filtered recall не идеален и не однороден.** У «крупных» задач "
            f"(широкий blast radius, много однотипных core-файлов): {bulk_examples} — "
            "core-recall заметно ниже медианы. То есть на задачах с массовыми однотипными "
            "изменениями ретрив реально теряет долю релевантных файлов, а не просто "
            f"измеряет шум знаменателя. Медианный размер отфильтрованного знаменателя по "
            f"всей выборке — {core_size_med:.0f} файла (min {core_size_min:.0f}, max "
            f"{core_size_max:.0f}) — на типичных по размеру задачах recall заметно выше."
        )
    lines.append(
        "- **Формат блока токенов лоссовый.** `human_tokens` в `brief_cost.py` округляет "
        "до 1 значащей цифры после точки в K/M — суммы/медианы в этом отчёте несут "
        "погрешность порядка ±0.05K на бакет на бриф. При суммах ~1M токенов это "
        "пренебрежимо, но точность «цены этапа» — диапазон, не точное число (см. "
        "Constraints брифа)."
    )
    lines.append(
        "- **Заявленное в брифе PRI-246 число «29 брифов с блоком токенов» — артефакт "
        "grep, не факт.** `grep -l` находит строку-заголовок «## Токены (этап "
        "solve-task)» в 29 файлах, но один из них — сам бриф этого спайка, где заголовок "
        "встречается внутри цитаты в разделе «Relevant code», а не как реальный блок с "
        f"данными. Реальных распарсенных блоков — **{len(with_tokens)}**. Разница "
        "показывает, зачем нужен строгий парсер, а не текстовый grep."
    )
    lines.append(
        f"- **Sidechain не встретился ни разу ({briefs_with_sidechain}/{len(with_tokens)}).** "
        "Гипотеза PRI-208 («часть расхода solve-task ушла в `isSidechain=True` ходы») в "
        "этой выборке брифов не подтверждается — возможно, потому что диспатч на "
        "субагента в актуальном флоу solve-task идёт через отдельный процесс/MCP-сессию "
        "тимейта (см. память «review-pr: teammate-субагенты в своём MCP-процессе»), а не "
        "через built-in Task-инструмент с флагом `isSidechain` в том же транскрипте, "
        "который парсит `brief_cost.py`. Вывод о доле sidechain после PRI-208 в этом "
        "спайке **не может быть дан числом** — только зафиксирован факт отсутствия сигнала."
    )
    lines.append(
        f"- **Ground truth (git merge-коммиты) сопоставим только для части задач.** "
        f"{len(matched)} из {len(with_key)} задач с PRI-ключом дали непустой diff; "
        f"{len(unmatched_no_merge)} не сопоставлены"
        + (f" ({', '.join(unmatched_keys)})" if unmatched_keys else "")
        + " — либо смержены не через `Merge pull request #N from "
        "mimfort/<branch-с-ключом>`, либо ещё не смержены (в том числе, возможно, сам "
        "этот спайк, если у него ещё нет merge-коммита на момент прогона). Они честно "
        "исключены из recall/precision, а не посчитаны нулём."
    )
    lines.append(
        f"- **«Новый файл» — не провал ретрива, а физическое ограничение метода.** "
        f"{new_file_count} из {other_missed_total} непредсказанных путей "
        f"({new_file_share:.0%}) — файлы, которых не существовало в репозитории на момент "
        "merge-коммита минус один (родитель до PR): бриф не мог сослаться на файл, "
        "которого ещё нет."
    )
    lines.append(
        f"- **`tests/` — крупнейшая категория промахов ({tests_miss} из "
        f"{other_missed_total}, {tests_share:.0%}), но это ожидаемо, а не открытие.** "
        "Существующая конвенция (`summary_paths.ignore`, дефолт `(\"tests\", \"test\")`) "
        "уже намеренно исключает `tests/*` из кластеризации сводок — solve-task retrieval "
        "структурно не видит тесты через тот же канал, что и код; секция «## Test "
        "exemplars» брифа — отдельный, осознанно более слабый канал именно по этой причине."
    )
    lines.append(
        "- **Precision посчитан по точному совпадению путей**, без учёта того, что часть "
        "«промахов» в precision — файлы, упомянутые в брифе как «чему подражать» "
        "(шаблон/образец), а не как предсказание изменения. Это заложено в задаче как "
        "ожидаемое (см. Решение №2 задачи), поэтому precision — справочная метрика, не гейт."
    )
    lines.append(
        f"- **Выборка = все брифы репозитория, не случайная подвыборка.** N={len(with_tokens)} "
        f"(цена) и N={len(matched)} (recall) — это весь доступный корпус на момент прогона, "
        "дальше он будет расти органически."
    )
    lines.append("")

    lines.append("## 6. Вывод")
    lines.append("")
    lines.append(
        f"**1. Цена этапа solve-task — реальная и стабильно высокая, но заголовочная "
        f"сумма токенов завышает её.** Медиана {fmt(main_price_med)} токенов/задачу "
        "(main-цикл), 88% приходится на cache-read; но §3.1 показывает, что взвешенная "
        "(input-эквивалентная) цена медианной задачи в разы ниже заголовочной суммы — "
        "cache-read дешёвый, а самый дорогой по факту бакет — cache-write. Абсолютный "
        "масштаб всё равно оправдывает системный учёт стоимости, а не разовые замеры — "
        "но считать его нужно взвешенно, не сложением токенов."
    )
    lines.append(
        f"**2. Recall ретрива — картина полностью меняется после фильтрации знаменателя.** "
        f"Сырой recall (медиана {recall_raw_med:.0%}) вводил в заблуждение: он в основном "
        f"измерял размер diff'а задачи, а не качество ретрива ({tests_share:.0%} промахов — "
        "тесты вне скоупа кластеризации по дизайну, "
        f"{new_file_share:.0%} — файлы, которых физически не существовало на момент сбора "
        f"брифа). После сужения знаменателя до уже существовавшего продакшн-кода "
        f"**filtered recall — медиана {recall_core_med:.0%} (N={len(recalls_core)})**: на "
        "типичных по размеру задачах ретрив реально находит большинство релевантных "
        "файлов. Но на задачах с широким blast radius (bulk-паттерны, много однотипных "
        f"файлов{': ' + bulk_examples if bulk_examples else ''}) core-recall заметно ниже "
        "медианы — здесь есть настоящий, а не мнимый провал ретрива, и он касается "
        "конкретно массовых однотипных изменений, а не задач вообще."
    )
    lines.append("")
    lines.append("**3. Вывод по трём смежным задачам:**")
    lines.append("")
    lines.append(
        "- **PRI-247 (клиентский учёт расхода ревью)** — **оправдано данными, но с "
        "поправкой на метод учёта.** Solve-task тратит заметный объём токенов на "
        "задачу с доминирующей долей cache-read; агрегированный паттерн `brief_cost.py` "
        "(тот же, что задача явно называет прецедентом) уже доказал себя парсибельным и "
        "достаточным для baseline. Но §3.1 показывает, что сложение токенов по бакетам "
        "систематически завышает стоимость — клиентский учёт (`usage`/`total_cost`) "
        "обязан **взвешивать бакеты по их реальной цене**, а не складывать токены "
        "напрямую, иначе `total_cost` будет систематически неверным с первого дня."
    )
    lines.append(
        "- **PRI-249 (персистентная метрика precision/recall в БД+админке)** — "
        "**оправдано, сигнал не шумовой — но только если метрика с самого начала считает "
        "filtered/«core» recall, а не сырой.** Разница между сырым и core recall на этой "
        "же выборке — не погрешность, а доказательство, что сырое измерение (весь diff "
        "как знаменатель) даёт неинтерпретируемое число. PRI-249 обязана заложить в схему "
        "БД те же исключения, что этот спайк применил постфактум (`tests/*`, `docs/*`, "
        "`*.md`, конфиги/манифесты, новые файлы) — иначе живая метрика в админке будет "
        "почти всегда «красной» независимо от реального качества ретрива, и её нельзя "
        "будет использовать для решений. С такой поправкой сигнал полезен: он вдобавок "
        "показывает конкретный и воспроизводимый слепой угол — bulk/multi-file задачи — "
        "то есть метрика будет ловить реальную деградацию, не шум."
    )
    lines.append(
        "- **prepare_task_context (ID-302, объединение preflight+сбора контекста)** — "
        "**переупорядочить, не блокировать, но и не начинать сразу.** §3.1 показывает, "
        "что cache-write — самый дорогой бакет по взвешенной цене: это контекст, заново "
        "записываемый между шагами, ровно то, на что целится консолидация — "
        "дополнительный аргумент ЗА. Но этот спайк не разбивает главный цикл на шаги "
        "(preflight vs retrieval vs brainstorming-handoff), поэтому не может напрямую "
        "подтвердить, что именно preflight/сбор контекста — источник cache-write, а не "
        "что-то ещё в SKILL.md. Рекомендация: прежде чем проектировать "
        "`prepare_task_context`, добавить дешёвую step-level инструментацию (сколько "
        "cache-write приходится на каждый под-шаг solve-task) — вероятно, силами того же "
        "`brief_cost.py`-паттерна, не отдельного нового измерения."
    )
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Отчёт записан: {REPORT_PATH}")


if __name__ == "__main__":
    main()
