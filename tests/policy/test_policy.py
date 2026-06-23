from reviewer.policy.policy import ReviewPolicy
from reviewer.config.settings import Settings
from reviewer.vcs.base import Finding

def F(cat, sev, file="a.py", confidence=0.9):
    return Finding(cat, sev, file, 1, "RIGHT", "msg", None, confidence)

def test_gate_filters_disabled_category_low_severity_and_ignored_paths():
    p = ReviewPolicy.from_yaml("""
categories: {correctness: true, style: false}
severity_threshold: medium
paths: {ignore: ["vendor/**"]}
max_comments: 10
""")
    assert p.gate(F("correctness", "high")) is True
    assert p.gate(F("style", "high")) is False
    assert p.gate(F("correctness", "low")) is False
    assert p.gate(F("correctness", "high", "vendor/x.py")) is False
    assert p.max_comments == 10

def test_defaults_when_no_yaml():
    p = ReviewPolicy.from_yaml(None)
    assert p.gate(F("correctness", "medium")) is True


def test_min_confidence_gate():
    p = ReviewPolicy(min_confidence=0.7)
    assert p.gate(F("correctness", "high", confidence=0.9)) is True
    assert p.gate(F("correctness", "high", confidence=0.5)) is False


def test_enabled_only_whitelist():
    p = ReviewPolicy(enabled_only=["security"])
    assert p.gate(F("security", "high")) is True
    assert p.gate(F("correctness", "high")) is False


def test_load_env_defaults_then_yaml_override():
    s = Settings(_env_file=None)   # дефолты: medium / min_conf 0.5 / max 25 / без вайтлиста
    p = ReviewPolicy.load(s, None)
    assert p.severity_threshold == "medium"
    assert p.min_confidence == 0.5
    assert p.max_comments == 25
    assert p.output_language == "ru"   # env-дефолт, YAML отсутствует
    assert p.gate(F("correctness", "high", confidence=0.4)) is False   # ниже env-порога confidence

    p2 = ReviewPolicy.load(s, "severity_threshold: high\nmax_comments: 5\nmin_confidence: 0.8")
    assert p2.severity_threshold == "high"
    assert p2.max_comments == 5
    assert p2.min_confidence == 0.8
    assert p2.output_language == "ru"   # YAML без ключа не трогает env-дефолт

    p3 = ReviewPolicy.load(s, "output_language: en")
    assert p3.output_language == "en"   # YAML переопределяет env-дефолт


def test_output_language_default_and_yaml_override():
    p = ReviewPolicy.from_yaml("severity_threshold: medium")
    assert p.output_language == "ru"

    p2 = ReviewPolicy.from_yaml("output_language: en")
    assert p2.output_language == "en"


def test_task_board_parsed_from_yaml():
    p = ReviewPolicy.from_yaml("task_board: {type: yougile, mcp: yougile}")
    assert p.task_board == {"type": "yougile", "mcp": "yougile"}


def test_task_board_none_when_absent():
    p = ReviewPolicy.from_yaml("severity_threshold: low")
    assert p.task_board is None


def test_load_applies_task_board_from_yaml():
    s = Settings(_env_file=None)
    p = ReviewPolicy.load(s, "task_board: {type: jira, mcp: atlassian}")
    assert p.task_board == {"type": "jira", "mcp": "atlassian"}


def test_load_task_board_none_without_yaml():
    s = Settings(_env_file=None)
    p = ReviewPolicy.load(s, None)
    assert p.task_board is None


def test_from_settings_task_board_from_env(monkeypatch):
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    p = ReviewPolicy.from_settings(Settings(_env_file=None))
    assert p.task_board == {"type": "yougile", "mcp": "yougile"}


def test_load_task_board_from_env_default(monkeypatch):
    # без .review.yml глобальный env-дефолт доски используется как fallback
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    p = ReviewPolicy.load(Settings(_env_file=None), None)
    assert p.task_board == {"type": "yougile", "mcp": "yougile"}


def test_yaml_task_board_overrides_env_default(monkeypatch):
    # per-repo .review.yml переопределяет глобальный env-дефолт
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    p = ReviewPolicy.load(Settings(_env_file=None), "task_board: {type: jira, mcp: atlassian}")
    assert p.task_board == {"type": "jira", "mcp": "atlassian"}


def test_empty_yaml_task_board_disables_env_default(monkeypatch):
    # явный пустой task_board в .review.yml выключает доску, несмотря на env-дефолт
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    p = ReviewPolicy.load(Settings(_env_file=None), "task_board:")
    assert p.task_board is None


def test_requirements_category_enabled_by_default():
    p = ReviewPolicy.from_yaml(None)
    assert p.gate(F("requirements", "medium")) is True


def test_requirements_category_can_be_disabled_via_yaml():
    p = ReviewPolicy.from_yaml("categories: {requirements: false}")
    assert p.gate(F("requirements", "high")) is False


def test_requirements_excluded_by_enabled_only_whitelist():
    p = ReviewPolicy(enabled_only=["correctness"])
    assert p.gate(F("requirements", "high")) is False


def test_requirements_respects_severity_and_confidence():
    p = ReviewPolicy(severity_threshold="medium", min_confidence=0.7)
    assert p.gate(F("requirements", "high", confidence=0.9)) is True
    assert p.gate(F("requirements", "low", confidence=0.9)) is False
    assert p.gate(F("requirements", "high", confidence=0.5)) is False


def test_min_confidence_default_aligned_to_0_5():
    # dataclass-дефолт и from_yaml-дефолт согласованы с env (0.5)
    assert ReviewPolicy().min_confidence == 0.5
    assert ReviewPolicy.from_yaml(None).min_confidence == 0.5


def test_min_confidence_gate_predictable_set():
    # набор примеров приёмки: порог 0.5 отсекает предсказуемо
    p = ReviewPolicy(min_confidence=0.5)
    assert p.gate(F("correctness", "high", confidence=0.4)) is False
    assert p.gate(F("correctness", "high", confidence=0.49)) is False
    assert p.gate(F("correctness", "high", confidence=0.5)) is True
    assert p.gate(F("correctness", "high", confidence=0.8)) is True


def test_policy_grounding_max_distance_default_and_yaml():
    """Дефолт grounding_max_distance == 5; .review.yml с grounding_max_distance: 12 даёт 12."""
    assert ReviewPolicy().grounding_max_distance == 5
    p = ReviewPolicy.from_yaml("grounding_max_distance: 12")
    assert p.grounding_max_distance == 12


def test_summary_cluster_depth_env_default_and_yaml_override():
    s = Settings(_env_file=None)                 # env-дефолт summary_cluster_depth = 2
    p = ReviewPolicy.load(s, None)
    assert p.summary_cluster_depth == 2          # из env, YAML отсутствует

    p2 = ReviewPolicy.load(s, "summary_cluster_depth: 1")
    assert p2.summary_cluster_depth == 1         # .review.yml переопределяет env

    p3 = ReviewPolicy.load(s, "severity_threshold: high")
    assert p3.summary_cluster_depth == 2         # YAML без ключа не трогает env-дефолт


def test_summary_cluster_depth_from_yaml():
    p = ReviewPolicy.from_yaml("summary_cluster_depth: 3")
    assert p.summary_cluster_depth == 3
    assert ReviewPolicy.from_yaml(None).summary_cluster_depth == 2


def test_policy_summary_topk_threshold_override():
    from reviewer.config.settings import Settings
    from reviewer.policy.policy import ReviewPolicy
    s = Settings()
    p = ReviewPolicy.load(s, "summary_topk_threshold: 7")
    assert p.summary_topk_threshold == 7


def test_policy_summary_topk_threshold_default_from_settings():
    from reviewer.config.settings import Settings
    from reviewer.policy.policy import ReviewPolicy
    s = Settings()
    assert ReviewPolicy.from_settings(s).summary_topk_threshold == s.summary_topk_threshold
