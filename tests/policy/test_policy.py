from reviewer.policy.policy import ReviewPolicy
from reviewer.vcs.base import Finding

def F(cat, sev, file="a.py"):
    return Finding(cat, sev, file, 1, "RIGHT", "msg", None, 0.9)

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
