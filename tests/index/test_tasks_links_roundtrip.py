from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore
from reviewer.index.store import ChunkStore
from reviewer.tasks.graph import TaskGraph
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import TaskRow, TaskStore, build_task_text, task_content_hash

pytestmark = pytest.mark.integration


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _init_task_schema(settings: Settings) -> None:
    chunk_store = ChunkStore(settings.pg_dsn)
    try:
        chunk_store.init_schema()
    finally:
        chunk_store.close()


def _row(
    settings: Settings,
    key: str,
    *,
    title: str = "Authoritative links",
    description: str = "Проверить сохранение snapshot связей",
    criteria: list[str] | None = None,
    links: list[dict] | None,
) -> TaskRow:
    criteria = criteria or []
    text = build_task_text(title, description, criteria)
    return TaskRow(
        key=key,
        aliases=[_key("alias")],
        title=title,
        description=description,
        status="Open",
        url=None,
        content_hash=task_content_hash(text),
        text=text,
        embedding=[0.0] * settings.embedding_dim,
        project="PRI",
        links=links,
    )


def _link_snapshot(links: list[dict]) -> set[tuple[str, str, str]]:
    return {
        (link["key"], link.get("title") or "", link.get("type") or "relates")
        for link in links
    }


def test_authoritative_links_survive_restart_preserve_when_omitted_and_clear() -> None:
    settings = Settings()
    _init_task_schema(settings)
    task_key = _key("task")
    links = [
        {"key": _key("child"), "title": "Первый ребёнок", "type": "subtask"},
        {"key": _key("related"), "title": "Связанная задача", "type": "related"},
    ]
    first = TaskStore(settings.pg_dsn)
    second: TaskStore | None = None

    try:
        original = _row(settings, task_key, links=links)
        first.upsert_task(original)
        first.close()

        second = TaskStore(settings.pg_dsn)
        reopened = second.get_task(task_key)
        assert reopened is not None
        assert _link_snapshot(reopened.links or []) == _link_snapshot(links)

        second.upsert_task(replace(original, status="In Progress", links=None))
        preserved = second.get_task(task_key)
        assert preserved is not None
        assert _link_snapshot(preserved.links or []) == _link_snapshot(links)

        second.upsert_task(replace(original, status="Done", links=[]))
        cleared = second.get_task(task_key)
        assert cleared is not None
        assert cleared.links == []
    finally:
        first.close()
        if second is not None:
            second.close()
        cleanup = TaskStore(settings.pg_dsn)
        try:
            cleanup.delete_tasks([task_key])
        finally:
            cleanup.close()


class _NoEmbeddingExpected:
    def __init__(self) -> None:
        self.doc_calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls.append(list(texts))
        raise AssertionError("Неизменившаяся задача не должна переэмбеддиться")


def test_task_service_writes_same_filtered_links_to_postgres_and_neo4j() -> None:
    settings = Settings()
    _init_task_schema(settings)
    parent_key = _key("task")
    old_child_key = _key("old-child")
    child_key = _key("child")
    related_key = _key("related")
    graph_store = GraphStore(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
    )
    graph_store.init_schema()
    graph = TaskGraph(graph_store.driver)
    store = TaskStore(settings.pg_dsn)
    title = "Service authoritative links"
    description = "Не переэмбеддивать неизменившийся текст"
    criteria = ["Postgres и Neo4j получают один snapshot"]
    initial_links = [{"key": old_child_key, "title": "Старая", "type": "related"}]
    input_links = [
        {"key": child_key, "title": "Дочерняя", "type": "subtask"},
        {"title": "Без ключа", "type": "related"},
        42,
        {"key": related_key, "title": "Связанная", "type": "related"},
        {"key": None, "title": "Пустой ключ"},
    ]
    expected_links = [input_links[0], input_links[3]]
    embedder = _NoEmbeddingExpected()

    try:
        store.upsert_task(
            _row(
                settings,
                parent_key,
                title=title,
                description=description,
                criteria=criteria,
                links=initial_links,
            )
        )
        graph.upsert_task(parent_key, [], title, "Open", None, "PRI")
        graph.replace_links(parent_key, initial_links)

        result = TaskService(store, graph, embedder).index_task({
            "key": parent_key,
            "aliases": [],
            "title": title,
            "description": description,
            "criteria": criteria,
            "status": "Open",
            "url": None,
            "project": "PRI",
            "links": input_links,
        })

        postgres_row = store.get_task(parent_key)
        assert postgres_row is not None
        records, _, _ = graph_store.driver.execute_query(
            "MATCH (:Task {key: $key})-[link:TASK_LINK]->(target:Task) "
            "RETURN target.key AS key, target.title AS title, link.type AS type",
            key=parent_key,
        )
        neo4j_links = {
            (record["key"], record["title"] or "", record["type"])
            for record in records
        }

        assert result["embedded"] is False
        assert result["links_stored"] is True
        assert result["links_upserted"] == 2
        assert result["warnings"] == []
        assert embedder.doc_calls == []
        assert _link_snapshot(postgres_row.links or []) == _link_snapshot(expected_links)
        assert neo4j_links == _link_snapshot(expected_links)
    finally:
        store.delete_tasks([parent_key])
        store.close()
        graph.delete_tasks([parent_key, old_child_key, child_key, related_key])
        graph_store.close()
