"""Оркестрация подготовки ревью PR.

Выделен из CLI — сервис инкапсулирует:
- получение PR и diff,
- синхронизацию индекса (overlay),
- кап ``max_files``.

Запуск analyze-этапа выполняется снаружи (Claude Code-скилл через MCP).
CLI остаётся тонкой обёрткой: парсит аргументы и вызывает
``ReviewService(...).prepare(...)``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from reviewer.app import Components
from reviewer.config.settings import Settings
from reviewer.vcs.base import ChangedFile, PullRequest, VCSProvider
from reviewer.vcs.github import GitHubProvider
from reviewer.policy.policy import ReviewPolicy
from reviewer.index.chunker import chunk_python
from reviewer.index.freshness import build_overlay, update_base
from reviewer.web.history import ReviewHistory
from reviewer.agent.state import ReviewUnit
from reviewer.services.task_keys import extract_task_keys

log = logging.getLogger(__name__)


def _hunk_count(patch: str | None) -> int:
    """Число hunks в unified diff; 0 если patch отсутствует."""
    if not patch:
        return 0
    return sum(1 for line in patch.splitlines() if line.startswith("@@"))


def _file_importance_key(file: ChangedFile) -> tuple[int, int]:
    """Ключ сортировки: больше hunks → важнее; при равенстве — длиннее patch."""
    return (-_hunk_count(file.patch), -(len(file.patch) if file.patch else 0))


def _select_changed_files(
    files: list[ChangedFile], max_files: int,
) -> list[ChangedFile]:
    """Оставить только .py-файлы (не удалённые), отсортированные по важности."""
    py_files = [f for f in files if f.path.endswith(".py") and f.status != "removed"]
    py_files.sort(key=_file_importance_key)
    return py_files[:max_files]


@dataclass
class PreparedReview:
    """Подготовленный контекст ревью PR: всё, что нужно analyze-этапу и публикации."""

    repo: str                              # канонический идентификатор "owner/name"
    prq: PullRequest
    units: list[ReviewUnit]
    policy: ReviewPolicy
    patches: dict[str, str | None]       # path -> unified diff
    sources: dict[str, str]              # path -> head-версия файла
    changed_paths: list[str]
    changed_node_ids: list[str]
    skipped_paths: list[str]
    overlay_ref: str                     # "pr:<n>"
    vcs: VCSProvider
    changed_status: dict[str, str]       # path -> статус файла (modified/added/removed)
    task_board: dict | None = None       # конфиг доски из policy (прокидывается в payload)
    task_keys: dict | None = None        # {"primary": str|None, "others": [...]}; None только когда task_board выкл.


class ReviewService:
    """Сервис подготовки ревью PR."""

    def __init__(
        self,
        settings: Settings,
        components: Components,
        history: ReviewHistory | None = None,
    ) -> None:
        self.settings = settings
        self.components = components
        self._history = history
        self._history_owned = history is None

    def _create_vcs_provider(self, owner: str, repo: str) -> GitHubProvider:
        """Создать GitHub-провайдер с retry."""
        return GitHubProvider(
            owner,
            repo,
            token=self.settings.github_token,
            retry_attempts=self.settings.github_retry_attempts,
            retry_backoff_base=self.settings.github_retry_backoff_base,
        )

    def _ensure_history(self) -> ReviewHistory | None:
        """Вернуть хранилище истории (создать при необходимости)."""
        if self._history is not None:
            return self._history
        if self.settings.review_history:
            self._history = ReviewHistory(
                self.settings.pg_dsn,
                min_size=self.settings.pg_pool_min_size,
                max_size=self.settings.pg_pool_max_size,
            )
            return self._history
        return None

    def prepare(
        self,
        owner: str,
        name: str,
        pr_number: int,
        vcs_provider: VCSProvider | None = None,
    ) -> PreparedReview:
        """Подготовка ревью: PR → синк base → отбор файлов → overlay → policy → юниты.

        Переиспользуется MCP-сервером: возвращает всё, что нужно
        для analyze-этапа и последующей публикации, без запуска LLM.

        Args:
            vcs_provider: опциональный кастомный VCS-провайдер (например,
                для прогона на локальных снапшотах в eval).
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(f"{owner}/{name}")

        # Self-healing: удаляем возможный «висящий» overlay прошлого прогона,
        # чтобы build_overlay строил чистый эфемерный ref.
        self.components.store.delete_ref(repo, f"pr:{pr_number}")

        vcs = vcs_provider or self._create_vcs_provider(owner, name)

        try:
            prq = vcs.get_pull_request(pr_number)

            files = vcs.get_changed_files(pr_number)

            # Свежесть base-индекса: подтягиваем чанки файлов, изменённых после
            # последней индексации (граф кода обновляется только на reviewer index).
            indexed = self.components.store.get_index_meta(repo, "base")
            # base-sync только для реального GitHub-ревью: при внешнем vcs_provider
            # (eval-снапшоты) синхронизация и set_index_meta затёрли бы прод-индекс
            # данными снапшота (у снапшота base_sha="base", не настоящий SHA ветки).
            if vcs_provider is None and indexed and indexed != prq.base_sha:
                try:
                    diff_files = vcs.compare_files(indexed, prq.base_sha)
                    update_base(
                        self.components.store,
                        self.components.embedder,
                        repo,
                        prq.base_ref,
                        [f.path for f in diff_files if f.status != "removed"],
                        read=lambda p: vcs.get_file_at_ref(p, prq.base_sha),
                        removed_files=[f.path for f in diff_files if f.status == "removed"],
                    )
                    self.components.store.set_index_meta(repo, "base", prq.base_sha)
                    # F2: инкрементальный repo-aware патч графа (fail-soft).
                    if self.components.graph is not None:
                        try:
                            from reviewer.services.graph_sync import patch_graph_incremental
                            changed_py: dict[str, str] = {}
                            for f in diff_files:
                                if f.status == "removed" or not f.path.endswith(".py"):
                                    continue
                                src = vcs.get_file_at_ref(f.path, prq.base_sha)
                                if src:
                                    changed_py[f.path] = src
                            removed_py = [
                                f.path for f in diff_files
                                if f.status == "removed" and f.path.endswith(".py")
                            ]
                            patch_graph_incremental(
                                self.components.graph, repo,
                                changed_sources=changed_py, removed_paths=removed_py)
                            log.info("Граф досинхронизирован инкрементально: "
                                     "%d изм., %d уд.", len(changed_py), len(removed_py))
                        except Exception:
                            log.warning("Инкрементальный патч графа не удался "
                                        "(дрейф графа сохранится до reviewer index)",
                                        exc_info=True)
                    log.info(
                        "Base-индекс синхронизирован: %d файлов (%s..%s)",
                        len(diff_files), indexed[:7], prq.base_sha[:7],
                    )
                except Exception as e:
                    log.warning("Не удалось синхронизировать base-индекс: %s", e)
            elif not indexed:
                log.warning(
                    "SHA base-индекса неизвестен (выполните reviewer index) "
                    "— индекс может быть устаревшим.",
                )

            selected_files = _select_changed_files(
                files, self.settings.review_max_files,
            )
            selected_paths = [f.path for f in selected_files]
            changed = selected_paths

            # Загружаем head-версии выбранных файлов один раз и переиспользуем
            # для overlay и для построения review-юнитов.
            head_sources: dict[str, str] = {}
            for f in selected_files:
                src = vcs.get_file_at_ref(f.path, prq.head_sha)
                if src:
                    head_sources[f.path] = src

            build_overlay(
                self.components.store,
                self.components.embedder,
                repo,
                pr_number,
                changed,
                head_sources=head_sources,
            )

            units: list[ReviewUnit] = []
            for f in selected_files:
                src = head_sources.get(f.path)
                if not src:
                    continue
                node_ids = [ch.node_id for ch in chunk_python(f.path, src.encode())]
                units.append(
                    ReviewUnit(f.path, node_ids, f.patch or "", new_source=src)
                )

            # Файлы вне лимита попадают в сводку как пропущенные
            all_py_paths = [
                f.path for f in files
                if f.path.endswith(".py") and f.status != "removed"
            ]
            skipped_paths = [
                p for p in all_py_paths if p not in set(selected_paths)
            ]

            # sources нужны для проверки наличия символов
            sources = {u.path: u.new_source for u in units}

            # changed_node_ids — объединение node_id всех юнитов (для graph-expansion)
            changed_node_ids = [nid for u in units for nid in u.node_ids]

            policy = ReviewPolicy.load(
                self.settings,
                vcs.get_file_at_ref(".review.yml", prq.base_ref),
            )

            task_board = policy.task_board
            task_keys = (
                extract_task_keys(
                    task_board.get("key_pattern"),
                    prq.title,
                    prq.body,
                    prq.head_ref,
                )
                if task_board
                else None
            )

            changed_status = {f.path: f.status for f in files}

            return PreparedReview(
                repo=repo,
                prq=prq,
                units=units,
                policy=policy,
                patches={f.path: f.patch for f in files},
                sources=sources,
                changed_paths=changed,
                changed_node_ids=changed_node_ids,
                skipped_paths=skipped_paths,
                overlay_ref=f"pr:{pr_number}",
                vcs=vcs,
                changed_status=changed_status,
                task_board=task_board,
                task_keys=task_keys,
            )
        except Exception:
            # При сбое подготовки чистим возможный недостроенный overlay pr:N —
            # у MCP-сервера не будет finally-страховки; повторное удаление идемпотентно.
            try:
                self.components.store.delete_ref(repo, f"pr:{pr_number}")
            except Exception:
                log.warning(
                    "Не удалось очистить overlay pr:%s после сбоя prepare",
                    pr_number, exc_info=True,
                )
            # Закрываем провайдер, созданный самим prepare; переданный снаружи
            # закрывает вызывающий. Без этого httpx-клиент GitHubProvider утёк бы
            # (критично для долгоживущего MCP-сервера).
            if vcs_provider is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning(
                        "Не удалось закрыть VCS-провайдер после сбоя prepare",
                        exc_info=True,
                    )
            raise
