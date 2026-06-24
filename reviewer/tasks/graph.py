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
                    status: str | None, url: str | None, project: str = "") -> None:
        """Upsert узла :Task. codes = [key, ...aliases]; project — метка скоупа (PRI-170)."""
        codes = [key] + [a for a in (aliases or []) if a and a != key]
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) "
            "SET t.codes=$codes, t.title=$title, t.status=$status, t.url=$url, "
            "t.project=$project",
            key=key, codes=codes, title=title, status=status, url=url, project=project)

    def upsert_links(self, key: str, links: list[dict]) -> int:
        """Рёбра TASK_LINK из явных board-links. Несуществующий сосед → стаб :Task.

        Предполагает, что узел :Task {key} уже существует (вызывающий сначала
        делает upsert_task); иначе MATCH ничего не находит и рёбра не создаются
        (тихий no-op).
        """
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

    def link_prs_batch(self, pairs: list[tuple[str, "PRRef"]]) -> int:
        """Батчевый UNWIND-MERGE для N пар (task_key, PRRef) без TOUCHES.

        Используется из index_batch вместо N×M вызовов link_pr — один execute_query.
        touched=[] для всех пар: код подтягивается лениво через get_pr_diff.
        """
        if not pairs:
            return 0
        rows = [{"key": key, "pid": pr.id, "repo": pr.repo,
                 "number": pr.number, "url": pr.url, "sha": pr.sha}
                for key, pr in pairs]
        self._driver.execute_query(
            "UNWIND $rows AS row "
            "MERGE (t:Task {key: row.key}) ON CREATE SET t.codes=[row.key] "
            "MERGE (p:PR {id: row.pid}) "
            "  SET p.repo=row.repo, p.number=row.number, p.url=row.url, "
            "      p.sha = CASE WHEN row.sha <> '' THEN row.sha ELSE coalesce(p.sha, '') END "
            "MERGE (t)-[:IMPLEMENTED_BY]->(p)",
            rows=rows)
        return len(pairs)

    def link_pr(self, task_key: str, pr: PRRef, touched_node_ids: list[str]) -> None:
        """(:Task)-[:IMPLEMENTED_BY]->(:PR)-[:TOUCHES]->(:Symbol). Symbol скоупится по pr.repo.

        Граф задач branch-agnostic: TOUCHES-символ создаётся под дефолтной веткой
        (sentinel ``branch=''`` — тот же дефолт, что у ``find_symbol``/``base_ref('')``).
        Это корреляционный маркер «PR затронул node_id», сшиваемый с код-графом по
        строке node_id, а не привязка к конкретной ветке кода.

        sha проставляется условно: пустой sha (линковка исторического PR из
        sync-tasks) не затирает реальный sha, ранее проставленный publish_review.
        """
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) ON CREATE SET t.codes=[$key] "
            "MERGE (p:PR {id: $pid}) "
            "  SET p.repo=$repo, p.number=$number, p.url=$url, "
            "      p.sha = CASE WHEN $sha <> '' THEN $sha ELSE coalesce(p.sha, '') END "
            "MERGE (t)-[:IMPLEMENTED_BY]->(p) "
            "WITH p "
            "UNWIND $touched AS nid "
            "MERGE (s:Symbol {repo: $repo, branch: '', id: nid}) "
            "MERGE (p)-[:TOUCHES]->(s)",
            key=task_key, pid=pr.id, repo=pr.repo, number=pr.number,
            url=pr.url, sha=pr.sha, touched=list(touched_node_ids or []))

    def task_context(self, key: str, project: str = "") -> dict:
        """Обход: задача + её PR/код + TASK_LINK-соседи и их PR. {} если не найдена.

        При project != "" соседи-задачи фильтруются по n.project (стабы без project
        и задачи чужих проектов отсекаются — PRI-170, критерий 3).
        """
        records, _, _ = self._driver.execute_query(
            "MATCH (t:Task) WHERE $k IN t.codes "
            "RETURN t.key AS key, t.title AS title, t.status AS status, t.url AS url, "
            "[ (t)-[:IMPLEMENTED_BY]->(p:PR) | "
            "  {id: p.id, url: p.url, sha: p.sha, "
            "   touched: [ (p)-[:TOUCHES]->(s:Symbol) | s.id ]} ] AS prs, "
            "[ (t)-[l:TASK_LINK]-(n:Task) WHERE ($project = '' OR n.project = $project) | "
            "  {key: n.key, title: n.title, status: n.status, type: l.type, "
            "   prs: [ (n)-[:IMPLEMENTED_BY]->(np:PR) | {id: np.id, url: np.url} ]} ] AS linked "
            "LIMIT 1",
            k=key, project=project)
        if not records:
            return {}
        r = records[0]
        # Неориентированный паттерн (t)-[l:TASK_LINK]-(n) может вернуть одного и
        # того же соседа дважды при взаимных рёбрах — дедуп по (key, type) с
        # сохранением порядка первого появления.
        linked = []
        seen = set()
        for n in r["linked"]:
            sig = (n["key"], n.get("type"))
            if sig in seen:
                continue
            seen.add(sig)
            linked.append(n)
        return {"key": r["key"], "title": r["title"], "status": r["status"],
                "url": r["url"], "prs": r["prs"], "linked": linked}

    def keys_with_prs(self, project: str = "") -> set[str]:
        """Ключи :Task с ребром IMPLEMENTED_BY; при project — только этого проекта."""
        if project:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task)-[:IMPLEMENTED_BY]->(:PR) WHERE t.project = $project "
                "RETURN t.key AS key", project=project)
        else:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task)-[:IMPLEMENTED_BY]->(:PR) RETURN t.key AS key")
        return {r["key"] for r in records}

    def list_keys(self, project: str = "") -> set[str]:
        """Ключи всех :Task (включая стабы); при project — только этого проекта.

        Стабы (upsert_links/link_pr) project не имеют → при scoped purge не попадают
        в скоуп проекта и не вычищаются чужим синком (PRI-170)."""
        if project:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task) WHERE t.project = $project RETURN t.key AS key",
                project=project)
        else:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task) RETURN t.key AS key")
        return {r["key"] for r in records}

    def delete_tasks(self, keys: list[str]) -> int:
        """Удалить :Task-узлы с рёбрами DETACH DELETE. :PR/:Symbol не трогает."""
        if not keys:
            return 0
        _, summary, _ = self._driver.execute_query(
            "MATCH (t:Task) WHERE t.key IN $keys DETACH DELETE t",
            keys=list(keys),
        )
        return summary.counters.nodes_deleted
