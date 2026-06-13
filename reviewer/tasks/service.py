"""Сервис задач: index_task / search_tasks / get_task_context / link_review.

Оркестрирует TaskStore (эмбеддинги) + TaskGraph (граф) + эмбеддер. Fail-soft по
слоям: сбой одного слоя не валит остальное (деградация как фазы 1/2).
"""
from __future__ import annotations

import logging

from reviewer.tasks.graph import PRRef
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
                    "warnings": ["task has no key"]}
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

        return {"key": key, "embedded": embedded,
                "links_upserted": links_upserted, "warnings": warnings}

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

    def link_review(self, task_key: str, pr: PRRef, touched_node_ids: list[str]) -> None:
        """Авто-линковка PR↔задача↔код (fail-soft; no-op без графа/ключа)."""
        if self._graph is None or not task_key:
            return
        try:
            self._graph.link_pr(task_key, pr, list(touched_node_ids or []))
        except Exception:
            log.warning("link_review: сбой линковки PR для %s", task_key, exc_info=True)


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
