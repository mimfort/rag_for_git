from __future__ import annotations
from neo4j import GraphDatabase


class GraphStore:
    def __init__(self, uri: str, user: str, password: str):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    def init_schema(self) -> None:
        self._driver.execute_query(
            "CREATE CONSTRAINT sym_id IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE s.id IS UNIQUE")

    def clear(self) -> None:
        self._driver.execute_query("MATCH (n) DETACH DELETE n")

    def upsert_nodes(self, node_ids: list[str]) -> None:
        self._driver.execute_query(
            "UNWIND $ids AS id MERGE (:Symbol {id: id})",
            ids=list(node_ids))

    def upsert_edges(self, edges: list[tuple[str, str, str]]) -> None:
        by_rel: dict[str, list[dict]] = {}
        for src, rel, dst in edges:
            by_rel.setdefault(rel, []).append({"src": src, "dst": dst})
        for rel, rows in by_rel.items():
            self._driver.execute_query(
                f"UNWIND $rows AS r "
                f"MATCH (a:Symbol {{id: r.src}}) MATCH (b:Symbol {{id: r.dst}}) "
                f"MERGE (a)-[:{rel}]->(b)",
                rows=rows)

    def expand(self, node_ids: list[str], hops: int = 2) -> set[str]:
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid MATCH (s:Symbol {{id: sid}}) "
            f"MATCH (s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-(n:Symbol) "
            f"RETURN DISTINCT n.id AS id",
            ids=list(node_ids))
        return {r["id"] for r in records}

    def callers(self, node_ids: list[str]) -> set[str]:
        """Кто вызывает данные символы — направленные входящие CALLS."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid MATCH (c:Symbol)-[:CALLS]->(s:Symbol {id: sid}) "
            "RETURN DISTINCT c.id AS id",
            ids=list(node_ids))
        return {r["id"] for r in records}

    def find_symbol(self, name: str) -> list[str]:
        """Резолв имени символа в node_id ('path#fqn'). Точное имя (#name / .name)
        приоритетнее подстроки. Возврат — до 25 id, точные сперва."""
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol) WHERE s.id CONTAINS $needle "
            "RETURN s.id AS id LIMIT 50",
            needle=name)
        ids = [r["id"] for r in records]
        suffix = "#" + name
        exact = [i for i in ids if i.endswith(suffix) or i.endswith("." + name)]
        rest = [i for i in ids if i not in exact]
        return (exact + rest)[:25]
