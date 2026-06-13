from __future__ import annotations
from neo4j import GraphDatabase


class GraphStore:
    def __init__(self, uri: str, user: str, password: str):
        # notifications_min_severity="OFF" глушит notification-спам драйвера
        # (напр. «relationship type IMPLEMENTS does not exist», когда граф наполнен
        # только частью типов рёбер) — на выполнение запросов это не влияет.
        self._driver = GraphDatabase.driver(
            uri, auth=(user, password), notifications_min_severity="OFF")

    @property
    def driver(self):
        """Neo4j-драйвер — для шаринга с TaskGraph (один коннект на инстанс)."""
        return self._driver

    def close(self) -> None:
        self._driver.close()

    def init_schema(self) -> None:
        # Старый одно-property constraint снимаем — id больше не глобально уникален.
        self._driver.execute_query("DROP CONSTRAINT sym_id IF EXISTS")
        # Композитная уникальность (repo, id) — property-uniqueness, есть в Neo4j 5 Community.
        self._driver.execute_query(
            "CREATE CONSTRAINT sym_repo_id IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE (s.repo, s.id) IS UNIQUE")
        # Граф задач (фаза 3): уникальность :Task(key) и :PR(id) + индекс на codes
        # (резолв по любому коду в WHERE $k IN t.codes).
        self._driver.execute_query(
            "CREATE CONSTRAINT task_key IF NOT EXISTS "
            "FOR (t:Task) REQUIRE t.key IS UNIQUE")
        self._driver.execute_query(
            "CREATE CONSTRAINT pr_id IF NOT EXISTS "
            "FOR (p:PR) REQUIRE p.id IS UNIQUE")
        self._driver.execute_query(
            "CREATE INDEX task_codes IF NOT EXISTS FOR (t:Task) ON (t.codes)")

    def clear(self, repo: str | None = None) -> None:
        """Удалить узлы/рёбра репозитория (repo) или весь граф (repo=None — тесты)."""
        if repo is None:
            self._driver.execute_query("MATCH (n) DETACH DELETE n")
        else:
            self._driver.execute_query(
                "MATCH (s:Symbol {repo: $repo}) DETACH DELETE s", repo=repo)

    def upsert_nodes(self, repo: str, node_ids: list[str]) -> None:
        self._driver.execute_query(
            "UNWIND $ids AS id MERGE (:Symbol {repo: $repo, id: id})",
            ids=list(node_ids), repo=repo)

    def upsert_edges(self, repo: str, edges: list[tuple[str, str, str]]) -> None:
        by_rel: dict[str, list[dict]] = {}
        for src, rel, dst in edges:
            by_rel.setdefault(rel, []).append({"src": src, "dst": dst})
        for rel, rows in by_rel.items():
            self._driver.execute_query(
                f"UNWIND $rows AS r "
                f"MATCH (a:Symbol {{repo: $repo, id: r.src}}) "
                f"MATCH (b:Symbol {{repo: $repo, id: r.dst}}) "
                f"MERGE (a)-[:{rel}]->(b)",
                rows=rows, repo=repo)

    def expand(self, repo: str, node_ids: list[str], hops: int = 2) -> set[str]:
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid MATCH (s:Symbol {{repo: $repo, id: sid}}) "
            f"MATCH (s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-(n:Symbol {{repo: $repo}}) "
            f"RETURN DISTINCT n.id AS id",
            ids=list(node_ids), repo=repo)
        return {r["id"] for r in records}

    def callers(self, repo: str, node_ids: list[str]) -> set[str]:
        """Кто вызывает данные символы — направленные входящие CALLS."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo})-[:CALLS]->(s:Symbol {repo: $repo, id: sid}) "
            "RETURN DISTINCT c.id AS id",
            ids=list(node_ids), repo=repo)
        return {r["id"] for r in records}

    def find_symbol(self, repo: str, name: str) -> list[str]:
        """Резолв имени символа в node_id ('path#fqn') в пределах repo. Точное имя
        (#name / .name) приоритетнее подстроки. Возврат — до 25 id."""
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo}) WHERE s.id CONTAINS $needle "
            "RETURN s.id AS id "
            "ORDER BY (CASE WHEN s.id ENDS WITH $suffix OR s.id ENDS WITH $dotname "
            "THEN 0 ELSE 1 END), s.id "
            "LIMIT 25",
            repo=repo, needle=name, suffix="#" + name, dotname="." + name)
        return [r["id"] for r in records]
