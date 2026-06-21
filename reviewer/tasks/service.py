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


class TaskService:
    """Оркестрация индексации и обхода графа задач."""

    def __init__(self, store, graph, embedder, *, max_chars: int = 8000) -> None:
        self._store = store
        self._graph = graph          # None, если Neo4j не подключён
        self._embedder = embedder
        self._max_chars = max_chars

    def index_task(self, task: dict) -> dict:
        """Проиндексировать нормализованный TaskBrief: эмбеддинг (дедуп) + граф."""
        key = task.get("key") if isinstance(task, dict) else None
        if not key:
            return {"key": None, "embedded": False, "links_upserted": 0,
                    "prs_linked": 0, "warnings": ["task has no key"]}
        aliases = [a for a in (task.get("aliases") or []) if a and a != key]
        title = task.get("title") or ""
        description = task.get("description") or ""
        criteria = task.get("criteria") or []
        status = task.get("status")
        url = task.get("url")
        links = [lk for lk in (task.get("links") or [])
                 if isinstance(lk, dict) and lk.get("key")]
        text = build_task_text(title, description, criteria)
        chash = task_content_hash(text)
        warnings: list[str] = []

        embedded = False
        try:
            prev = self._store.existing_hash(key)
            if prev == chash:
                self._store.update_meta(key, title, status, url, aliases)
            else:
                vec = self._embedder.embed_documents([text])[0]
                self._store.upsert_task(TaskRow(
                    key=key, aliases=aliases, title=title, description=description,
                    status=status, url=url, content_hash=chash, text=text,
                    embedding=vec))
                embedded = True
        except Exception as e:
            log.warning("index_task: сбой store для %s", key, exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")

        links_upserted = 0
        if self._graph is None:
            warnings.append("graph unavailable: task not added to task graph")
        else:
            try:
                self._graph.upsert_task(key, aliases, title, status, url)
                if links:
                    links_upserted = self._graph.upsert_links(key, links)
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
                "links_upserted": links_upserted, "prs_linked": prs_linked,
                "warnings": warnings}

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
                              "prs_linked": 0, "warnings": ["task has no key"]}
                parsed.append(None)
                continue
            aliases = [a for a in (task.get("aliases") or []) if a and a != key]
            title = task.get("title") or ""
            description = task.get("description") or ""
            criteria = task.get("criteria") or []
            status = task.get("status")
            url = task.get("url")
            links = [lk for lk in (task.get("links") or [])
                     if isinstance(lk, dict) and lk.get("key")]
            text = build_task_text(title, description, criteria)
            chash = task_content_hash(text)
            parsed.append({"key": key, "aliases": aliases, "title": title,
                           "description": description, "status": status, "url": url,
                           "links": links, "text": text, "chash": chash})

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
                              "prs_linked": 0, "warnings": [f"store: {type(e).__name__}: {e}"]}
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
            if embed_err:
                warnings.append(embed_err)
            else:
                try:
                    self._store.upsert_task(TaskRow(
                        key=p["key"], aliases=p["aliases"], title=p["title"],
                        description=p["description"], status=p["status"], url=p["url"],
                        content_hash=p["chash"], text=p["text"], embedding=embeddings[i]))
                    embedded = True
                except Exception as e:
                    log.warning("index_batch: сбой store для %s", p["key"], exc_info=True)
                    warnings.append(f"store: {type(e).__name__}: {e}")
            results[i] = {"key": p["key"], "embedded": embedded,
                          "links_upserted": 0, "warnings": warnings}

        # Шаг 5: update_meta для неизменившихся задач
        for i in meta_only:
            p = parsed[i]
            warnings: list[str] = []
            try:
                self._store.update_meta(p["key"], p["title"], p["status"],
                                        p["url"], p["aliases"])
            except Exception as e:
                log.warning("index_batch: сбой update_meta для %s", p["key"], exc_info=True)
                warnings.append(f"store: {type(e).__name__}: {e}")
            results[i] = {"key": p["key"], "embedded": False,
                          "links_upserted": 0, "warnings": warnings}

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
                                            p["status"], p["url"])
                    if p["links"]:
                        links_upserted = self._graph.upsert_links(p["key"], p["links"])
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

    def search_tasks(self, query: str, top_k: int = 5) -> str:
        """Похожие по смыслу задачи (гибрид-поиск по корпусу). Пусто/сбой → текстовая нота."""
        try:
            vec = self._embedder.embed_query(query)
            hits = self._store.search(query, vec, top_k=top_k)
        except Exception:
            log.warning("search_tasks: сбой поиска по запросу %r", query, exc_info=True)
            return "(task search unavailable)"
        if not hits:
            return "(no similar tasks found)"
        return "\n".join(
            f"- {h.key} [{h.status or '—'}] {h.title} (score {h.score:.2f})"
            for h in hits)

    def get_task_context(self, key: str) -> str:
        """Граф-контекст задачи: связанные задачи → их PR → код. Деградация → нота."""
        if self._graph is None:
            return "(task graph unavailable)"
        try:
            ctx = self._graph.task_context(key)
        except Exception:
            log.warning("get_task_context: сбой обхода графа для %s", key, exc_info=True)
            return "(task graph unavailable)"
        if not ctx:
            return f"(no task '{key}' in task graph)"
        return _format_task_context(ctx, self._max_chars)

    def get_task(self, key: str) -> dict | None:
        """Нормализованный TaskBrief задачи из стора (store-first одиночное чтение).

        Источник — Postgres ``tasks`` (заполнен sync_board). Граф не трогаем: links/PRs
        остаются за get_task_context. criteria=[] — требования несёт description
        (как в board-MCP-пути). Miss/сбой стора → None, чтобы вызывающий фолбэкнул.
        """
        try:
            row = self._store.get_task(key)
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
    ) -> dict:
        """Удалить задачи, отсутствующие в active_keys. Fail-soft по слоям."""
        warnings: list[str] = []
        active = set(active_keys)

        # Вселенная ключей-кандидатов = стор ∪ граф. Стабы :Task (upsert_links/
        # link_pr) живут только в графе и в стор не попадают — без учёта ключей
        # графа они никогда не вычислялись как orphaned и оставались навсегда.
        # Сбой любого слоя fail-soft: чистим то, что смогли перечислить.
        all_keys: set[str] = set()
        try:
            all_keys |= set(self._store.list_keys())
        except Exception as e:
            log.warning("purge_orphaned_tasks: сбой list_keys", exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")
        if self._graph is not None:
            try:
                all_keys |= set(self._graph.list_keys())
            except Exception as e:
                log.warning("purge_orphaned_tasks: сбой list_keys (graph)", exc_info=True)
                warnings.append(f"graph: {type(e).__name__}: {e}")

        orphaned = all_keys - active
        protected: set[str] = set()

        if keep_with_prs and self._graph is not None:
            try:
                pr_keys = self._graph.keys_with_prs()
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
    lines: list[str] = []
    head = f"Task {ctx.get('key')}"
    if ctx.get("status"):
        head += f" [{ctx['status']}]"
    if ctx.get("title"):
        head += f": {ctx['title']}"
    lines.append(head)
    if ctx.get("url"):
        lines.append(f"  url: {ctx['url']}")

    prs = ctx.get("prs") or []
    if prs:
        lines.append("  Implemented by PRs:")
        for p in prs:
            line = f"    - {p.get('id')}"
            if p.get("url"):
                line += f" ({p['url']})"
            touched = ", ".join(p.get("touched") or [])
            if touched:
                line += f" touches: {touched}"
            lines.append(line)

    linked = ctx.get("linked") or []
    if linked:
        lines.append("  Linked tasks:")
        for n in linked:
            ltype = n.get("type") or "relates"
            nstatus = f" [{n['status']}]" if n.get("status") else ""
            line = f"    - [{ltype}] {n.get('key')}{nstatus}: {n.get('title') or ''}"
            npr = [p.get("id") for p in (n.get("prs") or []) if p.get("id")]
            if npr:
                line += "  PRs: " + ", ".join(npr)
            lines.append(line)

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n… (truncated)"
    return out
