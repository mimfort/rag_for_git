# PRI-114 — детект устарелости установленных скиллов + защита Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать молчаливую устарелость установленных скиллов (баг PRI-114: kimi гонит цикл `index_task` по старому скиллу) — стампом установки, детектом рассинхрона в `reviewer check`, защитой в самом скилле и синхронизацией доки.

**Architecture:** Логика стампа/хэшей/детекта живёт в `reviewer/install.py` (когезия с уже существующим кодом установки скиллов). `install_skills` и CLI `install-skills` пишут стамп `.reviewer-skills.json` в каталог скиллов через общий хелпер `stamp_skills_dir`. `reviewer check` зовёт `staleness_warnings()` (fail-soft) и печатает предупреждение. Скилл `sync-tasks` получает явный запрет цикла. `.kimi-code/INSTALL.md` приводится к коду.

**Tech Stack:** Python 3.11+, click (CLI), httpx (сеть), pytest (unit на фейках, сеть мокается), hashlib, importlib.metadata.

## Global Constraints

- Язык кода — русский: комментарии, докстринги, сообщения CLI.
- ruff: line-length 100, target py311 (`.venv/bin/ruff check .` без новых замечаний).
- pytest по умолчанию исключает `integration`; unit-тесты на фейках, сеть мокается (`monkeypatch`), реальные API не дёргаются.
- Стамп-файл: `.reviewer-skills.json` в каталоге скиллов клиента; имя в константе `STAMP_NAME`.
- Детект устарелости — **fail-soft**: офлайн не валит `reviewer check` и НЕ влияет на exit-code (мягкое предупреждение).
- Скиллы НЕ пакуются в wheel — тянутся тарболом `main` (`SKILLS_TARBALL`), всегда свежие.
- Версия пакета — только через `importlib.metadata.version("rag-reviewer")`; отдельного `reviewer.__version__` нет.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Ветка работы: `fix/pri-114-skill-staleness` (уже создана; спек закоммичен).

---

### Task 1: Стамп и хэши скиллов (чистые функции в install.py)

**Files:**
- Modify: `reviewer/install.py` (добавить после блока «скилы», ~после строки 408)
- Test: `tests/install/test_skills_stamp.py` (создать)

**Interfaces:**
- Produces:
  - `STAMP_NAME: str = ".reviewer-skills.json"`
  - `current_pkg_version() -> str` — версия пакета или `"unknown"`.
  - `_skill_file_hashes(skills_dir: Path) -> dict[str, str]` — `{имя_скила: "sha256:<hex>"}`; стамп-файл и не-каталоги пропускаются.
  - `write_skills_stamp(skills_dir: Path, *, source_url: str, source_etag: str | None, pkg_version: str, hashes: dict[str, str]) -> Path`
  - `read_skills_stamp(skills_dir: Path) -> dict | None` — `None`, если файла нет или он битый.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/install/test_skills_stamp.py`:

```python
from pathlib import Path

from reviewer import install as inst


def _mk_skill(root: Path, name: str, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / name / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_skill_file_hashes_deterministic_and_per_skill(tmp_path):
    _mk_skill(tmp_path, "sync-tasks", {"SKILL.md": b"# a", "references/y.md": b"# b"})
    _mk_skill(tmp_path, "solve-task", {"SKILL.md": b"# c"})
    h1 = inst._skill_file_hashes(tmp_path)
    h2 = inst._skill_file_hashes(tmp_path)
    assert h1 == h2                                  # детерминизм
    assert set(h1) == {"sync-tasks", "solve-task"}   # по скилу
    assert all(v.startswith("sha256:") for v in h1.values())


def test_skill_file_hashes_changes_on_edit(tmp_path):
    _mk_skill(tmp_path, "sync-tasks", {"SKILL.md": b"# a"})
    before = inst._skill_file_hashes(tmp_path)["sync-tasks"]
    (tmp_path / "sync-tasks" / "SKILL.md").write_bytes(b"# changed")
    after = inst._skill_file_hashes(tmp_path)["sync-tasks"]
    assert before != after


def test_stamp_roundtrip_and_ignores_stamp_file(tmp_path):
    _mk_skill(tmp_path, "sync-tasks", {"SKILL.md": b"# a"})
    hashes = inst._skill_file_hashes(tmp_path)
    inst.write_skills_stamp(tmp_path, source_url="u", source_etag='"e1"',
                            pkg_version="0.1.8", hashes=hashes)
    stamp = inst.read_skills_stamp(tmp_path)
    assert stamp["source_etag"] == '"e1"'
    assert stamp["pkg_version"] == "0.1.8"
    assert stamp["skills"] == hashes
    # стамп-файл не должен попадать в хэши скилов
    assert inst.STAMP_NAME not in inst._skill_file_hashes(tmp_path)


def test_read_stamp_missing_returns_none(tmp_path):
    assert inst.read_skills_stamp(tmp_path) is None


def test_current_pkg_version_is_str():
    assert isinstance(inst.current_pkg_version(), str)
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_skills_stamp.py -q`
Expected: FAIL (`AttributeError: module 'reviewer.install' has no attribute '_skill_file_hashes'`).

- [ ] **Step 3: Реализовать функции**

В `reviewer/install.py` добавить (в конец файла, после `install_skills`):

```python
# --------------------------------------------------------------------------- #
# стамп установки скилов (для детекта устарелости)
# --------------------------------------------------------------------------- #
STAMP_NAME = ".reviewer-skills.json"


def current_pkg_version() -> str:
    """Версия установленного пакета rag-reviewer (или 'unknown')."""
    import importlib.metadata as md

    try:
        return md.version(PACKAGE)
    except md.PackageNotFoundError:
        return "unknown"


def _skill_file_hashes(skills_dir: Path) -> dict[str, str]:
    """sha256 каждого скила (по всем его файлам). Ключ — имя подкаталога-скила.

    Детерминизм: файлы скила сортируются по относительному пути, в дайджест идёт
    rel-path + NUL + содержимое. Не-каталоги верхнего уровня (включая сам
    стамп-файл) пропускаются.
    """
    import hashlib

    result: dict[str, str] = {}
    if not skills_dir.is_dir():
        return result
    for sub in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        h = hashlib.sha256()
        for f in sorted(sub.rglob("*")):
            if f.is_file():
                h.update(f.relative_to(sub).as_posix().encode("utf-8"))
                h.update(b"\0")
                h.update(f.read_bytes())
        result[sub.name] = "sha256:" + h.hexdigest()
    return result


def write_skills_stamp(
    skills_dir: Path, *, source_url: str, source_etag: str | None,
    pkg_version: str, hashes: dict[str, str],
) -> Path:
    """Записать стамп установки скилов в <skills_dir>/.reviewer-skills.json."""
    from datetime import datetime, timezone

    stamp = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source_url": source_url,
        "source_etag": source_etag,
        "pkg_version": pkg_version,
        "skills": hashes,
    }
    path = skills_dir / STAMP_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_skills_stamp(skills_dir: Path) -> dict | None:
    """Прочитать стамп; None, если файла нет или он битый."""
    path = skills_dir / STAMP_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/install/test_skills_stamp.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/install.py tests/install/test_skills_stamp.py
git commit -m "feat(install): стамп установки скилов и хэши для детекта устарелости"
```

---

### Task 2: ETag и запись стампа при установке скиллов

**Files:**
- Modify: `reviewer/install.py` (функции `fetch_skills_bytes` ~349, `install_skills` ~391; добавить `fetch_skills_archive`, `fetch_skills_etag`, `stamp_skills_dir`)
- Test: `tests/install/test_skills_stamp.py` (дополнить)

**Interfaces:**
- Consumes (из Task 1): `write_skills_stamp`, `_skill_file_hashes`, `current_pkg_version`, `STAMP_NAME`, `SKILLS_TARBALL`.
- Produces:
  - `fetch_skills_archive(url: str = SKILLS_TARBALL) -> tuple[bytes, str | None]` — (тарбол, ETag|None).
  - `fetch_skills_bytes(url: str = SKILLS_TARBALL) -> bytes` — обёртка (обратная совместимость).
  - `fetch_skills_etag(url: str = SKILLS_TARBALL, *, timeout: float = 5.0) -> str | None` — HEAD, fail-soft.
  - `stamp_skills_dir(skills_dir: Path, *, source_etag: str | None) -> Path`.
  - `install_skills(client, *, system=None, tar_bytes=None, source_etag=None) -> tuple[Path, list[str]]` — теперь пишет стамп.

- [ ] **Step 1: Написать падающий тест**

Дополнить `tests/install/test_skills_stamp.py`:

```python
import io
import tarfile


def _make_tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_skills_writes_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    tar = _make_tarball({
        "r/plugin/skills/sync-tasks/SKILL.md": b"# sync",
        "r/plugin/skills/solve-task/SKILL.md": b"# solve",
    })
    dest, names = inst.install_skills(
        inst.CLIENTS["kimi"], system="Linux", tar_bytes=tar, source_etag='"abc"')
    stamp = inst.read_skills_stamp(dest)
    assert stamp is not None
    assert stamp["source_etag"] == '"abc"'
    assert set(stamp["skills"]) == set(names)
    assert stamp["source_url"] == inst.SKILLS_TARBALL


def test_fetch_skills_bytes_backward_compat(monkeypatch):
    monkeypatch.setattr(inst, "fetch_skills_archive", lambda url=inst.SKILLS_TARBALL: (b"X", '"e"'))
    assert inst.fetch_skills_bytes() == b"X"


def test_fetch_skills_etag_failsoft(monkeypatch):
    import httpx
    def boom(*a, **k):
        raise RuntimeError("no network")
    # fetch_skills_etag делает `import httpx` внутри → патчим реальный httpx.head
    monkeypatch.setattr(httpx, "head", boom)
    assert inst.fetch_skills_etag(timeout=0.1) is None
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_skills_stamp.py -k "stamp or backward or etag" -q`
Expected: FAIL (`install_skills() got an unexpected keyword argument 'source_etag'` / `no attribute 'fetch_skills_archive'`).

- [ ] **Step 3: Реализовать**

В `reviewer/install.py` заменить `fetch_skills_bytes` (строки ~349-355) на:

```python
def fetch_skills_archive(url: str = SKILLS_TARBALL) -> tuple[bytes, str | None]:
    """Скачать тарбол репозитория + вернуть его ETag (для стампа). httpx уже в зависимостях."""
    import httpx

    resp = httpx.get(url, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    return resp.content, resp.headers.get("etag")


def fetch_skills_bytes(url: str = SKILLS_TARBALL) -> bytes:
    """Только тарбол (обратная совместимость со старыми вызовами)."""
    return fetch_skills_archive(url)[0]


def fetch_skills_etag(url: str = SKILLS_TARBALL, *, timeout: float = 5.0) -> str | None:
    """ETag тарбола через HEAD. Fail-soft: при любой ошибке/офлайне — None."""
    import httpx

    try:
        resp = httpx.head(url, follow_redirects=True, timeout=timeout)
        resp.raise_for_status()
        return resp.headers.get("etag")
    except Exception:  # noqa: BLE001 — детект устарелости не должен падать
        return None
```

Добавить хелпер стампа (после `read_skills_stamp` из Task 1):

```python
def stamp_skills_dir(skills_dir: Path, *, source_etag: str | None) -> Path:
    """Записать стамп для уже распакованного каталога скилов."""
    return write_skills_stamp(
        skills_dir,
        source_url=SKILLS_TARBALL,
        source_etag=source_etag,
        pkg_version=current_pkg_version(),
        hashes=_skill_file_hashes(skills_dir),
    )
```

Заменить тело `install_skills` (строки ~391-407) на:

```python
def install_skills(
    client: Client,
    *,
    system: str | None = None,
    tar_bytes: bytes | None = None,
    source_etag: str | None = None,
) -> tuple[Path, list[str]]:
    """Установить скилы в каталог клиента + записать стамп. Возвращает (каталог, имена).

    tar_bytes можно передать заранее скачанным (чтобы не качать на каждый клиент);
    в этом случае передайте и source_etag (иначе он будет None в стампе).
    """
    system = system or platform.system()
    if client.skills_fn is None:
        raise ValueError(f"{client.label}: файловые скилы не поддерживаются")
    dest = client.skills_fn(system)
    if tar_bytes is None:
        data, fetched_etag = fetch_skills_archive()
        if source_etag is None:
            source_etag = fetched_etag
    else:
        data = tar_bytes
    names = extract_skills(data, dest)
    stamp_skills_dir(dest, source_etag=source_etag)
    return dest, names
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/install/test_skills_stamp.py tests/install/test_install.py -q`
Expected: PASS (новые + старые install-тесты зелёные — обратная совместимость).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/install.py tests/install/test_skills_stamp.py
git commit -m "feat(install): ETag тарбола и запись стампа при установке скилов"
```

---

### Task 3: Детект устарелости (skills_staleness + staleness_warnings)

**Files:**
- Modify: `reviewer/install.py` (добавить `StalenessReport`, `skills_staleness`, `staleness_warnings`)
- Test: `tests/install/test_skills_staleness.py` (создать)

**Interfaces:**
- Consumes (Task 1-2): `read_skills_stamp`, `_skill_file_hashes`, `fetch_skills_etag`, `current_pkg_version`, `CLIENTS`, `Client`, `STAMP_NAME`.
- Produces:
  - `StalenessReport` (dataclass): `client_key: str`, `client_label: str`, `skills_dir: Path`, `stale: bool`, `reason: str`, `command: str`.
  - `skills_staleness(client: Client, *, system: str | None = None, timeout: float = 5.0) -> StalenessReport | None` — `None`, если у клиента нет файловых скилов или каталог не существует.
  - `staleness_warnings(system: str | None = None, *, timeout: float = 5.0) -> list[str]` — строки-предупреждения по всем клиентам (пусто, если всё свежо); внутри fail-soft.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/install/test_skills_staleness.py`:

```python
from pathlib import Path

from reviewer import install as inst


def _setup_kimi(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    d = inst.CLIENTS["kimi"].skills_fn("Linux")
    (d / "sync-tasks").mkdir(parents=True)
    (d / "sync-tasks" / "SKILL.md").write_bytes(b"# sync")
    return d


def test_none_when_no_skills_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    assert inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux") is None


def test_stale_when_no_stamp(monkeypatch, tmp_path):
    _setup_kimi(monkeypatch, tmp_path)
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "стамп" in rep.reason
    assert rep.command == "reviewer install-skills kimi"


def test_stale_on_local_drift(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"e1"')
    (d / "sync-tasks" / "SKILL.md").write_bytes(b"# CHANGED locally")  # дрейф
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "разош" in rep.reason


def test_stale_on_etag_mismatch(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"old"')
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: '"new"')
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "upstream" in rep.reason


def test_fresh_when_etag_matches(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"same"')
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: '"same"')
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale is False


def test_offline_fallback_pkg_version(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"e1"')
    # стамп пишет current_pkg_version(); эмулируем «сервер уехал вперёд» + офлайн
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: None)
    monkeypatch.setattr(inst, "current_pkg_version", lambda: "99.0.0")
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "сервер обновился" in rep.reason


def test_staleness_warnings_collects(monkeypatch, tmp_path):
    _setup_kimi(monkeypatch, tmp_path)  # kimi без стампа → stale
    lines = inst.staleness_warnings(system="Linux")
    assert any("kimi" in ln.lower() or "Kimi" in ln for ln in lines)


def test_staleness_warnings_failsoft(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(inst, "skills_staleness", boom)
    assert inst.staleness_warnings(system="Linux") == []  # не падает
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_skills_staleness.py -q`
Expected: FAIL (`module 'reviewer.install' has no attribute 'skills_staleness'`).

- [ ] **Step 3: Реализовать**

В `reviewer/install.py` добавить (после `stamp_skills_dir`):

```python
@dataclass
class StalenessReport:
    client_key: str
    client_label: str
    skills_dir: Path
    stale: bool
    reason: str            # человекочитаемая причина (пусто, если свежо)
    command: str           # рекомендуемая команда исправления


def skills_staleness(
    client: Client, *, system: str | None = None, timeout: float = 5.0
) -> StalenessReport | None:
    """Оценить, устарели ли установленные скилы клиента.

    None — у клиента нет файловых скилов или каталог не существует (нечего
    проверять). Иначе StalenessReport. Сетевой ETag — best-effort: при офлайне
    используется фолбэк по версии пакета.
    """
    system = system or platform.system()
    if client.skills_fn is None:
        return None
    skills_dir = client.skills_fn(system)
    if not skills_dir.exists():
        return None
    cmd = f"reviewer install-skills {client.key}"

    def report(stale: bool, reason: str) -> StalenessReport:
        return StalenessReport(client.key, client.label, skills_dir, stale, reason, cmd)

    stamp = read_skills_stamp(skills_dir)
    if stamp is None:
        return report(True, "нет стампа установки (старый установщик)")
    if _skill_file_hashes(skills_dir) != (stamp.get("skills") or {}):
        return report(True, "содержимое скилов разошлось со стампом (дрейф/частичная установка)")
    etag = fetch_skills_etag(timeout=timeout)
    if etag is not None:
        if stamp.get("source_etag") and etag != stamp["source_etag"]:
            return report(True, "upstream main обновился с момента установки")
        return report(False, "")
    # офлайн-фолбэк: сравнить версию пакета на момент установки и текущую
    cur = current_pkg_version()
    if cur != "unknown" and stamp.get("pkg_version") and cur != stamp["pkg_version"]:
        return report(
            True,
            f"сервер обновился ({stamp['pkg_version']}→{cur}), upstream недоступен офлайн")
    return report(False, "")


def staleness_warnings(system: str | None = None, *, timeout: float = 5.0) -> list[str]:
    """Строки-предупреждения по всем клиентам с файловыми скилами (fail-soft)."""
    system = system or platform.system()
    lines: list[str] = []
    for client in CLIENTS.values():
        if client.skills_fn is None or client.scope == "project":
            continue
        try:
            rep = skills_staleness(client, system=system, timeout=timeout)
        except Exception:  # noqa: BLE001 — детект не должен ломать вызывающего
            continue
        if rep and rep.stale:
            lines.append(f"⚠ скилы {rep.client_label} устарели ({rep.reason}) → {rep.command}")
    return lines
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/install/test_skills_staleness.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/install.py tests/install/test_skills_staleness.py
git commit -m "feat(install): детект устарелости установленных скилов (ETag/hash/версия)"
```

---

### Task 4: Проводка в CLI — стамп в install/install-skills, предупреждение в check

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (`install._ensure_skills` ~278-296; `install-skills` ~485-490; `check` ~99-121)
- Test: `tests/install/test_install_skills_cli.py` (создать)

**Interfaces:**
- Consumes (Task 2-3): `inst.fetch_skills_archive`, `inst.stamp_skills_dir`, `inst.install_skills(..., source_etag=...)`, `inst.staleness_warnings`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/install/test_install_skills_cli.py`:

```python
from pathlib import Path

from click.testing import CliRunner

from reviewer import install as inst
from reviewer.entrypoints.cli import cli


def _tar(members):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_skills_cli_writes_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    tar = _tar({"r/plugin/skills/sync-tasks/SKILL.md": b"# s"})
    monkeypatch.setattr(inst, "fetch_skills_archive", lambda url=inst.SKILLS_TARBALL: (tar, '"etagX"'))
    res = CliRunner().invoke(cli, ["install-skills", "kimi"])
    assert res.exit_code == 0, res.output
    dest = inst.CLIENTS["kimi"].skills_fn("Linux")
    stamp = inst.read_skills_stamp(dest)
    assert stamp is not None and stamp["source_etag"] == '"etagX"'
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_install_skills_cli.py -q`
Expected: FAIL (стамп не пишется — `install-skills` зовёт `extract_skills` напрямую, `read_skills_stamp` → None).

- [ ] **Step 3: Реализовать проводку**

В `reviewer/entrypoints/cli.py`, команда `install-skills` — заменить блок скачивания/распаковки (строки ~485-490) на:

```python
    click.echo("Скачиваю скилы с GitHub…")
    tar, etag = inst.fetch_skills_archive()
    for c in targets:
        dest = Path(path_opt).expanduser() if path_opt else c.skills_fn(_platform.system())
        names = inst.extract_skills(tar, dest)
        inst.stamp_skills_dir(dest, source_etag=etag)
        click.echo(f"✓ {c.label}: {len(names)} скилов → {dest}")
        if c.note:
            click.echo(f"  прим.: {c.note}")
```

В команде `install`, функция `_ensure_skills` (строки ~278-296) — кэшировать `(bytes, etag)` и передавать `source_etag`:

```python
    tar_cache: list[tuple[bytes, str | None]] = []  # тарбол+ETag качаем один раз

    def _ensure_skills(c) -> None:
        if no_skills or c.skills_fn is None:
            return
        if not tar_cache:
            click.echo("  скилы: скачиваю с GitHub…")
            try:
                tar_cache.append(inst.fetch_skills_archive())
            except Exception as exc:  # noqa: BLE001 — fail-soft, MCP уже прописан
                click.echo(f"  скилы: пропуск (не скачать тарбол: {exc})")
                tar_cache.append((b"", None))
                return
        data, etag = tar_cache[0]
        if not data:
            return
        try:
            dest, names = inst.install_skills(c, tar_bytes=data, source_etag=etag)
            click.echo(f"  скилы: {len(names)} шт. → {dest}")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  скилы: пропуск ({exc})")
```

(Строка `tar_cache: list[bytes] = []` ~276 заменяется объявлением выше; удалить старое.)

В команде `check`, перед финальным блоком `if failed:` (строка ~119) добавить секцию:

```python
    # 6. Свежесть установленных скилов (информационно, не влияет на exit-code)
    try:
        from reviewer import install as _inst
        warns = _inst.staleness_warnings()
        for line in warns:
            click.echo(line)
        if not warns:
            click.echo("✓ Скилы клиентов: актуальны (или не установлены)")
    except Exception:  # noqa: BLE001 — детект устарелости не должен валить check
        pass
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/install/ -q`
Expected: PASS (новый CLI-тест + все прежние install-тесты).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/install/test_install_skills_cli.py
git commit -m "feat(cli): стамп в install/install-skills и предупреждение об устаревших скилах в check"
```

---

### Task 5: Защита в скилле sync-tasks (запрет цикла index_task)

**Files:**
- Modify: `plugin/skills/sync-tasks/SKILL.md` (шаг 3 «Normalize + index», ~строки 39-45)
- Modify: `plugin/skills/sync-tasks/references/sync-tasks-yougile.md` (раздел «## 3. Index», ~строки 35-38)
- Test: `tests/skills/test_sync_tasks_guardrail.py` (создать)

**Interfaces:** нет (правка контента + guard-тест).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/skills/test_sync_tasks_guardrail.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "sync-tasks" / "SKILL.md"
REF = ROOT / "plugin" / "skills" / "sync-tasks" / "references" / "sync-tasks-yougile.md"


def test_skill_forbids_index_task_loop():
    text = SKILL.read_text(encoding="utf-8")
    assert "НИКОГДА" in text
    assert "index_task` в цикле" in text
    assert "index_tasks_batch" in text


def test_reference_forbids_index_task_loop():
    text = REF.read_text(encoding="utf-8")
    assert "index_task` в цикле" in text
    assert "index_tasks_batch" in text
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_sync_tasks_guardrail.py -q`
Expected: FAIL (`assert "НИКОГДА" in text` — фразы ещё нет).

- [ ] **Step 3: Добавить guardrail в скилл**

В `plugin/skills/sync-tasks/SKILL.md`, в конце шага 3 (после строки про `index_tasks_batch ... O(1) Voyage API calls`) добавить абзац:

```markdown
   > **НИКОГДА не вызывай `index_task` в цикле по задачам.** Собери все `TaskBrief` в один
   > список и сделай **один** `index_tasks_batch(...)`. Для очень больших досок — чанками по
   > ≤25 задач за вызов (всё равно O(число чанков) вызовов Voyage, а не O(N)). Одиночный
   > `index_task` существует только для сценария одной задачи (`solve-task`) — для синка он
   > запрещён: поштучный цикл упирается в Voyage 3 RPM и приводит к таймауту.
```

В `plugin/skills/sync-tasks/references/sync-tasks-yougile.md`, в раздел «## 3. Index» (после строки про `single call`) добавить:

```markdown
> **НИКОГДА не вызывай `index_task` в цикле.** Только один `index_tasks_batch(...)` на весь
> список (или чанки по ≤25). Поштучный `index_task` — лишь для `solve-task`; здесь запрещён.
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_sync_tasks_guardrail.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/sync-tasks/SKILL.md plugin/skills/sync-tasks/references/sync-tasks-yougile.md tests/skills/test_sync_tasks_guardrail.py
git commit -m "fix(skill): sync-tasks — явный запрет цикла index_task (defense-in-depth PRI-114)"
```

---

### Task 6: Синхронизировать .kimi-code/INSTALL.md с кодом

**Files:**
- Modify: `.kimi-code/INSTALL.md` (раздел «## Skills», ~строки 39-59)
- Test: `tests/skills/test_kimi_install_doc.py` (создать)

**Interfaces:** нет (правка доки + лёгкий guard).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/skills/test_kimi_install_doc.py`:

```python
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / ".kimi-code" / "INSTALL.md"


def test_install_doc_mentions_install_command_for_skills():
    text = DOC.read_text(encoding="utf-8")
    # быстрый путь ставит и скилы; обновление = повторный запуск
    assert "reviewer install kimi" in text
    assert "обновлен" in text.lower()  # есть явная пометка про обновление
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_kimi_install_doc.py -q`
Expected: FAIL (нет пометки про обновление / про установку скилов командой).

- [ ] **Step 3: Привести INSTALL.md к коду**

В `.kimi-code/INSTALL.md` в разделе «## Быстрая установка» дополнить, что команда ставит и скилы; раздел «## Skills (optional)» переписать так, чтобы основным путём был `reviewer install kimi` (ставит MCP + скилы), а ручной `curl|tar` остался помеченной офлайн-альтернативой. Добавить явный блок:

```markdown
## Обновление

Скилы — это снапшот, скачанный при установке; сами они не обновляются. После апгрейда
сервера освежите их повторным запуском:

\`\`\`bash
uvx --from rag-reviewer reviewer install kimi      # MCP + свежие скилы
# или только скилы:
uvx --from rag-reviewer reviewer install-skills kimi
\`\`\`

`reviewer check` предупредит, если установленные скилы устарели.
```

(В разделе «Step 2 — download the skills» добавить строку: «Это снапшот; для обновления
используйте `reviewer install kimi` — см. раздел «Обновление».»)

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/skills/test_kimi_install_doc.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add .kimi-code/INSTALL.md tests/skills/test_kimi_install_doc.py
git commit -m "docs(install): kimi INSTALL.md — install ставит скилы, раздел про обновление"
```

---

### Task 7: Освежить скиллы на машине + E2E-верификация (операционно/manual)

**Files:** нет правок кода — операционная проверка фикса на реальном kimi.

**Interfaces:** Consumes всё выше (рабочий `reviewer install-skills`, обновлённый скилл на GitHub-`main` после мержа — для локальной проверки достаточно скопировать из репо).

- [ ] **Step 1: Прогнать весь набор тестов и линт**

Run:
```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```
Expected: unit зелёные; ruff без новых замечаний.

- [ ] **Step 2: Освежить установленные скиллы из текущего репо**

> До мержа в `main` тарбол ещё старый, поэтому копируем свежие скиллы из репо напрямую
> (после мержа сработает обычный `reviewer install-skills kimi`).

Run:
```bash
cp -R plugin/skills/sync-tasks ~/.kimi-code/skills/
cp -R plugin/skills/solve-task ~/.kimi-code/skills/
cp -R plugin/skills/review-pr ~/.kimi-code/skills/
grep -n "index_tasks_batch" ~/.kimi-code/skills/sync-tasks/SKILL.md
```
Expected: SKILL.md теперь содержит `index_tasks_batch` и блок «НИКОГДА не вызывай index_task в цикле».

- [ ] **Step 3: Чистый прогон kimi вне репозитория**

Run (как в воспроизведении):
```bash
SCRATCH=/tmp/kimi_verify; rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
printf 'task_board:\n  type: yougile\n  mcp: yougile\n  key_pattern: %s\n  url_template: %s\n' \
  "'[A-Z]+-\\d+'" "'https://ru.yougile.com/team/686c049c8af8/#{code}'" > "$SCRATCH/.review.yml"
cd "$SCRATCH"
kimi -p "Просиндексируй задачи с доски в reviewer: используй скилл reviewer_sync-tasks. Доска (yougile) в .review.yml. Выполни синк до конца." --output-format stream-json > /tmp/kimi_verify.jsonl 2>&1
```

- [ ] **Step 4: Проверить трейс — один batch, ноль цикла**

Run:
```bash
echo "index_task     : $(grep -ao '"name":"mcp__reviewer__index_task"' /tmp/kimi_verify.jsonl | wc -l | tr -d ' ')"
echo "index_tasks_batch: $(grep -ao '"name":"mcp__reviewer__index_tasks_batch"' /tmp/kimi_verify.jsonl | wc -l | tr -d ' ')"
```
Expected: `index_task : 0` (или ≤ числа чанков, без поштучного цикла), `index_tasks_batch ≥ 1`.

- [ ] **Step 5: Проверить предупреждение об устарелости (негативный + позитивный)**

Run:
```bash
reviewer check 2>&1 | grep -iE "скил|устарел|актуальны" || true
```
Expected: после освежения — «✓ Скилы клиентов: актуальны» (или предупреждение, если стамп ещё не записан — тогда выполнить `reviewer install-skills kimi` и повторить).

- [ ] **Step 6: Финальный коммит-маркер (если остались незакоммиченные правки)**

```bash
git status --short
```
Expected: чисто (всё закоммичено в Tasks 1-6). Если есть — закоммитить с осмысленным сообщением.

---

## Self-Review

**Spec coverage:**
- Компонент 1 (освежить сейчас) → Task 7 (Step 2-5).
- Компонент 2 (стамп + детект в check) → Tasks 1, 2, 3, 4.
- Компонент 3 (guardrail в скилле) → Task 5.
- Компонент 4 (INSTALL.md) → Task 6.
- Критерии приёмки спека → Task 7 (E2E: один batch; check-предупреждение) + Task 1-6 (unit).
- Не-цели соблюдены: wheel не трогаем, `index_batch` не трогаем, сервер скилы не рефрешит.

**Placeholder scan:** код приведён полностью в каждом шаге; команд с ожидаемым выводом — есть; «add error handling»-формулировок нет.

**Type consistency:** `fetch_skills_archive -> (bytes, str|None)`; `install_skills(..., source_etag=None)`; `stamp_skills_dir(dest, *, source_etag)`; `skills_staleness(...) -> StalenessReport | None`; `staleness_warnings(...) -> list[str]`; `StalenessReport.command == "reviewer install-skills <key>"` — используются согласованно во всех тасках и тестах. `STAMP_NAME`, `SKILLS_TARBALL`, `PACKAGE`, `CLIENTS`, `Client`, `_home` — существующие/вводимые имена совпадают.
