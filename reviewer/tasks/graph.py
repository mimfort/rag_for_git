"""Граф задач в Neo4j: узлы :Task/:PR, рёбра TASK_LINK/IMPLEMENTED_BY/TOUCHES.

Переиспользует Neo4j-драйвер :class:`GraphStore` (один коннект). Рёбра TOUCHES
ссылаются на :Symbol того же графа кода — сшивка через node_id='path#fqn'.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PRRef:
    """Ссылка на PR для узла :PR графа задач."""

    repo: str          # "owner/name"
    number: int
    url: str
    sha: str

    @property
    def id(self) -> str:
        return f"{self.repo}#{self.number}"


class TaskGraph:
    """Узлы и рёбра задач в Neo4j поверх общего драйвера GraphStore."""

    def __init__(self, driver) -> None:
        self._driver = driver

    def upsert_task(self, key: str, aliases: list[str], title: str,
                    status: str | None, url: str | None) -> None:
        """Upsert узла :Task. codes = [key, ...aliases] для резолва по любому коду."""
        codes = [key] + [a for a in (aliases or []) if a and a != key]
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) "
            "SET t.codes=$codes, t.title=$title, t.status=$status, t.url=$url",
            key=key, codes=codes, title=title, status=status, url=url)

    def upsert_links(self, key: str, links: list[dict]) -> int:
        """Рёбра TASK_LINK из явных board-links. Несуществующий сосед → стаб :Task."""
        rows = [{"key": lk["key"], "title": lk.get("title") or "",
                 "type": lk.get("type") or "relates"}
                for lk in links if lk.get("key")]
        if not rows:
            return 0
        self._driver.execute_query(
            "MATCH (t:Task {key: $key}) "
            "UNWIND $rows AS lk "
            "MERGE (n:Task {key: lk.key}) "
            "  ON CREATE SET n.title=lk.title, n.codes=[lk.key] "
            "MERGE (t)-[:TASK_LINK {type: lk.type}]->(n)",
            key=key, rows=rows)
        return len(rows)

    def link_pr(self, task_key: str, pr: PRRef, touched_node_ids: list[str]) -> None:
        """(:Task)-[:IMPLEMENTED_BY]->(:PR)-[:TOUCHES]->(:Symbol). Стаб :Task/:Symbol при отсутствии."""
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) ON CREATE SET t.codes=[$key] "
            "MERGE (p:PR {id: $pid}) "
            "  SET p.repo=$repo, p.number=$number, p.url=$url, p.sha=$sha "
            "MERGE (t)-[:IMPLEMENTED_BY]->(p) "
            "WITH p "
            "UNWIND $touched AS nid "
            "MERGE (s:Symbol {id: nid}) "
            "MERGE (p)-[:TOUCHES]->(s)",
            key=task_key, pid=pr.id, repo=pr.repo, number=pr.number,
            url=pr.url, sha=pr.sha, touched=list(touched_node_ids or []))

    def task_context(self, key: str) -> dict:
        """Обход: сама задача + её PR/код + TASK_LINK-соседи и их PR. {} если не найдена."""
        records, _, _ = self._driver.execute_query(
            "MATCH (t:Task) WHERE $k IN t.codes "
            "RETURN t.key AS key, t.title AS title, t.status AS status, t.url AS url, "
            "[ (t)-[:IMPLEMENTED_BY]->(p:PR) | "
            "  {id: p.id, url: p.url, sha: p.sha, "
            "   touched: [ (p)-[:TOUCHES]->(s:Symbol) | s.id ]} ] AS prs, "
            "[ (t)-[l:TASK_LINK]-(n:Task) | "
            "  {key: n.key, title: n.title, status: n.status, type: l.type, "
            "   prs: [ (n)-[:IMPLEMENTED_BY]->(np:PR) | {id: np.id, url: np.url} ]} ] AS linked "
            "LIMIT 1",
            k=key)
        if not records:
            return {}
        r = records[0]
        return {"key": r["key"], "title": r["title"], "status": r["status"],
                "url": r["url"], "prs": r["prs"], "linked": r["linked"]}
