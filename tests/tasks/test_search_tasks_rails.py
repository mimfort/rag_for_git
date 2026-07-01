from reviewer.tasks.service import TaskService


class _Hit:
    def __init__(self, key):
        self.key, self.title, self.status, self.score = key, key, "—", 0.02


class _Store:
    def __init__(self, n):
        self._hits = [_Hit(f"ID-{i}") for i in range(n)]
        self.calls = []

    def search(self, query, vec, top_k, candidates=50, project=None):
        self.calls.append({"top_k": top_k, "candidates": candidates})
        return self._hits[:top_k]


class _Emb:
    def embed_query(self, q):
        return [0.0] * 8


def _svc(n):
    return TaskService(store=_Store(n), graph=None, embedder=_Emb(), max_chars=8000)


def test_search_tasks_caps_at_ceiling_and_notes_tail():
    out = _svc(14).search_tasks("q")          # дефолт ceiling=8, found=14
    lines = [ln for ln in out.splitlines() if ln and ln[0].isdigit()]
    assert len(lines) == 8
    assert "показано 8 из 14" in out


def test_search_tasks_topk_override_raises_ceiling():
    out = _svc(14).search_tasks("q", top_k=12)
    lines = [ln for ln in out.splitlines() if ln and ln[0].isdigit()]
    assert len(lines) == 12


def test_search_tasks_no_tail_note_when_within_ceiling():
    out = _svc(3).search_tasks("q")
    assert "показано" not in out
