"""Сервис задач: index_task / search_tasks / get_task_context / link_review.

Оркестрирует TaskStore (эмбеддинги) + TaskGraph (граф) + эмбеддер. Fail-soft по
слоям: сбой одного слоя не валит остальное (деградация как фазы 1/2).
"""
from __future__ import annotations

import logging

from reviewer.tasks.graph import PRRef
from reviewer.tasks.pr_links import extract_pr_refs
from reviewer.tasks.store import TaskRow, build_task_text, task_content_hash

log = logging.getLogger(__name__)


def _normalize_links(links: object) -> list[dict]:
    """Filter and deduplicate one authoritative links snapshot."""
    result: list[dict] = []
    seen: set[tuple[object, object]] = set()
    for link in links or ():
        if not isinstance(link, dict) or not link.get("key"):
            continue
        identity = (link["key"], link.get("type") or "relates")
        try:
            if identity in seen:
                continue
            seen.add(identity)
        except TypeError:
            continue
        result.append(dict(link))
    return result


class TaskService:
    """Оркестрация индексации и обхода графа задач."""

    def __init__(self, store, graph, embedder, *, max_chars: int = 8000,
                 attachment_embed_chars: int = 8000) -> None:
        self._store = store
        self._graph = graph          # None, если Neo4j не подключён
        self._embedder = embedder
        self._max_chars = max_chars
        self._attachment_embed_chars = attachment_embed_chars

    def index_task(self, task: dict) -> dict:
        """Проиндексировать нормализованный TaskBrief: эмбеддинг (дедуп) + граф."""
        key = task.get("key") if isinstance(task, dict) else None
        if not key:
            return {"key": None, "embedded": False, "links_upserted": 0,
                    "links_stored": None, "prs_linked": 0,
                    "warnings": ["task has no key"], "retry_required": False}
        aliases = [a for a in (task.get("aliases") or []) if a and a != key]
        title = task.get("title") or ""
        description = task.get("description") or ""
        criteria = task.get("criteria") or []
        attachments = task.get("attachments") or []
        status = task.get("status")
        url = task.get("url")
        project = task.get("project") or ""
        links_supplied = "links" in task
        links = _normalize_links(task.get("links")) if links_supplied else None
        text = build_task_text(title, description, criteria, attachments,
                               embed_chars=self._attachment_embed_chars)
        chash = task_content_hash(text)
        warnings: list[str] = []
        retry_required = False

        embedded = False
        links_stored: bool | None = None
        try:
            prev = self._store.existing_hash(key)
            if prev == chash:
                self._store.update_meta(key, title, status, url, aliases, project)
            else:
                vec = self._embedder.embed_documents([text])[0]
                self._store.upsert_task(TaskRow(
                    key=key, aliases=aliases, title=title, description=description,
                    status=status, url=url, content_hash=chash, text=text,
                    embedding=vec, project=project, attachments=attachments,
                    links=links))
                embedded = True
                if links_supplied:
                    links_stored = True
        except Exception as e:
            retry_required = True
            log.warning("index_task: сбой store для %s", key, exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")

        if links_supplied and links_stored is not True:
            try:
                links_stored = self._store.update_links(key, links)
                if links_stored is False:
                    warnings.append(f"store links: task {key} not found")
            except Exception as e:
                log.warning("index_task: сбой store links для %s", key, exc_info=True)
                warnings.append(f"store links: {type(e).__name__}: {e}")
                links_stored = False

        links_upserted = 0
        if self._graph is None:
            warnings.append("graph unavailable: task not added to task graph")
        else:
            try:
                self._graph.upsert_task(key, aliases, title, status, url, project)
                if links_supplied:
                    links_upserted = self._graph.replace_links(key, links)
            except Exception as e:
                log.warning("index_task: сбой графа для %s", key, exc_info=True)
                warnings.append(f"graph: {type(e).__name__}: {e}")

        # Авто-линковка PR из description — только для изменившихся (embedded)
        # задач и при доступном графе (повторный синк без изменений ничего не делает).
        prs_linked = 0
        if embedded and self._graph is not None:
            refs = extract_pr_refs(description)
            for pr in refs:
                self.link_review(key, pr, [])  # touched=[] — код подтянется лениво
            prs_linked = len(refs)

        return {"key": key, "embedded": embedded,
                "links_upserted": links_upserted, "links_stored": links_stored,
                "prs_linked": prs_linked,
                "warnings": warnings, "retry_required": retry_required}

    def index_batch(self, tasks: list[dict]) -> list[dict]:
        """Батчевая индексация: один Voyage-вызов для всех изменившихся задач."""
        if not tasks:
            return []

        # Шаг 1: распарсить все задачи и вычислить хэши
        parsed: list[dict | None] = []
        results: list[dict | None] = [None] * len(tasks)

        for i, task in enumerate(tasks):
            key = task.get("key") if isinstance(task, dict) else None
            if not key:
                results[i] = {"key": None, "embedded": False, "links_upserted": 0,
                              "links_stored": None, "prs_linked": 0,
                              "warnings": ["task has no key"],
                              "retry_required": False}
                parsed.append(None)
                continue
            aliases = [a for a in (task.get("aliases") or []) if a and a != key]
            title = task.get("title") or ""
            description = task.get("description") or ""
            criteria = task.get("criteria") or []
            attachments = task.get("attachments") or []
            status = task.get("status")
            url = task.get("url")
            links_supplied = "links" in task
            links = _normalize_links(task.get("links")) if links_supplied else None
            text = build_task_text(title, description, criteria, attachments,
                                   embed_chars=self._attachment_embed_chars)
            chash = task_content_hash(text)
            parsed.append({"key": key, "aliases": aliases, "title": title,
                           "description": description, "status": status, "url": url,
                           "links": links, "links_supplied": links_supplied,
                           "text": text, "chash": chash,
                           "project": task.get("project") or "",
                           "attachments": attachments})

        # Шаг 2: разделить на to_embed / meta_only по content-hash
        to_embed: list[int] = []
        meta_only: list[int] = []

        for i, p in enumerate(parsed):
            if p is None:
                continue
            try:
                prev = self._store.existing_hash(p["key"])
            except Exception as e:
                log.warning("index_batch: existing_hash сбой для %s", p["key"], exc_info=True)
                results[i] = {"key": p["key"], "embedded": False, "links_upserted": 0,
                              "links_stored": None, "prs_linked": 0,
                              "warnings": [f"store: {type(e).__name__}: {e}"],
                              "retry_required": True}
                continue
            (meta_only if prev == p["chash"] else to_embed).append(i)

        # Шаг 3: один Voyage-вызов для изменившихся задач
        embed_err: str | None = None
        embeddings: dict[int, list[float]] = {}
        if to_embed:
            try:
                vecs = self._embedder.embed_documents([parsed[i]["text"] for i in to_embed])
                embeddings = {i: vecs[idx] for idx, i in enumerate(to_embed)}
            except Exception as e:
                log.warning("index_batch: сбой embed_documents", exc_info=True)
                embed_err = f"embedder: {type(e).__name__}: {e}"

        # Шаг 4: upsert изменившихся задач (или propagate embed_err)
        for i in to_embed:
            p = parsed[i]
            warnings: list[str] = []
            embedded = False
            retry_required = False
            if embed_err:
                warnings.append(embed_err)
                retry_required = True
            else:
                try:
                    self._store.upsert_task(TaskRow(
                        key=p["key"], aliases=p["aliases"], title=p["title"],
                        description=p["description"], status=p["status"], url=p["url"],
                        content_hash=p["chash"], text=p["text"], embedding=embeddings[i],
                        project=p["project"], attachments=p["attachments"],
                        links=p["links"]))
                    embedded = True
                except Exception as e:
                    retry_required = True
                    log.warning("index_batch: сбой store для %s", p["key"], exc_info=True)
                    warnings.append(f"store: {type(e).__name__}: {e}")
            results[i] = {"key": p["key"], "embedded": embedded,
                          "links_upserted": 0,
                          "links_stored": True if embedded and p["links_supplied"] else None,
                          "warnings": warnings, "retry_required": retry_required}

        # Шаг 5: update_meta для неизменившихся задач
        for i in meta_only:
            p = parsed[i]
            warnings: list[str] = []
            retry_required = False
            try:
                self._store.update_meta(p["key"], p["title"], p["status"],
                                        p["url"], p["aliases"], p["project"])
            except Exception as e:
                retry_required = True
                log.warning("index_batch: сбой update_meta для %s", p["key"], exc_info=True)
                warnings.append(f"store: {type(e).__name__}: {e}")
            results[i] = {"key": p["key"], "embedded": False,
                          "links_upserted": 0, "links_stored": None,
                          "warnings": warnings, "retry_required": retry_required}

        # Snapshot links обновляется независимо от ветки embed/meta-only.
        for i, p in enumerate(parsed):
            if (p is None or results[i] is None or not p["links_supplied"]
                    or results[i]["links_stored"] is True):
                continue
            try:
                results[i]["links_stored"] = self._store.update_links(p["key"], p["links"])
                if results[i]["links_stored"] is False:
                    results[i]["warnings"].append(
                        f"store links: task {p['key']} not found")
            except Exception as e:
                log.warning("index_batch: сбой store links для %s", p["key"], exc_info=True)
                results[i]["warnings"].append(f"store links: {type(e).__name__}: {e}")
                results[i]["links_stored"] = False

        # Шаг 6: граф для всех валидных задач (+ сбор PR-пар для батч-линковки)
        pr_pairs: list[tuple[str, PRRef]] = []
        for i, p in enumerate(parsed):
            if p is None or results[i] is None:
                continue
            links_upserted = 0
            prs_linked = 0
            if self._graph is None:
                results[i]["warnings"].append(
                    "graph unavailable: task not added to task graph")
            else:
                try:
                    self._graph.upsert_task(p["key"], p["aliases"], p["title"],
                                            p["status"], p["url"], p["project"])
                    if p["links_supplied"]:
                        links_upserted = self._graph.replace_links(p["key"], p["links"])
                except Exception as e:
                    log.warning("index_batch: сбой графа для %s", p["key"], exc_info=True)
                    results[i]["warnings"].append(f"graph: {type(e).__name__}: {e}")
                # Собираем PR-ссылки для батчевого MERGE (один запрос после цикла).
                if results[i]["embedded"]:
                    refs = extract_pr_refs(p["description"])
                    pr_pairs.extend((p["key"], ref) for ref in refs)
                    prs_linked = len(refs)
            results[i]["links_upserted"] = links_upserted
            results[i]["prs_linked"] = prs_linked

        # Батчевый MERGE IMPLEMENTED_BY — один запрос вместо N×M round-trip.
        if pr_pairs and self._graph is not None:
            try:
                self._graph.link_prs_batch(pr_pairs)
            except Exception:
                log.warning("index_batch: сбой батчевой PR-линковки", exc_info=True)

        return results

    def refresh_meta_batch(self, metas: list[dict]) -> dict:
        """Дешёвый self-healing meta-refresh (PRI-207): backfill плоских
        метаданных (project/title/status/url/aliases) для задач ниже watermark.
        Стор — один батч update_meta_batch (UPDATE по key: 0 строк, если задачи
        нет — НЕ воскрешает её в сторе); граф — upsert_task per-task (MERGE: узел
        :Task создаётся при отсутствии — заживляет прежний fail-soft пропуск графа),
        fail-soft. НИКОГДА не эмбедит и не upsert-ит полную строку в стор и не
        линкует PR. metas — как из normalize_meta."""
        metas = [{k: v for k, v in m.items() if k != "links"}
                 for m in metas if isinstance(m, dict) and m.get("key")]
        if not metas:
            return {"meta_refreshed": 0, "warnings": []}
        warnings: list[str] = []
        try:
            self._store.update_meta_batch(metas)
        except Exception as e:
            log.warning("refresh_meta_batch: сбой update_meta_batch", exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")
        if self._graph is None:
            warnings.append("graph unavailable: task projects not refreshed in graph")
        else:
            for m in metas:
                try:
                    self._graph.upsert_task(
                        m["key"], m.get("aliases") or [], m.get("title") or "",
                        m.get("status"), m.get("url"), m.get("project") or "")
                except Exception as e:
                    log.warning("refresh_meta_batch: сбой графа для %s",
                                m["key"], exc_info=True)
                    warnings.append(f"graph {m['key']}: {type(e).__name__}: {e}")
        return {"meta_refreshed": len(metas), "warnings": warnings}

    def search_tasks(self, query: str, top_k: int | None = None,
                     project: str | None = None) -> str:
        """Похожие задачи (RRF, без реранкера) с рельсой ceiling (PRI-202).

        top_k — override потолка (None → дефолт-константа). Фетчим больше кандидатов,
        возвращаем ≤ceiling, при хвосте дописываем заметку. Пусто/сбой → нота.
        """
        from reviewer.policy.context_limits import TasksLimits
        ceiling = top_k or TasksLimits.ceiling
        try:
            vec = self._embedder.embed_query(query)
            hits = self._store.search(query, vec, top_k=max(ceiling * 3, 30), project=project)
        except Exception:
            log.warning("search_tasks: сбой поиска по запросу %r", query, exc_info=True)
            return "(task search unavailable)"
        if not hits:
            return "(no similar tasks found)"
        total = len(hits)
        shown = hits[:ceiling]
        # Ранг-ординал — стабильный, query-независимый сигнал для relevance-фильтра
        # (solve-task/review-pr прунят по порядку). Score даём с 4 знаками: RRF лежит
        # в ≈0.016–0.033, и грубая точность схлопнула бы близкие задачи в одно число.
        lines = [f"{i}. {h.key} [{h.status or '—'}] {h.title} (score {h.score:.4f})"
                 for i, h in enumerate(shown, 1)]
        if total > ceiling:
            lines.append(f"— показано {ceiling} из {total} (рельса ceiling). "
                         f"Перевызови с большим ceiling для остальных.")
        return "\n".join(lines)

    def get_task_context(self, key: str, project: str | None = None) -> str:
        """Граф-контекст задачи: связанные задачи → их PR → код. Деградация → нота."""
        if self._graph is None:
            return "(task graph unavailable)"
        try:
            ctx = self._graph.task_context(key, project or "")
        except Exception:
            log.warning("get_task_context: сбой обхода графа для %s", key, exc_info=True)
            return "(task graph unavailable)"
        if not ctx:
            return f"(no task '{key}' in task graph)"
        return _format_task_context(ctx, self._max_chars)

    def count_tasks(self, project: str | None = None) -> int:
        """Число проиндексированных :Task проекта (best-effort). Граф None/сбой → 0.

        Read-only: считает узлы графа, не ходит на доску. Источник размера доски для
        рекомендации context_limits.search_tasks в скилле configure-review."""
        if self._graph is None:
            return 0
        try:
            return int(self._graph.count(project or ""))
        except Exception:
            log.warning("count_tasks: сбой графа (project=%s)", project, exc_info=True)
            return 0

    def get_task(self, key: str, project: str | None = None) -> dict | None:
        """Нормализованный TaskBrief задачи из стора (store-first одиночное чтение).

        Источник — Postgres ``tasks`` (заполнен sync_board), включая snapshot links.
        Граф не трогаем: PRs и код остаются за get_task_context. criteria=[] — требования несёт description
        (как в board-MCP-пути). Miss/сбой стора → None, чтобы вызывающий фолбэкнул.
        """
        try:
            row = self._store.get_task(key, project=project)
        except Exception:
            log.warning("get_task: сбой стора для %s", key, exc_info=True)
            return None
        if row is None:
            return None
        return {
            "key": row.key,
            "aliases": list(row.aliases or []),
            "title": row.title,
            "description": row.description,
            "criteria": [],
            "status": row.status,
            "url": row.url,
            "attachments": list(row.attachments or []),
            "links": list(row.links or []),
        }

    def link_review(self, task_key: str, pr: PRRef, touched_node_ids: list[str]) -> None:
        """Авто-линковка PR↔задача↔код (fail-soft; no-op без графа/ключа)."""
        if self._graph is None or not task_key:
            return
        try:
            self._graph.link_pr(task_key, pr, list(touched_node_ids or []))
        except Exception:
            log.warning("link_review: сбой линковки PR для %s", task_key, exc_info=True)

    def purge_orphaned_tasks(
        self,
        active_keys: list[str],
        *,
        keep_with_prs: bool = True,
        project: str | None = None,
    ) -> dict:
        """Удалить задачи вне active_keys. При project — скоуп по проекту (PRI-170)."""
        warnings: list[str] = []
        active = set(active_keys)

        # Вселенная ключей-кандидатов = стор ∪ граф. Стабы :Task (upsert_links/
        # link_pr) живут только в графе и в стор не попадают — без учёта ключей
        # графа они никогда не вычислялись как orphaned и оставались навсегда.
        # Сбой любого слоя fail-soft: чистим то, что смогли перечислить.
        all_keys: set[str] = set()
        try:
            all_keys |= set(self._store.list_keys(project=project))
        except Exception as e:
            log.warning("purge_orphaned_tasks: сбой list_keys", exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")
        if self._graph is not None:
            try:
                all_keys |= set(self._graph.list_keys(project or ""))
            except Exception as e:
                log.warning("purge_orphaned_tasks: сбой list_keys (graph)", exc_info=True)
                warnings.append(f"graph: {type(e).__name__}: {e}")

        orphaned = all_keys - active
        protected: set[str] = set()

        if keep_with_prs and self._graph is not None:
            try:
                pr_keys = self._graph.keys_with_prs(project or "")
                protected = orphaned & pr_keys
                orphaned = orphaned - protected
            except Exception as e:
                log.warning("purge_orphaned_tasks: сбой keys_with_prs", exc_info=True)
                warnings.append(f"graph: {type(e).__name__}: {e}")

        to_delete = list(orphaned)
        deleted_store = 0
        try:
            deleted_store = self._store.delete_tasks(to_delete)
        except Exception as e:
            log.warning("purge_orphaned_tasks: сбой delete_tasks (store)", exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")

        deleted_graph = 0
        if self._graph is not None:
            try:
                deleted_graph = self._graph.delete_tasks(to_delete)
            except Exception as e:
                log.warning("purge_orphaned_tasks: сбой delete_tasks (graph)", exc_info=True)
                warnings.append(f"graph: {type(e).__name__}: {e}")

        return {
            "deleted_store": deleted_store,
            "deleted_graph": deleted_graph,
            "protected_prs": len(protected),
            "warnings": warnings,
        }


def _format_task_context(ctx: dict, max_chars: int) -> str:
    # Заголовочный блок (всегда сохраняется): задача, статус, url.
    head = f"Task {ctx.get('key')}"
    if ctx.get("status"):
        head += f" [{ctx['status']}]"
    if ctx.get("title"):
        head += f": {ctx['title']}"
    head_block = [head]
    if ctx.get("url"):
        head_block.append(f"  url: {ctx['url']}")

    # Остальное — атомарные блоки: каждый PR и каждая связанная задача отдельным
    # блоком; заголовок секции прикреплён к первому её блоку, чтобы при усечении
    # на границе блока он не осиротел (а структура списка не рвалась посреди строки).
    blocks: list[list[str]] = []

    prs = ctx.get("prs") or []
    for idx, p in enumerate(prs):
        line = f"    - {p.get('id')}"
        if p.get("url"):
            line += f" ({p['url']})"
        touched = ", ".join(p.get("touched") or [])
        if touched:
            line += f" touches: {touched}"
        blocks.append(["  Implemented by PRs:", line] if idx == 0 else [line])

    linked = ctx.get("linked") or []
    for idx, n in enumerate(linked):
        ltype = n.get("type") or "relates"
        nstatus = f" [{n['status']}]" if n.get("status") else ""
        line = f"    - [{ltype}] {n.get('key')}{nstatus}: {n.get('title') or ''}"
        npr = [p.get("id") for p in (n.get("prs") or []) if p.get("id")]
        if npr:
            line += "  PRs: " + ", ".join(npr)
        blocks.append(["  Linked tasks:", line] if idx == 0 else [line])

    # Жадная сборка по границам блоков: голова обязательна, далее каждый блок
    # добавляем целиком, пока влезает в бюджет; иначе стоп с нотой о хвосте.
    out_lines = list(head_block)
    total = len("\n".join(out_lines))
    dropped = 0
    for i, block in enumerate(blocks):
        btext = "\n".join(block)
        if total + 1 + len(btext) <= max_chars:
            out_lines.extend(block)
            total += 1 + len(btext)
        else:
            dropped = len(blocks) - i
            break

    out = "\n".join(out_lines)
    if dropped:
        out += f"\n… (truncated {dropped} more)"
    return out
