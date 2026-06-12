import pytest
from pydantic import ValidationError

from reviewer.config.settings import Settings


def test_enum_fields_have_valid_defaults():
    """По умолчанию все enum-поля заполнены допустимыми значениями."""
    s = Settings(_env_file=None)
    assert s.review_severity_threshold == "medium"
    assert s.review_suggestions == "apply"
    assert s.graph_backend == "auto"


@pytest.mark.parametrize(
    ("var", "value", "field"),
    [
        ("REVIEW_SEVERITY_THRESHOLD", "medium-high", "review_severity_threshold"),
        ("REVIEW_SUGGESTIONS", "both", "review_suggestions"),
        ("GRAPH_BACKEND", "neo4j", "graph_backend"),
    ],
)
def test_invalid_enum_value_raises_validation_error(monkeypatch, var, value, field):
    """Недопустимые значения enum-полей отклоняются pydantic при старте."""
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    errors = exc_info.value.errors()
    assert any(e["loc"] == (field,) for e in errors)


@pytest.mark.parametrize(
    ("var", "value", "field"),
    [
        ("REVIEW_SEVERITY_THRESHOLD", "critical", "review_severity_threshold"),
        ("REVIEW_SUGGESTIONS", "text", "review_suggestions"),
        ("GRAPH_BACKEND", "scip", "graph_backend"),
    ],
)
def test_valid_enum_values_accepted(monkeypatch, var, value, field):
    """Допустимые значения из Literal-списка принимаются без ошибок."""
    monkeypatch.setenv(var, value)
    s = Settings(_env_file=None)
    assert getattr(s, field) == value
