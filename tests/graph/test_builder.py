from reviewer.graph.builder import build_graph_from_files

def test_build_graph_extracts_cross_file_calls():
    files = {
        "a.py": "def f():\n    return 1\n",
        "b.py": "def g():\n    return f()\n",
    }
    nodes, edges = build_graph_from_files(files)
    assert "a.py#f" in nodes and "b.py#g" in nodes
    assert ("b.py#g", "CALLS", "a.py#f") in edges

def test_build_graph_method_call_by_name():
    files = {
        "m.py": "class A:\n    def helper(self):\n        return 1\n    def run(self):\n        return self.helper()\n",
    }
    nodes, edges = build_graph_from_files(files)
    assert "m.py#A.helper" in nodes and "m.py#A.run" in nodes
    # self.helper() -> CALLS A.helper (name-based resolution)
    assert ("m.py#A.run", "CALLS", "m.py#A.helper") in edges
