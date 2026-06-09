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

def test_import_aware_resolves_to_imported_module_only():
    # from utils import helper + helper() -> ребро только на utils.py#helper,
    # а НЕ на одноимённую функцию в другом модуле
    files = {
        "utils.py": "def helper():\n    return 1\n",
        "other.py": "def helper():\n    return 2\n",
        "main.py": "from utils import helper\ndef run():\n    return helper()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "main.py#run"]
    assert run_edges == [("main.py#run", "CALLS", "utils.py#helper")]
    assert ("main.py#run", "CALLS", "other.py#helper") not in edges

def test_local_definition_overrides_import():
    # локальное определение перекрывает импорт
    files = {
        "utils.py": "def helper():\n    return 1\n",
        "main.py": (
            "from utils import helper\n"
            "def helper():\n    return 9\n"
            "def run():\n    return helper()\n"
        ),
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "main.py#run"]
    assert run_edges == [("main.py#run", "CALLS", "main.py#helper")]
    assert ("main.py#run", "CALLS", "utils.py#helper") not in edges

def test_import_module_alias_attribute_call():
    # import utils as u + u.helper() -> ребро на utils.py#helper
    files = {
        "utils.py": "def helper():\n    return 1\n",
        "main.py": "import utils as u\ndef run():\n    return u.helper()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "main.py#run"]
    assert run_edges == [("main.py#run", "CALLS", "utils.py#helper")]

def test_relative_import_resolves_to_sibling_module():
    # относительный импорт from .utils import helper из pkg/mod.py -> pkg/utils.py#helper
    files = {
        "pkg/utils.py": "def helper():\n    return 1\n",
        "pkg/mod.py": "from .utils import helper\ndef run():\n    return helper()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "pkg/mod.py#run"]
    assert run_edges == [("pkg/mod.py#run", "CALLS", "pkg/utils.py#helper")]

def test_relative_import_two_dots_climbs_package():
    # from ..utils import helper из pkg/sub/mod.py -> pkg/utils.py#helper
    files = {
        "pkg/utils.py": "def helper():\n    return 1\n",
        "pkg/sub/mod.py": "from ..utils import helper\ndef run():\n    return helper()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "pkg/sub/mod.py#run"]
    assert run_edges == [("pkg/sub/mod.py#run", "CALLS", "pkg/utils.py#helper")]

def test_import_alias_with_as_name():
    # from utils import helper as h + h() -> ребро на utils.py#helper
    files = {
        "utils.py": "def helper():\n    return 1\n",
        "main.py": "from utils import helper as h\ndef run():\n    return h()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "main.py#run"]
    assert run_edges == [("main.py#run", "CALLS", "utils.py#helper")]

def test_star_import_resolves_to_source_module():
    # star-import: from utils import * + helper() -> ребро на utils.py#helper
    files = {
        "utils.py": "def helper():\n    return 1\n",
        "main.py": "from utils import *\ndef run():\n    return helper()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "main.py#run"]
    assert run_edges == [("main.py#run", "CALLS", "utils.py#helper")]

def test_fallback_to_all_matches_when_not_imported():
    # имя не в импортах и нет локального -> fallback на все совпадения (поведение v1)
    files = {
        "a.py": "def thing():\n    return 1\n",
        "b.py": "def thing():\n    return 2\n",
        "main.py": "def run():\n    return thing()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = sorted(e for e in edges if e[0] == "main.py#run")
    assert run_edges == [
        ("main.py#run", "CALLS", "a.py#thing"),
        ("main.py#run", "CALLS", "b.py#thing"),
    ]

def test_import_of_module_absent_in_repo_yields_no_edges():
    # импорт несуществующего в репо модуля -> рёбер нет, ничего не падает
    files = {
        "main.py": "from numpy import array\ndef run():\n    return array()\n",
    }
    nodes, edges = build_graph_from_files(files)
    run_edges = [e for e in edges if e[0] == "main.py#run"]
    assert run_edges == []
