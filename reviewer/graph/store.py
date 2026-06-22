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
        # branch-scoped уникальность: id уникален в пределах (repo, branch).
        self._driver.execute_query("DROP CONSTRAINT sym_id IF EXISTS")
        self._driver.execute_query("DROP CONSTRAINT sym_repo_id IF EXISTS")
        self._driver.execute_query(
            "CREATE CONSTRAINT sym_repo_branch_id IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE (s.repo, s.branch, s.id) IS UNIQUE")
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

    def migrate_legacy_branch(self, primary: str) -> None:
        """Проставить branch=<primary> символам без ветки (legacy-узлы). Идемпотентно."""
        self._driver.execute_query(
            "MATCH (s:Symbol) WHERE s.branch IS NULL SET s.branch = $primary",
            primary=primary)

    def clear(self, repo: str | None = None, *, branch: str | None = None) -> None:
        """Удалить узлы/рёбра: весь граф (repo=None), репо целиком (branch=None),
        или только одну ветку репо (branch задан)."""
        if repo is None:
            self._driver.execute_query("MATCH (n) DETACH DELETE n")
        elif branch is None:
            self._driver.execute_query(
                "MATCH (s:Symbol {repo: $repo}) DETACH DELETE s", repo=repo)
        else:
            self._driver.execute_query(
                "MATCH (s:Symbol {repo: $repo, branch: $branch}) DETACH DELETE s",
                repo=repo, branch=branch)

    def upsert_nodes(self, repo: str, node_ids: list[str], *, branch: str = "") -> None:
        self._driver.execute_query(
            "UNWIND $ids AS id MERGE (:Symbol {repo: $repo, branch: $branch, id: id})",
            ids=list(node_ids), repo=repo, branch=branch)

    def upsert_edges(self, repo: str, edges: list[tuple[str, str, str]], *,
                     branch: str = "") -> None:
        by_rel: dict[str, list[dict]] = {}
        for src, rel, dst in edges:
            by_rel.setdefault(rel, []).append({"src": src, "dst": dst})
        for rel, rows in by_rel.items():
            self._driver.execute_query(
                f"UNWIND $rows AS r "
                f"MATCH (a:Symbol {{repo: $repo, branch: $branch, id: r.src}}) "
                f"MATCH (b:Symbol {{repo: $repo, branch: $branch, id: r.dst}}) "
                f"MERGE (a)-[:{rel}]->(b)",
                rows=rows, repo=repo, branch=branch)

    def expand(self, repo: str, node_ids: list[str], hops: int = 2, *,
               branch: str = "") -> set[str]:
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid MATCH (s:Symbol {{repo: $repo, branch: $branch, id: sid}}) "
            f"MATCH (s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-"
            f"(n:Symbol {{repo: $repo, branch: $branch}}) "
            f"RETURN DISTINCT n.id AS id",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"] for r in records}

    def callers(self, repo: str, node_ids: list[str], *, branch: str = "") -> set[str]:
        """Кто вызывает данные символы — направленные входящие CALLS."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->"
            "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
            "RETURN DISTINCT c.id AS id",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"] for r in records}

    def callers_detailed(self, repo: str, node_ids: list[str], *,
                         branch: str = "") -> list[dict]:
        """Вызывающие символы с типом ребра — входящие CALLS.
        Элементы: {"id": <node_id>, "rel": "CALLS"}, упорядочены по id."""
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->"
            "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
            "RETURN DISTINCT c.id AS id ORDER BY id",
            ids=list(node_ids), repo=repo, branch=branch)
        return [{"id": r["id"], "rel": "CALLS"} for r in records]

    def expand_detailed(self, repo: str, node_ids: list[str], hops: int = 2, *,
                        branch: str = "") -> list[dict]:
        """Соседи символа с типами рёбер кратчайшего пути и дистанцией.
        Элементы: {"id", "rels": [тип,...], "dist": int}; seed исключён;
        упорядочены по (dist, id).
        Обход рёбер undirected — соседи в обе стороны (как expand).
        При нескольких seed n.id<>sid исключает лишь свой sid (в тулах seed один)."""
        records, _, _ = self._driver.execute_query(
            f"UNWIND $ids AS sid "
            f"MATCH (s:Symbol {{repo: $repo, branch: $branch, id: sid}}) "
            f"MATCH p=(s)-[:CALLS|IMPLEMENTS|TESTED_BY*1..{hops}]-"
            f"(n:Symbol {{repo: $repo, branch: $branch}}) "
            f"WHERE n.id <> sid "
            f"WITH n.id AS id, [r IN relationships(p) | type(r)] AS rels, length(p) AS dist "
            f"ORDER BY dist "
            f"WITH id, collect({{rels: rels, dist: dist}})[0] AS best "
            f"RETURN id, best.rels AS rels, best.dist AS dist "
            f"ORDER BY best.dist, id",
            ids=list(node_ids), repo=repo, branch=branch)
        return [{"id": r["id"], "rels": list(r["rels"]), "dist": r["dist"]} for r in records]

    def in_degree(self, repo: str, node_ids: list[str], *, branch: str = "") -> dict[str, int]:
        """Число входящих CALLS на каждый символ (центральность = сколько мест зависит).

        Узлы без вызывающих в результат не попадают → вызывающий трактует
        отсутствие ключа как 0.
        """
        if not node_ids:
            return {}
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:CALLS]->"
            "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
            "RETURN sid AS id, count(c) AS deg",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"]: r["deg"] for r in records}

    def find_symbol(self, repo: str, name: str, *, branch: str = "") -> list[str]:
        """Резолв имени символа в node_id ('path#fqn') в пределах (repo, branch).
        Точное имя (#name / .name) приоритетнее подстроки. Возврат — до 25 id."""
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo, branch: $branch}) WHERE s.id CONTAINS $needle "
            "RETURN s.id AS id "
            "ORDER BY (CASE WHEN s.id ENDS WITH $suffix OR s.id ENDS WITH $dotname "
            "THEN 0 ELSE 1 END), s.id "
            "LIMIT 25",
            repo=repo, branch=branch, needle=name, suffix="#" + name, dotname="." + name)
        return [r["id"] for r in records]

    def symbols_for_paths(self, repo: str, paths: list[str], *, branch: str = "") -> set[str]:
        """node_id всех :Symbol (repo, branch), чьи пути в paths (id начинается с 'path#')."""
        if not paths:
            return set()
        prefixes = [p + "#" for p in paths]
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo, branch: $branch}) "
            "WHERE any(p IN $prefixes WHERE s.id STARTS WITH p) "
            "RETURN s.id AS id",
            repo=repo, branch=branch, prefixes=prefixes)
        return {r["id"] for r in records}

    def delete_symbols(self, repo: str, ids: list[str], *, branch: str = "") -> None:
        """DETACH DELETE перечисленных символов (исчезнувшие/переименованные)."""
        if not ids:
            return
        self._driver.execute_query(
            "UNWIND $ids AS id MATCH (s:Symbol {repo: $repo, branch: $branch, id: id}) "
            "DETACH DELETE s",
            ids=list(ids), repo=repo, branch=branch)

    def delete_outgoing_calls(self, repo: str, ids: list[str], *, branch: str = "") -> None:
        """Снести только ИСХОДЯЩИЕ CALLS у символов (входящие сохраняются)."""
        if not ids:
            return
        self._driver.execute_query(
            "UNWIND $ids AS id "
            "MATCH (s:Symbol {repo: $repo, branch: $branch, id: id})-[r:CALLS]->() DELETE r",
            ids=list(ids), repo=repo, branch=branch)

    def count_nodes(self, repo: str, branch: str = "") -> int:
        """Число :Symbol-узлов в (repo, branch)."""
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo, branch: $branch}) RETURN count(s) AS n",
            repo=repo, branch=branch)
        return records[0]["n"]
