import psycopg
import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow


def _row(ref, path, fqn):
    return ChunkRow(repo="a/x", ref=ref, content_hash=fqn, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text="code", embedding=[0.0] * 1024)


@pytest.mark.integration
def test_migrate_legacy_base_to_primary():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    store.upsert([_row("base", "a.py", "f")])
    store.set_index_meta("a/x", "base", "deadbeef")
    store.migrate_legacy_base("main")
    with psycopg.connect(s.pg_dsn) as conn:
        refs = {r[0] for r in conn.execute("SELECT DISTINCT ref FROM chunks").fetchall()}
        meta = conn.execute(
            "SELECT ref, sha FROM index_meta WHERE repo='a/x'").fetchone()
    assert refs == {"base:main"}
    assert meta == ("base:main", "deadbeef")


@pytest.mark.integration
def test_migrate_legacy_base_conflict_resistant():
    """Если ветка base:main уже проиндексирована, миграция legacy 'base' не падает.

    Сценарий: пользователь сделал `reviewer index --branch main` (есть base:main),
    затем `migrate-branches`. Legacy-строка с тем же (repo, path, symbol_fqn), что
    у уже существующей target-копии, не нарушает UNIQUE — она удаляется. Legacy-чанк
    без конфликта переносится.
    """
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    store.upsert([
        # конфликтный ключ: есть и legacy 'base', и уже существующая target 'base:main'
        _row("base", "a.py", "dup"),
        _row("base:main", "a.py", "dup"),
        # неконфликтный legacy-чанк — должен перенестись в base:main
        _row("base", "b.py", "solo"),
    ])
    n = store.migrate_legacy_base("main")
    with psycopg.connect(s.pg_dsn) as conn:
        refs = {r[0] for r in conn.execute("SELECT DISTINCT ref FROM chunks").fetchall()}
        dup_count = conn.execute(
            "SELECT count(*) FROM chunks WHERE repo='a/x' AND path='a.py' AND symbol_fqn='dup'"
        ).fetchone()[0]
        solo = conn.execute(
            "SELECT ref FROM chunks WHERE repo='a/x' AND path='b.py' AND symbol_fqn='solo'"
        ).fetchone()
    # (а) не упало; (б) legacy 'base' исчез
    assert refs == {"base:main"}
    # (в) для конфликтного ключа осталась ровно одна строка
    assert dup_count == 1
    # (г) неконфликтный legacy-чанк перенесён
    assert solo == ("base:main",)
    # rowcount = только фактически перенесённый неконфликтный чанк
    assert n == 1
